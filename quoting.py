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
    Returns (bid_price, ask_price).
    """
    half_spread = market.max_incentive_spread * config.SPREAD_PCT
    bid = midpoint - half_spread
    ask = midpoint + half_spread

    bid = _clamp(_round_to_tick(bid, market.tick_size))
    ask = _clamp(_round_to_tick(ask, market.tick_size))

    # Ensure bid < ask
    if bid >= ask:
        tick = float(market.tick_size)
        bid = _clamp(midpoint - tick, 0.01, 0.99)
        ask = _clamp(midpoint + tick, 0.01, 0.99)

    return bid, ask


def compute_size(price: float, market: Market) -> float:
    """Compute order size in shares from dollar amount."""
    shares = config.ORDER_SIZE_USD / price
    return round(shares, 2)


def place_quotes(client: ClobClient, market: Market) -> QuotedMarket | None:
    """Place two-sided GTC limit orders on the YES token."""
    mid = get_midpoint(client, market.yes_token_id)
    if mid is None:
        log.error("Cannot get midpoint for %s, skipping", market.ticker)
        return None

    bid_price, ask_price = compute_quotes(market, mid)
    bid_size = compute_size(bid_price, market)
    ask_size = compute_size(ask_price, market)

    # Check minimum incentive size
    if market.min_incentive_size > 0:
        if bid_size < market.min_incentive_size:
            log.warning("%s: bid size %.1f < min_incentive_size %.1f (need $%.0f per side)",
                        market.ticker, bid_size, market.min_incentive_size,
                        market.min_incentive_size * bid_price)
        if ask_size < market.min_incentive_size:
            log.warning("%s: ask size %.1f < min_incentive_size %.1f",
                        market.ticker, ask_size, market.min_incentive_size)

    quoted = QuotedMarket(market=market, mid_at_placement=mid)

    # Place BUY order
    try:
        bid_order = client.create_order(
            OrderArgs(
                token_id=market.yes_token_id,
                price=bid_price,
                size=bid_size,
                side=SIDE_BUY,
            )
        )
        resp = client.post_order(bid_order, orderType=OrderType.GTC)
        quoted.bid_order_id = resp.get("orderID") or resp.get("id")
        log.info("%s BUY  %.2f @ $%.3f -> order %s",
                 market.ticker, bid_size, bid_price, quoted.bid_order_id)
    except Exception as e:
        log.error("%s BUY order failed: %s", market.ticker, e)

    # Place SELL order
    try:
        ask_order = client.create_order(
            OrderArgs(
                token_id=market.yes_token_id,
                price=ask_price,
                size=ask_size,
                side=SIDE_SELL,
            )
        )
        resp = client.post_order(ask_order, orderType=OrderType.GTC)
        quoted.ask_order_id = resp.get("orderID") or resp.get("id")
        log.info("%s SELL %.2f @ $%.3f -> order %s",
                 market.ticker, ask_size, ask_price, quoted.ask_order_id)
    except Exception as e:
        log.error("%s SELL order failed: %s", market.ticker, e)

    return quoted


def should_refresh(client: ClobClient, quoted: QuotedMarket) -> bool:
    """Check if midpoint has drifted beyond threshold."""
    mid = get_midpoint(client, quoted.market.yes_token_id)
    if mid is None:
        return False
    drift = abs(mid - quoted.mid_at_placement)
    if drift > config.REFRESH_THRESHOLD:
        log.info("%s midpoint drifted %.4f (%.3f -> %.3f), refreshing",
                 quoted.market.ticker, drift, quoted.mid_at_placement, mid)
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
