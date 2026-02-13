import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

import config
from discovery import Market
from inventory import get_token_balance

log = logging.getLogger(__name__)

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"


@dataclass
class QuotedMarket:
    market: Market
    bid_order_id: str | None = None
    ask_order_id: str | None = None
    yes_exit_order_id: str | None = None
    no_exit_order_id: str | None = None
    mid_at_placement: float = 0.0
    inventory_since: float | None = None
    entry_mid: float = 0.0
    entry_bid_price: float = 0.0    # actual bid price placed (cost basis for YES fills)
    entry_ask_price: float = 0.0    # actual ask price placed (cost basis for NO fills)
    exit_price_placed: float = 0.0
    exit_cooldown_until: float = 0.0  # don't retry exits before this timestamp


def _round_to_tick(price: float, tick_size: str) -> float:
    """Round price to the nearest tick."""
    tick = float(tick_size)
    return round(round(price / tick) * tick, 4)


def _clamp(price: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, price))


def get_midpoint(client: ClobClient, token_id: str) -> float | None:
    """Fetch current midpoint price for a token."""
    try:
        resp = client.get_midpoint(token_id)
        mid = float(resp.get("mid", 0))
        if mid <= 0:
            return None
        return mid
    except Exception as e:
        log.error("Failed to get midpoint for %s: %s", token_id[:16], e)
        return None


def compute_quotes(market: Market, midpoint: float) -> tuple[float, float]:
    """
    Compute bid and ask prices around the midpoint.
    Returns (bid_price, ask_price), clamped to [0.001, 0.999].
    """
    half_spread = market.max_incentive_spread * config.SPREAD_PCT
    bid = midpoint - half_spread
    ask = midpoint + half_spread

    bid = _clamp(_round_to_tick(bid, market.tick_size), 0.001, 0.999)
    ask = _clamp(_round_to_tick(ask, market.tick_size), 0.001, 0.999)

    # Ensure bid < ask
    if bid >= ask:
        tick = float(market.tick_size)
        bid = _clamp(midpoint - tick, 0.001, 0.999)
        ask = _clamp(midpoint + tick, 0.001, 0.999)

    return bid, ask


def compute_size(market: Market) -> float:
    """Return the minimum incentive size, or TEST_SIZE_OVERRIDE if set."""
    if config.TEST_SIZE_OVERRIDE is not None:
        return float(config.TEST_SIZE_OVERRIDE)
    return market.min_incentive_size


def compute_exit_price(
    market: Market,
    mid: float,
    inventory_since: float,
    entry_mid: float,
    entry_price: float,
    side: str,
) -> float:
    """Compute exit price in native token space, anchored to entry cost.

    Returns YES-price for YES exits, NO-price for NO exits.

    Escalation: full-spread profit at t=0, breakeven at t=1.
    Stop-loss: if mid moves against position by >= STOP_LOSS_PCT, snap to mid.
    """
    elapsed = time.time() - inventory_since
    t = min(elapsed / config.EXIT_ESCALATION_SECONDS, 1.0)

    half_spread = market.max_incentive_spread * config.SPREAD_PCT

    # Stop-loss: only trigger when mid moves AGAINST our position
    if entry_mid > 0:
        if side == "YES":
            loss_pct = (entry_mid - mid) / entry_mid  # mid dropping = loss
        else:
            loss_pct = (mid - entry_mid) / entry_mid  # mid rising = loss for NO
        if loss_pct >= config.STOP_LOSS_PCT:
            if side == "YES":
                return _clamp(_round_to_tick(mid, market.tick_size), 0.01, 0.99)
            else:
                return _clamp(_round_to_tick(1.0 - mid, market.tick_size), 0.01, 0.99)

    if side == "YES":
        # Bought YES at entry_price. Sell at entry_price + edge, decaying to breakeven.
        price = entry_price + half_spread * (1.0 - t)
    else:
        # Bought NO at (1 - entry_price). Sell NO at cost + edge, decaying to breakeven.
        no_cost = 1.0 - entry_price
        price = no_cost + half_spread * (1.0 - t)

    return _clamp(_round_to_tick(price, market.tick_size), 0.01, 0.99)


