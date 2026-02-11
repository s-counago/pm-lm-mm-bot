import logging
from dataclasses import dataclass

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

import config
from discovery import Market

log = logging.getLogger(__name__)

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"


@dataclass
class QuotedMarket:
    market: Market
    bid_order_id: str | None = None
    ask_order_id: str | None = None
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


TEST_SIZE_OVERRIDE = None  # Set to None to use min_incentive_size. Set to any integer to use that as order size


def compute_size(market: Market) -> float:
    """Return the minimum incentive size (always 100 shares for now)."""
    if TEST_SIZE_OVERRIDE is not None:
        return float(TEST_SIZE_OVERRIDE)
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


def should_refresh(client: ClobClient, quoted: QuotedMarket) -> bool:
    """Check if midpoint has drifted beyond threshold."""
    mid = get_midpoint(client, quoted.market.yes_token_id)
    if mid is None:
        return False
    drift_pct = abs(mid - quoted.mid_at_placement) / quoted.mid_at_placement
    log.info("%s | open: %.4f | now: %.4f | drift: %.1f%%",
             quoted.market.ticker, quoted.mid_at_placement, mid, drift_pct * 100)
    if drift_pct > config.REFRESH_THRESHOLD_PCT:
        return True
    return False


def cancel_quoted(client: ClobClient, quoted: QuotedMarket) -> None:
    """Cancel both orders for a quoted market."""
    order_ids = [oid for oid in (quoted.bid_order_id, quoted.ask_order_id) if oid]
    if not order_ids:
        return
    try:
        client.cancel_orders(order_ids)
        log.info("%s cancelled %d orders", quoted.market.ticker, len(order_ids))
    except Exception as e:
        log.error("%s cancel failed: %s", quoted.market.ticker, e)
    quoted.bid_order_id = None
    quoted.ask_order_id = None


def refresh_quotes(client: ClobClient, quoted: QuotedMarket) -> QuotedMarket | None:
    """Cancel existing orders and re-place at current midpoint."""
    cancel_quoted(client, quoted)
    return place_quotes(client, quoted.market)


def cancel_all_quoted(client: ClobClient, quoted_markets: list[QuotedMarket]) -> None:
    """Cancel all orders across all managed markets."""
    all_ids = []
    for qm in quoted_markets:
        if qm.bid_order_id:
            all_ids.append(qm.bid_order_id)
        if qm.ask_order_id:
            all_ids.append(qm.ask_order_id)
        qm.bid_order_id = None
        qm.ask_order_id = None

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
