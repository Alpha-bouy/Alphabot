"""
polymarket/client.py — Wrapper around py-clob-client with retry logic,
credential management, and clean error handling.
"""

import os
import json
from typing import Optional, Dict, Any, Tuple
from tenacity import retry, stop_after_attempt, wait_exponential

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import (
    OrderArgs, MarketOrderArgs, OrderType, ApiCreds
)
from py_clob_client.order_builder.constants import BUY, SELL

from config import config
from logger import get_logger

log = get_logger("polymarket.client")

CREDS_FILE = "polymarket_creds.json"


class PolymarketClient:
    def __init__(self):
        self._client: Optional[ClobClient] = None
        self._address: str = ""
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return

        log.info("Initializing Polymarket CLOB client...")

        self._client = ClobClient(
            host           = config.CLOB_HOST,
            chain_id       = config.CHAIN_ID,
            key            = config.PRIVATE_KEY,
            signature_type = 0,
        )

        from eth_account import Account
        acct = Account.from_key(config.PRIVATE_KEY)
        self._address = acct.address
        log.info(f"Wallet: {self._address}")

        self._setup_api_creds()
        self._initialized = True
        log.info("Polymarket client ready ✓")

    def _setup_api_creds(self) -> None:
        if config.CLOB_API_KEY and config.CLOB_API_SECRET and config.CLOB_API_PASSPHRASE:
            log.info("Using API credentials from environment")
            creds = ApiCreds(
                api_key        = config.CLOB_API_KEY,
                api_secret     = config.CLOB_API_SECRET,
                api_passphrase = config.CLOB_API_PASSPHRASE,
            )
            self._client.set_api_creds(creds)
            return

        if os.path.exists(CREDS_FILE):
            with open(CREDS_FILE) as f:
                saved = json.load(f)
            creds = ApiCreds(
                api_key        = saved["key"],
                api_secret     = saved["secret"],
                api_passphrase = saved["passphrase"],
            )
            self._client.set_api_creds(creds)
            log.info(f"Loaded API creds from {CREDS_FILE}")
            return

        log.info("Deriving new API credentials...")
        try:
            creds = self._client.create_or_derive_api_creds()
            with open(CREDS_FILE, "w") as f:
                json.dump({
                    "key":        creds.api_key,
                    "secret":     creds.api_secret,
                    "passphrase": creds.api_passphrase,
                }, f, indent=2)
            log.info(f"API credentials saved to {CREDS_FILE}")
        except Exception as e:
            log.error(f"Failed to derive API credentials: {e}")
            raise

    # ── Balance ────────────────────────────────────────────────────────────

    def get_usdc_balance(self) -> float:
        """Return available USDC balance using correct py-clob-client method."""
        try:
            # Try multiple method names used across different versions
            for method in ["get_usdc_balance", "get_balance", "get_collateral_balance"]:
                if hasattr(self._client, method):
                    raw = getattr(self._client, method)()
                    return float(raw) / 1e6
            
            # Fallback: use requests directly to Polymarket balance API
            import requests
            resp = requests.get(
                f"{config.CLOB_HOST}/balance",
                headers={"POLY_ADDRESS": self._address},
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                return float(data.get("balance", 0)) / 1e6
            return 0.0
        except Exception as e:
            log.error(f"Balance fetch failed: {e}")
            return 0.0

    # ── Price Queries ──────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def get_token_price(self, token_id: str) -> Optional[float]:
        try:
            book = self._client.get_order_book(token_id)
            if not book or not book.asks:
                return None
            return float(book.asks[0].price)
        except Exception as e:
            log.warning(f"Price fetch failed: {e}")
            return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def get_token_bid(self, token_id: str) -> Optional[float]:
        try:
            book = self._client.get_order_book(token_id)
            if not book or not book.bids:
                return None
            return float(book.bids[0].price)
        except Exception as e:
            log.warning(f"Bid fetch failed: {e}")
            return None

    def get_mid_price(self, token_id: str) -> Optional[float]:
        ask = self.get_token_price(token_id)
        bid = self.get_token_bid(token_id)
        if ask is None or bid is None:
            return ask or bid
        return round((ask + bid) / 2, 4)

    def get_orderbook_depth(self, token_id: str, price_level: float, side: str = "ask") -> float:
        try:
            book = self._client.get_order_book(token_id)
            orders = book.asks if side == "ask" else book.bids
            total = 0.0
            for order in orders:
                p = float(order.price)
                if side == "ask" and p <= price_level + 0.02:
                    total += float(order.size)
                elif side == "bid" and p >= price_level - 0.02:
                    total += float(order.size)
            return total
        except Exception as e:
            log.warning(f"Orderbook depth check failed: {e}")
            return 0.0

    # ── Order Placement ────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=5))
    def market_buy(self, token_id: str, usdc_amount: float) -> Optional[Dict]:
        try:
            log.info(f"MARKET BUY: ${usdc_amount} on token {token_id[:20]}...")
            order = self._client.create_market_order(
                MarketOrderArgs(token_id=token_id, amount=usdc_amount, side=BUY)
            )
            resp = self._client.post_order(order, OrderType.FOK)
            if resp and resp.get("success"):
                log.info(f"✅ BUY filled: {resp.get('orderID','n/a')}")
            else:
                log.warning(f"BUY not filled: {resp}")
            return resp
        except Exception as e:
            log.error(f"Market buy failed: {e}")
            return None

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=5))
    def limit_sell(self, token_id: str, price: float, shares: float) -> Optional[Dict]:
        try:
            log.info(f"LIMIT SELL: {shares:.4f} shares @ ${price}")
            order_args = OrderArgs(
                token_id = token_id,
                price    = round(price, 4),
                size     = round(shares, 4),
                side     = SELL,
            )
            signed_order = self._client.create_order(order_args)
            resp = self._client.post_order(signed_order, OrderType.GTC)
            if resp and resp.get("success"):
                log.info(f"✅ SELL placed: {resp.get('orderID','n/a')}")
            return resp
        except Exception as e:
            log.error(f"Limit sell failed: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._client.cancel(order_id)
            return True
        except Exception as e:
            log.error(f"Cancel failed: {e}")
            return False

    def get_order_status(self, order_id: str) -> Optional[Dict]:
        try:
            return self._client.get_order(order_id)
        except Exception as e:
            log.warning(f"Order status failed: {e}")
            return None

    @property
    def address(self) -> str:
        return self._address


poly_client = PolymarketClient()