def place_quotes(client: ClobClient, market: Market) -> QuotedMarket | None:
    """Place two-sided GTC limit orders on the YES token."""
    mid = get_midpoint(client, market.yes_token_id)
    if mid is None:
        log.error("Cannot get midpoint for %s, skipping", market.ticker)
        return None

    if mid < config.MIN_QUOTABLE_MID:
        log.warning("%s mid=%.3f < MIN_QUOTABLE_MID=%.2f, skipping (can't two-side)",
                    market.ticker, mid, config.MIN_QUOTABLE_MID)
        return None

    bid_price, ask_price = compute_quotes(market, mid)
    size = compute_size(market)

    quoted = QuotedMarket(market=market, mid_at_placement=mid,
                          entry_bid_price=bid_price, entry_ask_price=ask_price)

    # Place BUY order
    try:
        bid_order = client.create_order(
            OrderArgs(
                token_id=market.yes_token_id,
                price=bid_price,
                size=size,
                side=SIDE_BUY,
            )
        )
        resp = client.post_order(bid_order, orderType=OrderType.GTC)
        quoted.bid_order_id = resp.get("orderID") or resp.get("id")
        log.info("%s BUY  %.2f @ $%.3f -> order %s",
                 market.ticker, size, bid_price, quoted.bid_order_id)
    except Exception as e:
        log.error("%s BUY order failed: %s", market.ticker, e)

    # Place ask side: BUY NO at (1 - ask_price), equivalent to SELL YES at ask_price
    no_price = _round_to_tick(1.0 - ask_price, market.tick_size)
    try:
        ask_order = client.create_order(
            OrderArgs(
                token_id=market.no_token_id,
                price=no_price,
                size=size,
                side=SIDE_BUY,
            )
        )
        resp = client.post_order(ask_order, orderType=OrderType.GTC)
        quoted.ask_order_id = resp.get("orderID") or resp.get("id")
        log.info("%s BUY NO %.2f @ $%.3f (= SELL YES @ $%.3f) -> order %s",
                 market.ticker, size, no_price, ask_price, quoted.ask_order_id)
    except Exception as e:
        log.error("%s BUY NO order failed: %s", market.ticker, e)

    return quoted


def fetch_market_data(
    client: ClobClient,
    quoted_markets: list[QuotedMarket],
) -> list[tuple[float, float, float | None]]:
    """Fetch all balances + midpoints in parallel across all markets.

    Returns a list of (yes_bal, no_bal, mid) tuples, one per market.
    """
    # Build (index, callable) work items
    results: dict[int, dict[str, float | None]] = {
        i: {"yes_bal": None, "no_bal": None, "mid": None}
        for i in range(len(quoted_markets))
    }
    work: list[tuple[int, str, callable]] = []
    for i, qm in enumerate(quoted_markets):
        m = qm.market
        work.append((i, "yes_bal", lambda c=client, t=m.yes_token_id: get_token_balance(c, t)))
        work.append((i, "no_bal", lambda c=client, t=m.no_token_id: get_token_balance(c, t)))
        work.append((i, "mid", lambda c=client, t=m.yes_token_id: get_midpoint(c, t)))

    with ThreadPoolExecutor(max_workers=min(len(work), 3)) as pool:
        futures = {pool.submit(fn): (idx, key) for idx, key, fn in work}
        for future in futures:
            idx, key = futures[future]
            try:
                results[idx][key] = future.result()
            except Exception as e:
                ticker = quoted_markets[idx].market.ticker
                log.error("%s fetch %s failed: %s", ticker, key, e)

    return [(r["yes_bal"], r["no_bal"], r["mid"]) for r in results.values()]


