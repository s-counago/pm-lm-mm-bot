import os
from dotenv import load_dotenv

load_dotenv()

# --- Credentials ---
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
CLOB_API_KEY = os.getenv("CLOB_API_KEY", "")
CLOB_SECRET = os.getenv("CLOB_SECRET", "")
CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")

# --- CLOB host ---
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

# --- Tickers to quote ---
TICKERS = ["SPX", "GOOGL", "MSFT"]  # auto-builds daily equity "Up or Down" slugs

# --- Explicit market slugs ---
# Each entry: {"slug": "event-slug"} for all incentivized outcomes,
# or {"slug": "event-slug", "outcome": "Norway"} for a specific outcome.
MARKETS: list[dict] = [
    #{"slug": "spx-opens-up-or-down-on-february-12-2026"},
    #{"slug": "bangladesh-parliamentary-election-winner-154"}
]

# --- Quoting parameters ---
# How much of max_incentive_spread to use (0.8 = 80% of allowed spread from mid)
# Tighter = more rewards but more adverse selection risk
SPREAD_PCT = 0.6

# Dollar amount per side per market
ORDER_SIZE_USD = 15.0

# Override order size for testing. Set to None to use min_incentive_size.
TEST_SIZE_OVERRIDE = None

# Midpoint drift as a percentage before cancel+re-place (e.g. 0.10 = 10%)
REFRESH_THRESHOLD_PCT = 0.005

# --- Timing ---
# How often to check midpoint for drift (seconds)
POLL_INTERVAL_SECONDS = 0.2

# Cancel all orders before NASDAQ close (ET timezone, 24h format "HH:MM")
SHUTDOWN_TIME = "15:50"

# --- Inventory dumper ---
INVENTORY_POLL_SECONDS = 0.5       # how often to check positions
INVENTORY_MIN_SHARES = 1.0         # ignore dust below this
