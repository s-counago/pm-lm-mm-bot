import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

import config

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class Market:
    ticker: str                 # e.g. "AAPL"
    question: str               # full event question
    condition_id: str
    yes_token_id: str
    no_token_id: str
    max_incentive_spread: float # in price units (e.g. 0.05)
    min_incentive_size: float   # minimum shares per side
    tick_size: str              # e.g. "0.01"


def _today_str() -> tuple[str, str]:
    """Return (month_name, day) for today in ET-ish UTC terms."""
    now = datetime.now(timezone.utc)
    return now.strftime("%B"), str(now.day)


def discover_markets(tickers: list[str] | None = None) -> list[Market]:
    """
    Find today's daily equity "Up or Down" markets via the Gamma API.
    Returns a Market for each configured ticker that has a live market today.
    """
    if tickers is None:
        tickers = config.TICKERS

    month, day = _today_str()
    markets: list[Market] = []

    for ticker in tickers:
        search = f"{ticker} Up or Down {month} {day}"
        log.info("Searching Gamma API for: %s", search)

        try:
            resp = requests.get(
                f"{GAMMA_API}/events",
                params={"title": search, "closed": "false", "limit": 5},
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            log.error("Gamma API request failed for %s: %s", ticker, e)
            continue

        if not events:
            log.warning("No events found for %s", ticker)
            continue

        # Find the best matching event
        matched_event = None
        for event in events:
            title = event.get("title", "")
            # Exact pattern: "{TICKER} Up or Down {Month} {Day}?"
            if ticker in title and "Up or Down" in title:
                matched_event = event
                break

        if not matched_event:
            log.warning("No matching event for %s in results: %s",
                        ticker, [e.get("title") for e in events])
            continue

        # Each event has markets (outcomes). For binary Up/Down, there's one market
        # with YES/NO tokens.
        event_markets = matched_event.get("markets", [])
        if not event_markets:
            log.warning("Event '%s' has no markets", matched_event.get("title"))
            continue

        mkt = event_markets[0]
        condition_id = mkt.get("conditionId", "")

        # Token IDs: clobTokenIds is a JSON string like '["yes_id", "no_id"]'
        clob_token_ids = mkt.get("clobTokenIds")
        if not clob_token_ids:
            log.warning("No clobTokenIds for %s", ticker)
            continue

        # Parse token IDs - could be a string or already a list
        if isinstance(clob_token_ids, str):
            import json
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except json.JSONDecodeError:
                log.warning("Could not parse clobTokenIds for %s: %s", ticker, clob_token_ids)
                continue

        if len(clob_token_ids) < 2:
            log.warning("Expected 2 token IDs for %s, got %d", ticker, len(clob_token_ids))
            continue

        yes_token_id = clob_token_ids[0]
        no_token_id = clob_token_ids[1]

        # Reward parameters
        rewards = mkt.get("rewards", {}) or {}
        max_spread = float(rewards.get("maxSpread", 0) or 0)
        min_size = float(rewards.get("minSize", 0) or 0)

        # If rewards aren't populated at event level, try market-level fields
        if max_spread == 0:
            max_spread = float(mkt.get("max_incentive_spread", 0) or 0)
        if min_size == 0:
            min_size = float(mkt.get("min_incentive_size", 0) or 0)

        # Tick size
        tick_size = str(mkt.get("minimum_tick_size", "0.01") or "0.01")

        market = Market(
            ticker=ticker,
            question=matched_event.get("title", ""),
            condition_id=condition_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            max_incentive_spread=max_spread,
            min_incentive_size=min_size,
            tick_size=tick_size,
        )
        markets.append(market)
        log.info("Found market: %s | condition=%s | spread=%.4f | min_size=%.1f",
                 market.question, condition_id[:12], max_spread, min_size)

    return markets


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    found = discover_markets()
    print(f"\nDiscovered {len(found)} markets:")
    for m in found:
        print(f"  {m.ticker}: {m.question}")
        print(f"    YES token: {m.yes_token_id[:20]}...")
        print(f"    spread: {m.max_incentive_spread}, min_size: {m.min_incentive_size}")
