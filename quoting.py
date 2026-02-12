import logging
import math
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


def place_quotes(client: ClobClient, market: Market) -> QuotedMarket | None:
    """Place two-sided GTC limit orders on the YES token."""
    mid = get_midpoint(client, market.yes_token_id)
    if mid is None:
        log.error("Cannot get midpoint for %s, skipping", market.ticker)
        return None

    bid_price, ask_price = compute_quotes(market, mid)
    size = compute_size(market)

    quoted = QuotedMarket(market=market, mid_at_placement=mid)

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
        i: {"yes_bal": 0.0, "no_bal": 0.0, "mid": None}
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
    yes_bal: float,
    no_bal: float,
    mid: float | None,
) -> QuotedMarket:
    """Handle the full per-market cycle: QUOTING (no inventory) or EXITING (has inventory)."""
    market = qm.market
    has_inventory = yes_bal >= config.INVENTORY_MIN_SHARES or no_bal >= config.INVENTORY_MIN_SHARES

    if mid is None:
        log.warning("%s no midpoint, skipping cycle", market.ticker)
        return qm

    if has_inventory:
        # --- EXITING mode: cancel quotes, manage exit orders ---

        # Cancel quotes if still open
        quote_ids = [oid for oid in (qm.bid_order_id, qm.ask_order_id) if oid]
        if quote_ids:
            log.info("%s PAUSE quotes, entering exit mode", market.ticker)
            try:
                client.cancel_orders(quote_ids)
                qm.bid_order_id = None
                qm.ask_order_id = None
            except Exception as e:
                log.error("%s cancel quotes failed: %s", market.ticker, e)

        # Check drift on existing exits → cancel for re-placement
        has_exits = qm.yes_exit_order_id is not None or qm.no_exit_order_id is not None
        if has_exits:
            parts = []
            if yes_bal >= config.INVENTORY_MIN_SHARES:
                parts.append(f"YES {yes_bal:.2f}")
            if no_bal >= config.INVENTORY_MIN_SHARES:
                parts.append(f"NO {no_bal:.2f}")
            exit_label = "EXIT " + " + ".join(parts) + " "
            if should_refresh(qm, mid, exit_label):
                exit_ids = [oid for oid in (qm.yes_exit_order_id, qm.no_exit_order_id) if oid]
                log.info("%s refreshing exit orders", market.ticker)
                try:
                    client.cancel_orders(exit_ids)
                    qm.yes_exit_order_id = None
                    qm.no_exit_order_id = None
                except Exception as e:
                    log.error("%s cancel exits failed: %s", market.ticker, e)

        # Place exit orders if needed
        if yes_bal >= config.INVENTORY_MIN_SHARES and qm.yes_exit_order_id is None:
            shares = math.floor(yes_bal * 100) / 100
            exit_price = _clamp(_round_to_tick(mid, market.tick_size), 0.001, 0.999)
            log.info("EXIT %s SELL YES %.2f @ $%.3f (mid)", market.ticker, shares, exit_price)
            try:
                order = client.create_order(
                    OrderArgs(
                        token_id=market.yes_token_id,
                        price=exit_price,
                        size=shares,
                        side=SIDE_SELL,
                    )
                )
                resp = client.post_order(order, orderType=OrderType.GTC)
                qm.yes_exit_order_id = resp.get("orderID") or resp.get("id")
                qm.mid_at_placement = mid
            except Exception as e:
                log.error("EXIT %s SELL YES failed: %s", market.ticker, e)

        if no_bal >= config.INVENTORY_MIN_SHARES and qm.no_exit_order_id is None:
            shares = math.floor(no_bal * 100) / 100
            exit_price = _clamp(_round_to_tick(1.0 - mid, market.tick_size), 0.001, 0.999)
            log.info("EXIT %s SELL NO %.2f @ $%.3f (= BUY YES @ mid $%.3f)",
                     market.ticker, shares, exit_price, mid)
            try:
                order = client.create_order(
                    OrderArgs(
                        token_id=market.no_token_id,
                        price=exit_price,
                        size=shares,
                        side=SIDE_SELL,
                    )
                )
                resp = client.post_order(order, orderType=OrderType.GTC)
                qm.no_exit_order_id = resp.get("orderID") or resp.get("id")
                qm.mid_at_placement = mid
            except Exception as e:
                log.error("EXIT %s SELL NO failed: %s", market.ticker, e)

    else:
        # --- QUOTING mode: manage quotes, clear stale exits ---

        # Cancel stale exit orders (may not have fully filled) and clear IDs
        if qm.yes_exit_order_id or qm.no_exit_order_id:
            exit_ids = [oid for oid in (qm.yes_exit_order_id, qm.no_exit_order_id) if oid]
            log.info("%s inventory cleared, cancelling exits and resuming quotes", market.ticker)
            try:
                client.cancel_orders(exit_ids)
            except Exception as e:
                log.error("%s cancel stale exits failed: %s", market.ticker, e)
            qm.yes_exit_order_id = None
            qm.no_exit_order_id = None

        # Place quotes if none open (first cycle or just transitioned from EXITING)
        if qm.bid_order_id is None and qm.ask_order_id is None:
            new_qm = place_quotes(client, market)
            if new_qm:
                return new_qm
            return qm

        # Check drift → refresh quotes
        if should_refresh(qm, mid, "QUOT "):
            cancel_quoted(client, qm)
            new_qm = place_quotes(client, market)
            if new_qm:
                return new_qm

    return qm


def should_refresh(quoted: QuotedMarket, mid: float, label: str = "") -> bool:
    """Check if midpoint has drifted beyond threshold (using pre-fetched mid)."""
    drift_pct = abs(mid - quoted.mid_at_placement) / quoted.mid_at_placement
    log.info("%s %s| open: %.4f | now: %.4f | drift: %.1f%%",
             quoted.market.ticker, label, quoted.mid_at_placement, mid, drift_pct * 100)
    return drift_pct > config.REFRESH_THRESHOLD_PCT


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
