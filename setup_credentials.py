"""
setup_credentials.py — Run this ONCE to generate Polymarket API credentials.

Usage:
  python setup_credentials.py

This will:
  1. Connect to Polymarket CLOB using your private key
  2. Sign a message to derive API key/secret/passphrase
  3. Save them to polymarket_creds.json AND print them for your .env

Run BEFORE starting the bot for the first time.
"""

import os
import json
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

if not PRIVATE_KEY:
    print("❌ ERROR: PRIVATE_KEY not set in .env")
    exit(1)

print("🔑 Polymarket Credential Setup")
print("=" * 40)

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON

    client = ClobClient(
        host     = "https://clob.polymarket.com",
        chain_id = POLYGON,
        key      = PRIVATE_KEY,
        signature_type = 0,
    )

    from eth_account import Account
    acct = Account.from_key(PRIVATE_KEY)
    print(f"Wallet address: {acct.address}")
    print("Deriving API credentials (requires one on-chain signature)...")

    creds = client.create_or_derive_api_creds()

    output = {
        "key":        creds.api_key,
        "secret":     creds.api_secret,
        "passphrase": creds.api_passphrase,
    }

    with open("polymarket_creds.json", "w") as f:
        json.dump(output, f, indent=2)

    print("\n✅ Credentials derived and saved to polymarket_creds.json")
    print("\nAdd these to your .env file:")
    print(f"  CLOB_API_KEY={creds.api_key}")
    print(f"  CLOB_API_SECRET={creds.api_secret}")
    print(f"  CLOB_API_PASSPHRASE={creds.api_passphrase}")
    print("\nOr leave .env empty — bot will auto-load from polymarket_creds.json")

except Exception as e:
    print(f"❌ Error: {e}")
    print("\nMake sure py-clob-client is installed: pip install -r requirements.txt")
