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
TICKERS = ["AAPL", "TSLA", "NVDA"]

# --- Quoting parameters ---
# How much of max_incentive_spread to use (0.8 = 80% of allowed spread from mid)
# Tighter = more rewards but more adverse selection risk
SPREAD_PCT = 0.8

# Dollar amount per side per market
ORDER_SIZE_USD = 15.0

# Midpoint drift in price units before cancel+re-place (e.g. 0.02 = 2 cents)
REFRESH_THRESHOLD = 0.02

# --- Timing ---
# How often to check midpoint for drift (seconds)
POLL_INTERVAL_SECONDS = 30

# Cancel all orders before NASDAQ close (ET timezone, 24h format "HH:MM")
SHUTDOWN_TIME = "15:50"
