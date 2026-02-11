#!/usr/bin/env python3
"""
Polymarket Liquidity Mining Market Maker — MVP

Discovers daily equity "Up or Down" markets, places two-sided quotes
within the reward incentive spread, and refreshes when the midpoint drifts.
"""
import logging
import signal
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from client import build_client, get_usdc_balance
from discovery import discover_markets
from quoting import (
    QuotedMarket,
    cancel_all_quoted,
    place_quotes,
    refresh_quotes,
    should_refresh,
)

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def past_shutdown_time() -> bool:
    """Check if current ET time is past the configured shutdown time."""
    now_et = datetime.now(ET)
    h, m = config.SHUTDOWN_TIME.split(":")
    shutdown = now_et.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    return now_et >= shutdown


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 1. Initialize client
    log.info("Initializing CLOB client...")
    client = build_client()

    # 2. Check balance
    balance = get_usdc_balance(client)
    log.info("USDC balance: $%.2f", balance)
    if balance < config.ORDER_SIZE_USD * 2:
        log.warning("Low balance! Need at least $%.0f for one market (2 sides × $%.0f)",
                    config.ORDER_SIZE_USD * 2, config.ORDER_SIZE_USD)

    # 3. Discover markets
    log.info("Discovering markets for tickers: %s", config.TICKERS)
    markets = discover_markets()
    if not markets:
        log.error("No markets found. Exiting.")
        sys.exit(1)
    log.info("Found %d markets", len(markets))

    # Capital check
    total_needed = len(markets) * config.ORDER_SIZE_USD * 2
    if balance < total_needed:
        log.warning("Balance $%.2f < needed $%.0f for %d markets. Some may be undersized.",
                    balance, total_needed, len(markets))

    # 4. Place initial quotes
    quoted_markets: list[QuotedMarket] = []
    for market in markets:
        if market.max_incentive_spread <= 0:
            log.warning("%s: no incentive spread set, skipping", market.ticker)
            continue
        qm = place_quotes(client, market)
        if qm:
            quoted_markets.append(qm)

    if not quoted_markets:
        log.error("No quotes placed. Exiting.")
        sys.exit(1)

    log.info("Placed quotes on %d markets. Entering monitor loop (poll every %ds)...",
             len(quoted_markets), config.POLL_INTERVAL_SECONDS)

    # Graceful shutdown on Ctrl+C
    def handle_sigint(sig, frame):
        log.info("SIGINT received, cancelling all orders...")
        cancel_all_quoted(client, quoted_markets)
        log.info("All orders cancelled. Exiting.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)

    # 5. Monitor loop
    while True:
        time.sleep(config.POLL_INTERVAL_SECONDS)

        # Check shutdown time
        if past_shutdown_time():
            log.info("Past shutdown time (%s ET), cancelling all orders...", config.SHUTDOWN_TIME)
            cancel_all_quoted(client, quoted_markets)
            log.info("Shutdown complete.")
            break

        # Check each market for drift
        for i, qm in enumerate(quoted_markets):
            try:
                if should_refresh(client, qm):
                    new_qm = refresh_quotes(client, qm)
                    if new_qm:
                        quoted_markets[i] = new_qm
            except Exception as e:
                log.error("Error refreshing %s: %s", qm.market.ticker, e)


if __name__ == "__main__":
    main()
