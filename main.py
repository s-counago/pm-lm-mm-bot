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

import config
from client import build_client, get_usdc_balance, refresh_allowances
from discovery import discover_markets
from quoting import (
    QuotedMarket,
    cancel_all_quoted,
    fetch_market_data,
    place_quotes,
    process_market_cycle,
)

log = logging.getLogger(__name__)


def main():
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s",
                            datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    logfile = logging.FileHandler("mm.log")
    logfile.setFormatter(fmt)
    root.addHandler(logfile)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

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
    log.info("Discovering markets for tickers=%s, markets=%s", config.TICKERS, config.MARKETS)
    markets = discover_markets()
    if not markets:
        log.error("No markets found. Exiting.")
        sys.exit(1)
    log.info("Found %d markets", len(markets))

    # Refresh CLOB-cached allowances for all discovered tokens
    all_token_ids = []
    for m in markets:
        all_token_ids.extend([m.yes_token_id, m.no_token_id])
    refresh_allowances(client, all_token_ids)

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

        # Parallel fetch all balances + midpoints
        print()
        market_data = fetch_market_data(client, quoted_markets)

        for i, (qm, (yes_bal, no_bal, mid)) in enumerate(
            zip(quoted_markets, market_data)
        ):
            try:
                quoted_markets[i] = process_market_cycle(client, qm, yes_bal, no_bal, mid)
            except Exception as e:
                log.error("Error processing %s: %s", qm.market.ticker, e)


if __name__ == "__main__":
    main()
