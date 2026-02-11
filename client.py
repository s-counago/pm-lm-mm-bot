import logging
import sys

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams

import config

log = logging.getLogger(__name__)

CLOB_HOST = config.CLOB_HOST
CHAIN_ID = config.CHAIN_ID


def build_client() -> ClobClient:
    """Build and return an authenticated L2 ClobClient."""
    creds = None
    if config.CLOB_API_KEY and config.CLOB_SECRET and config.CLOB_PASSPHRASE:
        creds = ApiCreds(
            api_key=config.CLOB_API_KEY,
            api_secret=config.CLOB_SECRET,
            api_passphrase=config.CLOB_PASSPHRASE,
        )

    funder = config.FUNDER_ADDRESS or None
    # Use POLY_GNOSIS_SAFE (2) signature type when a funder/proxy wallet is set
    sig_type = 2 if funder else 0

    client = ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=config.PRIVATE_KEY,
        creds=creds,
        signature_type=sig_type,
        funder=funder,
    )

    if creds is None:
        log.info("No API creds in .env, deriving from private key...")
        creds = client.create_or_derive_api_creds()
        if creds is None:
            log.error("Failed to derive API credentials")
            sys.exit(1)
        client.set_api_creds(creds)
        log.info("API creds derived. Add these to your .env to skip derivation:")
        log.info("  CLOB_API_KEY=%s", creds.api_key)
        log.info("  CLOB_SECRET=%s", creds.api_secret)
        log.info("  CLOB_PASSPHRASE=%s", creds.api_passphrase)

    return client


def get_usdc_balance(client: ClobClient) -> float:
    """Get the USDC collateral balance available for trading."""
    try:
        resp = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        balance = float(resp.get("balance", 0))
        # Balance is in raw USDC units (6 decimals)
        return balance / 1e6
    except Exception as e:
        log.warning("Could not fetch USDC balance: %s", e)
        return 0.0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if "--derive-keys" in sys.argv:
        c = ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID, key=config.PRIVATE_KEY)
        creds = c.create_or_derive_api_creds()
        if creds:
            print(f"CLOB_API_KEY={creds.api_key}")
            print(f"CLOB_SECRET={creds.api_secret}")
            print(f"CLOB_PASSPHRASE={creds.api_passphrase}")
        else:
            print("Failed to derive credentials")
            sys.exit(1)
    else:
        client = build_client()
        balance = get_usdc_balance(client)
        print(f"USDC balance: ${balance:.2f}")
