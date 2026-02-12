#!/usr/bin/env python3
"""
Inventory Dumper — standalone script that periodically checks for filled
token positions and dumps them at market price via FOK/FAK orders.

Run alongside (or instead of) the main quoting bot:
    .venv/bin/python3 inventory.py
"""
import logging
import math
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderType,
)

import config
from client import build_client
from discovery import Market, discover_markets

log = logging.getLogger(__name__)

SIDE_SELL = "SELL"
COOLDOWN_SECONDS = 3  # skip token after successful sell while balance settles

# token_id -> timestamp of last successful sell
_last_sold: dict[str, float] = {}


def get_token_balance(client: ClobClient, token_id: str) -> float:
    """Get the conditional token balance (in shares) for a given token."""
    resp = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
    )
    raw = float(resp.get("balance", 0))
    # Conditional tokens use the same 6-decimal raw encoding as USDC
    return raw / 1e6


def dump_position(client: ClobClient, token_id: str, shares: float, label: str) -> bool:
    """
    Try to sell `shares` of a conditional token via a market order.
    Attempts FOK first (all-or-nothing); falls back to FAK (partial fill).
    Returns True if the order was posted successfully.
    """
    for order_type in (OrderType.FOK, OrderType.FAK):
        try:
            order = client.create_market_order(
                MarketOrderArgs(
                    token_id=token_id,
                    amount=shares,
                    side=SIDE_SELL,
                )
            )
            resp = client.post_order(order, orderType=order_type)
            order_id = resp.get("orderID") or resp.get("id")
            log.info(
                "%s SELL %.2f shares (%s) -> order %s",
                label, shares, order_type, order_id,
            )
            return True
        except Exception as e:
            log.warning("%s SELL %.2f (%s) failed: %s", label, shares, order_type, e)

    return False


def check_and_dump(client: ClobClient, markets: list[Market]) -> None:
    """Check all token positions in parallel and dump any above the threshold."""
    # 1. Collect tokens to check (skip cooldowns)
    to_check: list[tuple[str, str]] = []
    for market in markets:
        for side, token_id in [("YES", market.yes_token_id), ("NO", market.no_token_id)]:
            if time.time() - _last_sold.get(token_id, 0) < COOLDOWN_SECONDS:
                continue
            to_check.append((f"{market.ticker}/{side}", token_id))

    if not to_check:
        return

    # 2. Parallel balance fetch
    balances: dict[tuple[str, str], float] = {}
    with ThreadPoolExecutor(max_workers=len(to_check)) as pool:
        futures = {
            pool.submit(get_token_balance, client, token_id): (label, token_id)
            for label, token_id in to_check
        }
        for future in as_completed(futures):
            label, token_id = futures[future]
            try:
                balances[(label, token_id)] = future.result()
            except Exception as e:
                log.error("%s balance check failed: %s", label, e)

    # 3. Dump any positions found
    for (label, token_id), balance in balances.items():
        if balance < config.INVENTORY_MIN_SHARES:
            continue
        shares = math.floor(balance * 100) / 100
        log.info("%s balance=%.4f — dumping %.2f", label, balance, shares)
        if dump_position(client, token_id, shares, label):
            _last_sold[token_id] = time.time()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # 1. Build client
    log.info("Initializing CLOB client...")
    client = build_client()

    # 2. Discover markets
    log.info("Discovering markets for tickers=%s, markets=%s", config.TICKERS, config.MARKETS)
    markets = discover_markets()
    if not markets:
        log.error("No markets found. Exiting.")
        sys.exit(1)
    log.info("Found %d markets, monitoring inventory every %.1fs",
             len(markets), config.INVENTORY_POLL_SECONDS)

    # Graceful shutdown
    running = True

    def handle_sigint(sig, frame):
        nonlocal running
        log.info("SIGINT received, stopping...")
        running = False

    signal.signal(signal.SIGINT, handle_sigint)

    # 3. Poll loop
    while running:
        check_and_dump(client, markets)
        time.sleep(config.INVENTORY_POLL_SECONDS)

    log.info("Inventory dumper stopped.")


if __name__ == "__main__":
    main()