def process_market_cycle(
    client: ClobClient,
    qm: QuotedMarket,
    yes_bal: float | None,
    no_bal: float | None,
    mid: float | None,
) -> QuotedMarket:
    """Unified per-market cycle: always quote (with asymmetric sizing) + manage exits."""
    market = qm.market

    has_yes = yes_bal is not None and yes_bal >= config.INVENTORY_MIN_SHARES
    has_no = no_bal is not None and no_bal >= config.INVENTORY_MIN_SHARES
    has_inventory = has_yes or has_no

    # If balance fetch failed but we have active exit orders, don't change state
    if not has_inventory and (yes_bal is None or no_bal is None):
        if qm.yes_exit_order_id or qm.no_exit_order_id:
            log.warning("%s balance fetch failed, keeping current state", market.ticker)
            return qm

    if mid is None:
        log.warning("%s no midpoint, skipping cycle", market.ticker)
        return qm

    # Park if mid dropped below quotable threshold
    if mid < config.MIN_QUOTABLE_MID:
        log.warning("%s mid=%.3f below MIN_QUOTABLE_MID, parking", market.ticker, mid)
        cancel_quoted(client, qm)
        return qm

    # --- Track inventory entry state ---
    if has_inventory and qm.inventory_since is None:
        qm.inventory_since = time.time()
        qm.entry_mid = mid
        log.info("%s inventory detected (YES=%.1f NO=%.1f), entry_mid=%.3f",
                 market.ticker, yes_bal or 0, no_bal or 0, mid)
    elif not has_inventory:
        if qm.inventory_since is not None:
            log.info("%s inventory cleared", market.ticker)
        qm.inventory_since = None
        qm.entry_mid = 0.0
        qm.exit_cooldown_until = 0.0
        # Cancel stale exit orders
        exit_ids = [oid for oid in (qm.yes_exit_order_id, qm.no_exit_order_id) if oid]
        if exit_ids:
            try:
                client.cancel_orders(exit_ids)
            except Exception as e:
                log.error("%s cancel stale exits failed: %s", market.ticker, e)
            qm.yes_exit_order_id = None
            qm.no_exit_order_id = None

    # --- QUOTE MANAGEMENT (asymmetric sizing) ---
    # When holding inventory, stop quoting the side that would add to exposure
    want_bid = not has_yes   # don't buy more YES if already holding
    want_ask = not has_no    # don't buy more NO if already holding

    # Cancel unwanted sides
    if not want_bid and qm.bid_order_id:
        log.info("%s cancelling bid (holding YES inventory)", market.ticker)
        try:
            client.cancel_orders([qm.bid_order_id])
            qm.bid_order_id = None
        except Exception as e:
            log.error("%s cancel bid failed: %s", market.ticker, e)

    if not want_ask and qm.ask_order_id:
        log.info("%s cancelling ask (holding NO inventory)", market.ticker)
        try:
            client.cancel_orders([qm.ask_order_id])
            qm.ask_order_id = None
        except Exception as e:
            log.error("%s cancel ask failed: %s", market.ticker, e)

    # Check drift on active quotes
    has_active_quotes = (want_bid and qm.bid_order_id) or (want_ask and qm.ask_order_id)
    needs_refresh = has_active_quotes and should_refresh(qm, mid, "QUOT ")

    # Determine which sides need (re)placement
    needs_bid = want_bid and (qm.bid_order_id is None or needs_refresh)
    needs_ask = want_ask and (qm.ask_order_id is None or needs_refresh)

    if needs_bid or needs_ask:
        # Cancel existing quotes before re-placing (for refresh)
        if needs_refresh:
            cancel_ids = [oid for oid in (qm.bid_order_id, qm.ask_order_id) if oid]
            if cancel_ids:
                try:
                    client.cancel_orders(cancel_ids)
                    qm.bid_order_id = None
                    qm.ask_order_id = None
                except Exception as e:
                    log.error("%s cancel for refresh failed: %s", market.ticker, e)

        bid_price, ask_price = compute_quotes(market, mid)
        size = compute_size(market)

        if needs_bid and qm.bid_order_id is None:
            try:
                order = client.create_order(
                    OrderArgs(token_id=market.yes_token_id, price=bid_price,
                              size=size, side=SIDE_BUY)
                )
                resp = client.post_order(order, orderType=OrderType.GTC)
                qm.bid_order_id = resp.get("orderID") or resp.get("id")
                qm.entry_bid_price = bid_price
                qm.mid_at_placement = mid
                log.info("%s BUY YES %.2f @ $%.3f -> %s",
                         market.ticker, size, bid_price, qm.bid_order_id)
            except Exception as e:
                log.error("%s BUY YES failed: %s", market.ticker, e)

        if needs_ask and qm.ask_order_id is None:
            no_price = _round_to_tick(1.0 - ask_price, market.tick_size)
            try:
                order = client.create_order(
                    OrderArgs(token_id=market.no_token_id, price=no_price,
                              size=size, side=SIDE_BUY)
                )
                resp = client.post_order(order, orderType=OrderType.GTC)
                qm.ask_order_id = resp.get("orderID") or resp.get("id")
                qm.entry_ask_price = ask_price
                qm.mid_at_placement = mid
                log.info("%s BUY NO %.2f @ $%.3f (= SELL YES @ $%.3f) -> %s",
                         market.ticker, size, no_price, ask_price, qm.ask_order_id)
            except Exception as e:
                log.error("%s BUY NO failed: %s", market.ticker, e)

    # --- EXIT MANAGEMENT ---
    if has_inventory and time.time() >= qm.exit_cooldown_until:
        _manage_exits(client, qm, yes_bal if has_yes else 0.0,
                      no_bal if has_no else 0.0, mid)

    return qm


