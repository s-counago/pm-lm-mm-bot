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
        if qm.yes_exit_order_id is None:
            work.append((i, "yes_bal", lambda c=client, t=m.yes_token_id: get_token_balance(c, t)))
        if qm.no_exit_order_id is None:
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


def check_and_place_exits(
    client: ClobClient,
    qm: QuotedMarket,
    yes_bal: float,
    no_bal: float,
    mid: float | None,
) -> QuotedMarket:
    """Place SELL limit orders for filled positions using pre-fetched data."""
    market = qm.market

    # YES fill → SELL YES at ask
    if qm.yes_exit_order_id is None and yes_bal >= config.INVENTORY_MIN_SHARES:
        shares = math.floor(yes_bal * 100) / 100
        log.info("FILL %s YES | balance=%.2f shares", market.ticker, yes_bal)
        if mid is not None:
            _, ask_price = compute_quotes(market, mid)
            log.info("EXIT %s SELL YES %.2f @ $%.3f", market.ticker, shares, ask_price)
            try:
                order = client.create_order(
                    OrderArgs(
                        token_id=market.yes_token_id,
                        price=ask_price,
                        size=shares,
                        side=SIDE_SELL,
                    )
                )
                resp = client.post_order(order, orderType=OrderType.GTC)
                qm.yes_exit_order_id = resp.get("orderID") or resp.get("id")
                log.info("EXIT %s SELL YES placed -> %s", market.ticker, qm.yes_exit_order_id)
            except Exception as e:
                log.error("EXIT %s SELL YES failed: %s", market.ticker, e)

    # NO fill → SELL NO at (1 - bid)
    if qm.no_exit_order_id is None and no_bal >= config.INVENTORY_MIN_SHARES:
        shares = math.floor(no_bal * 100) / 100
        log.info("FILL %s NO  | balance=%.2f shares", market.ticker, no_bal)
        if mid is not None:
            bid_price, _ = compute_quotes(market, mid)
            sell_no_price = _round_to_tick(1.0 - bid_price, market.tick_size)
            log.info("EXIT %s SELL NO  %.2f @ $%.3f (= BUY YES @ $%.3f)",
                     market.ticker, shares, sell_no_price, bid_price)
            try:
                order = client.create_order(
                    OrderArgs(
                        token_id=market.no_token_id,
                        price=sell_no_price,
                        size=shares,
                        side=SIDE_SELL,
                    )
                )
                resp = client.post_order(order, orderType=OrderType.GTC)
                qm.no_exit_order_id = resp.get("orderID") or resp.get("id")
                log.info("EXIT %s SELL NO  placed -> %s", market.ticker, qm.no_exit_order_id)
            except Exception as e:
                log.error("EXIT %s SELL NO  failed: %s", market.ticker, e)

    return qm


def should_refresh(quoted: QuotedMarket, mid: float) -> bool:
    """Check if midpoint has drifted beyond threshold (using pre-fetched mid)."""
    drift_pct = abs(mid - quoted.mid_at_placement) / quoted.mid_at_placement
    log.info("%s | open: %.4f | now: %.4f | drift: %.1f%%",
             quoted.market.ticker, quoted.mid_at_placement, mid, drift_pct * 100)
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
    except Exception as e:
        log.error("%s cancel failed: %s", quoted.market.ticker, e)
    quoted.bid_order_id = None
    quoted.ask_order_id = None
    quoted.yes_exit_order_id = None
    quoted.no_exit_order_id = None


def refresh_quotes(client: ClobClient, quoted: QuotedMarket) -> QuotedMarket | None:
    """Cancel existing orders and re-place at current midpoint."""
    had_yes_exit = quoted.yes_exit_order_id is not None
    had_no_exit = quoted.no_exit_order_id is not None
    cancel_quoted(client, quoted)
    if had_yes_exit or had_no_exit:
        sides = [s for s, had in [("YES", had_yes_exit), ("NO", had_no_exit)] if had]
        log.info("%s exit orders cancelled (%s) — will re-place at new mid next cycle",
                 quoted.market.ticker, " + ".join(sides))
    return place_quotes(client, quoted.market)


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
