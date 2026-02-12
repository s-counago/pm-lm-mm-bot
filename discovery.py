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
    ticker: str                 # e.g. "AAPL" or "Norway" (groupItemTitle)
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


def _parse_market(mkt: dict, label: str, question: str) -> Market | None:
    """Parse a single Gamma API market dict into a Market dataclass."""
    condition_id = mkt.get("conditionId", "")

    clob_token_ids = mkt.get("clobTokenIds")
    if not clob_token_ids:
        log.warning("No clobTokenIds for %s", label)
        return None

    if isinstance(clob_token_ids, str):
        try:
            clob_token_ids = json.loads(clob_token_ids)
        except json.JSONDecodeError:
            log.warning("Could not parse clobTokenIds for %s: %s", label, clob_token_ids)
            return None

    if len(clob_token_ids) < 2:
        log.warning("Expected 2 token IDs for %s, got %d", label, len(clob_token_ids))
        return None

    max_spread_cents = float(mkt.get("rewardsMaxSpread", 0) or 0)
    max_spread = max_spread_cents / 100.0
    min_size = float(mkt.get("rewardsMinSize", 0) or 0)
    tick_size = str(mkt.get("orderPriceMinTickSize", "0.01") or "0.01")

    return Market(
        ticker=label,
        question=question,
        condition_id=condition_id,
        yes_token_id=clob_token_ids[0],
        no_token_id=clob_token_ids[1],
        max_incentive_spread=max_spread,
        min_incentive_size=min_size,
        tick_size=tick_size,
    )


def _fetch_event(slug: str) -> dict | None:
    """Fetch event from Gamma API by slug. Returns event dict or None."""
    url = f"{GAMMA_API}/events/slug/{slug}"
    log.info("Fetching %s", url)
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 404:
            log.warning("No event found for slug: %s", slug)
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error("Gamma API request failed for slug %s: %s", slug, e)
        return None


def discover_markets() -> list[Market]:
    """
    Discover markets from both TICKERS (daily equity) and MARKETS (explicit slugs).
    Returns a Market for each live market found.
    """
    markets: list[Market] = []

    # --- Daily equity tickers ---
    for ticker in config.TICKERS:
        slug = _build_slug(ticker)
        event = _fetch_event(slug)
        if not event:
            continue

        event_markets = event.get("markets", [])
        if not event_markets:
            log.warning("Event '%s' has no markets", event.get("title"))
            continue

        m = _parse_market(event_markets[0], ticker, event.get("title", ""))
        if m:
            markets.append(m)
            log.info("Found: %s | spread=%.3f | min_size=%.0f | tick=%s",
                     m.question, m.max_incentive_spread, m.min_incentive_size, m.tick_size)

    # --- Explicit market slugs ---
    for entry in config.MARKETS:
        slug = entry["slug"]
        outcome = entry.get("outcome")

        event = _fetch_event(slug)
        if not event:
            continue

        event_markets = event.get("markets", [])
        if not event_markets:
            log.warning("Event '%s' has no markets", event.get("title"))
            continue

        question = event.get("title", "")

        if outcome:
            # Filter to the specific outcome
            matched = [m for m in event_markets if m.get("groupItemTitle") == outcome]
            if not matched:
                log.warning("Outcome '%s' not found in event '%s'", outcome, question)
                continue
            candidates = matched
        elif len(event_markets) == 1:
            # Single market (binary event)
            candidates = event_markets
        else:
            # Multiple markets — only keep incentivized ones
            candidates = [m for m in event_markets
                          if float(m.get("rewardsMaxSpread", 0) or 0) > 0]
            if not candidates:
                log.warning("No incentivized markets in event '%s'", question)
                continue

        for mkt in candidates:
            label = mkt.get("groupItemTitle") or slug
            m = _parse_market(mkt, label, question)
            if m:
                markets.append(m)
                log.info("Found: %s [%s] | spread=%.3f | min_size=%.0f | tick=%s",
                         m.question, m.ticker, m.max_incentive_spread,
                         m.min_incentive_size, m.tick_size)

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