def _manage_exits(
    client: ClobClient,
    qm: QuotedMarket,
    yes_bal: float,
    no_bal: float,
    mid: float,
) -> None:
    """Place or refresh exit orders for held inventory."""
    market = qm.market
    elapsed = time.time() - qm.inventory_since

    # --- Check if existing exit orders need price refresh ---
    if qm.yes_exit_order_id or qm.no_exit_order_id:
        if qm.yes_exit_order_id:
            new_exit = compute_exit_price(
                market, mid, qm.inventory_since, qm.entry_mid,
                qm.entry_bid_price, "YES")
        else:
            new_exit = compute_exit_price(
                market, mid, qm.inventory_since, qm.entry_mid,
                qm.entry_ask_price, "NO")

        price_changed = abs(new_exit - qm.exit_price_placed) >= float(market.tick_size)
        log.info("%s EXIT | placed: $%.3f | target: $%.3f | %.0fs elapsed",
                 market.ticker, qm.exit_price_placed, new_exit, elapsed)

        if price_changed:
            exit_ids = [oid for oid in (qm.yes_exit_order_id, qm.no_exit_order_id) if oid]
            log.info("%s refreshing exits (price moved >= 1 tick)", market.ticker)
            try:
                client.cancel_orders(exit_ids)
                qm.yes_exit_order_id = None
                qm.no_exit_order_id = None
            except Exception as e:
                log.error("%s cancel exits failed: %s", market.ticker, e)

    # --- Place YES exit ---
    if yes_bal >= config.INVENTORY_MIN_SHARES and qm.yes_exit_order_id is None:
        shares = math.floor(yes_bal * 100) / 100
        exit_price = compute_exit_price(
            market, mid, qm.inventory_since, qm.entry_mid,
            qm.entry_bid_price, "YES")
        log.info("EXIT %s SELL YES %.2f @ $%.3f (entry $%.3f, %.0fs elapsed)",
                 market.ticker, shares, exit_price, qm.entry_bid_price, elapsed)
        try:
            order = client.create_order(
                OrderArgs(token_id=market.yes_token_id, price=exit_price,
                          size=shares, side=SIDE_SELL)
            )
            resp = client.post_order(order, orderType=OrderType.GTC)
            qm.yes_exit_order_id = resp.get("orderID") or resp.get("id")
            qm.exit_price_placed = exit_price
        except Exception as e:
            if "not enough balance" in str(e) or "allowance" in str(e):
                log.info("EXIT %s YES already sold, cooldown 5s", market.ticker)
                qm.exit_cooldown_until = time.time() + 5.0
            else:
                log.error("EXIT %s SELL YES failed: %s", market.ticker, e)

    # --- Place NO exit ---
    if no_bal >= config.INVENTORY_MIN_SHARES and qm.no_exit_order_id is None:
        shares = math.floor(no_bal * 100) / 100
        exit_price = compute_exit_price(
            market, mid, qm.inventory_since, qm.entry_mid,
            qm.entry_ask_price, "NO")
        log.info("EXIT %s SELL NO %.2f @ $%.3f (entry ask $%.3f, %.0fs elapsed)",
                 market.ticker, shares, exit_price, qm.entry_ask_price, elapsed)
        try:
            order = client.create_order(
                OrderArgs(token_id=market.no_token_id, price=exit_price,
                          size=shares, side=SIDE_SELL)
            )
            resp = client.post_order(order, orderType=OrderType.GTC)
            qm.no_exit_order_id = resp.get("orderID") or resp.get("id")
            qm.exit_price_placed = exit_price
        except Exception as e:
            if "not enough balance" in str(e) or "allowance" in str(e):
                log.info("EXIT %s NO already sold, cooldown 5s", market.ticker)
                qm.exit_cooldown_until = time.time() + 5.0
            else:
                log.error("EXIT %s SELL NO failed: %s", market.ticker, e)


