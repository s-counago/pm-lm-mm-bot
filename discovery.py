import json
import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import config

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
ET = ZoneInfo("America/New_York")


@dataclass
class Market:
    ticker: str                 # e.g. "AAPL"
    question: str               # full event question
    condition_id: str
    yes_token_id: str
    no_token_id: str
    max_incentive_spread: float # in price units (e.g. 0.055)
    min_incentive_size: float   # minimum shares per side
    tick_size: str              # e.g. "0.001"


def _build_slug(ticker: str) -> str:
    """
    Build the Gamma API event slug for today's daily equity market.
    Format: {ticker}-up-or-down-on-{month}-{day}-{year}
    e.g. "coin-up-or-down-on-february-11-2026"
    """
    now = datetime.now(ET)
    month = now.strftime("%B").lower()
    day = now.day
    year = now.year
    return f"{ticker.lower()}-up-or-down-on-{month}-{day}-{year}"


def discover_markets(tickers: list[str] | None = None) -> list[Market]:
    """
    Find today's daily equity "Up or Down" markets via the Gamma API slug endpoint.
    Returns a Market for each configured ticker that has a live market today.
    """
    if tickers is None:
        tickers = config.TICKERS

    markets: list[Market] = []

    for ticker in tickers:
        slug = _build_slug(ticker)
        url = f"{GAMMA_API}/events/slug/{slug}"
        log.info("Fetching %s", url)

        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 404:
                log.warning("No event found for %s (slug: %s)", ticker, slug)
                continue
            resp.raise_for_status()
            event = resp.json()
        except Exception as e:
            log.error("Gamma API request failed for %s: %s", ticker, e)
            continue

        event_markets = event.get("markets", [])
        if not event_markets:
            log.warning("Event '%s' has no markets", event.get("title"))
            continue

        mkt = event_markets[0]
        condition_id = mkt.get("conditionId", "")

        # Token IDs: clobTokenIds is a JSON string like '["yes_id", "no_id"]'
        clob_token_ids = mkt.get("clobTokenIds")
        if not clob_token_ids:
            log.warning("No clobTokenIds for %s", ticker)
            continue

        if isinstance(clob_token_ids, str):
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

        # Reward parameters — top-level market fields, spread is in cents
        max_spread_cents = float(mkt.get("rewardsMaxSpread", 0) or 0)
        max_spread = max_spread_cents / 100.0  # convert cents → price units
        min_size = float(mkt.get("rewardsMinSize", 0) or 0)

        tick_size = str(mkt.get("orderPriceMinTickSize", "0.01") or "0.01")

        market = Market(
            ticker=ticker,
            question=event.get("title", ""),
            condition_id=condition_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            max_incentive_spread=max_spread,
            min_incentive_size=min_size,
            tick_size=tick_size,
        )
        markets.append(market)
        log.info("Found: %s | spread=%.3f | min_size=%.0f | tick=%s",
                 market.question, max_spread, min_size, tick_size)

    return markets


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    found = discover_markets()
    print(f"\nDiscovered {len(found)} markets:")

    # Optionally fetch midpoints if client credentials are available
    mid_available = False
    try:
        from client import build_client
        from quoting import get_midpoint
        client = build_client()
        mid_available = True
    except Exception:
        pass

    for m in found:
        mid_str = ""
        if mid_available:
            mid = get_midpoint(client, m.yes_token_id)
            mid_str = f"  mid: {mid:.3f}" if mid else "  mid: N/A"
        print(f"  {m.ticker}: {m.question}")
        print(f"    YES token: {m.yes_token_id[:20]}...")
        print(f"    spread: {m.max_incentive_spread}, min_size: {m.min_incentive_size}, tick: {m.tick_size}{mid_str}")