def should_refresh(quoted: QuotedMarket, mid: float, label: str = "",
                   threshold: float | None = None) -> bool:
    """Check if midpoint has drifted beyond threshold (using pre-fetched mid)."""
    thr = threshold if threshold is not None else config.REFRESH_THRESHOLD_PCT
    drift_pct = abs(mid - quoted.mid_at_placement) / quoted.mid_at_placement
    log.info("%s %s| open: %.4f | now: %.4f | drift: %.1f%%",
             quoted.market.ticker, label, quoted.mid_at_placement, mid, drift_pct * 100)
    return drift_pct > thr


def cancel_quoted(client: ClobClient, quoted: QuotedMarket) -> None:
    """Cancel all orders (quotes + exits) for a quoted market."""
    order_ids = [oid for oid in (
        quoted.bid_order_id, quoted.ask_order_id,
        quoted.yes_exit_order_id, quoted.no_exit_order_id,
    ) if oid]
    if not order_ids:
        return

    try:
        client.cancel_orders(order_ids)
        parts = []
        if quoted.bid_order_id or quoted.ask_order_id:
            parts.append("quotes")
        if quoted.yes_exit_order_id:
            parts.append("YES exit")
        if quoted.no_exit_order_id:
            parts.append("NO exit")
        log.info("%s cancelled %d orders (%s)", quoted.market.ticker, len(order_ids), " + ".join(parts))
        quoted.bid_order_id = None
        quoted.ask_order_id = None
        quoted.yes_exit_order_id = None
        quoted.no_exit_order_id = None
    except Exception as e:
        log.error("%s cancel failed: %s", quoted.market.ticker, e)



def cancel_all_quoted(client: ClobClient, quoted_markets: list[QuotedMarket]) -> None:
    """Cancel all orders (quotes + exits) across all managed markets."""
    all_ids = []
    for qm in quoted_markets:
        for oid in (qm.bid_order_id, qm.ask_order_id,
                    qm.yes_exit_order_id, qm.no_exit_order_id):
            if oid:
                all_ids.append(oid)
        qm.bid_order_id = None
        qm.ask_order_id = None
        qm.yes_exit_order_id = None
        qm.no_exit_order_id = None

    if not all_ids:
        log.info("No orders to cancel")
        return

    try:
        client.cancel_orders(all_ids)
        log.info("Cancelled %d orders across all markets", len(all_ids))
    except Exception as e:
        log.error("Bulk cancel failed: %s, trying cancel_all", e)
        try:
            client.cancel_all()
            log.info("cancel_all succeeded")
        except Exception as e2:
            log.error("cancel_all also failed: %s", e2)
