"""
polymarket/client.py — Wrapper around py-clob-client with retry logic,
credential management, and clean error handling.
"""

import os
import json
from typing import Optional, Dict, Any, Tuple
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

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
    """
    Thread-safe Polymarket CLOB client.
    Handles: auth, order placement, price queries, position queries.
    """

    def __init__(self):
        self._client: Optional[ClobClient] = None
        self._address: str = ""
        self._initialized = False

    def initialize(self) -> None:
        """
        Set up CLOB client + API credentials.
        Called once at bot startup.
        """
        if self._initialized:
            return

        log.info("Initializing Polymarket CLOB client...")

        # Build client with private key
        self._client = ClobClient(
            host      = config.CLOB_HOST,
            chain_id  = config.CHAIN_ID,
            key       = config.PRIVATE_KEY,
            signature_type = 0,   # EOA wallet (not Gnosis Safe)
        )

        # Derive wallet address
        from eth_account import Account
        acct = Account.from_key(config.PRIVATE_KEY)
        self._address = acct.address
        log.info(f"Wallet: {self._address}")

        # Get or create API credentials
        self._setup_api_creds()
        self._initialized = True
        log.info("Polymarket client ready ✓")

    def _setup_api_creds(self) -> None:
        """Load saved creds or derive new ones from wallet signature."""
        # If env has explicit creds, use them
        if config.CLOB_API_KEY and config.CLOB_API_SECRET and config.CLOB_API_PASSPHRASE:
            log.info("Using API credentials from environment")
            creds = ApiCreds(
                api_key        = config.CLOB_API_KEY,
                api_secret     = config.CLOB_API_SECRET,
                api_passphrase = config.CLOB_API_PASSPHRASE,
            )
            self._client.set_api_creds(creds)
            return

        # Try to load from local file
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

        # Derive new credentials via wallet signature
        log.info("Deriving new API credentials (one-time setup)...")
        try:
            creds = self._client.create_or_derive_api_creds()
            # Save for next run
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def get_usdc_balance(self) -> float:
        """Return available USDC balance."""
        try:
            balance = self._client.get_balance()
            usdc = float(balance) / 1e6  # Polymarket uses 6-decimal USDC
            log.debug(f"USDC balance: ${usdc:.4f}")
            return usdc
        except Exception as e:
            log.error(f"Balance fetch failed: {e}")
            return 0.0

    # ── Price Queries ──────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def get_token_price(self, token_id: str) -> Optional[float]:
        """
        Get the best ask (buy) price for a YES token.
        This is what we'd pay to buy shares right now.
        """
        try:
            book = self._client.get_order_book(token_id)
            if not book or not book.asks:
                return None
            # Best ask = lowest price someone will sell at
            best_ask = float(book.asks[0].price)
            return best_ask
        except Exception as e:
            log.warning(f"Price fetch for {token_id[:20]}... failed: {e}")
            return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def get_token_bid(self, token_id: str) -> Optional[float]:
        """
        Get the best bid (sell) price for a YES token.
        This is what we'd get if we sell shares right now.
        """
        try:
            book = self._client.get_order_book(token_id)
            if not book or not book.bids:
                return None
            best_bid = float(book.bids[0].price)
            return best_bid
        except Exception as e:
            log.warning(f"Bid fetch for {token_id[:20]}... failed: {e}")
            return None

    def get_mid_price(self, token_id: str) -> Optional[float]:
        """Mid price = (best_ask + best_bid) / 2. Used for P&L display."""
        ask = self.get_token_price(token_id)
        bid = self.get_token_bid(token_id)
        if ask is None or bid is None:
            return ask or bid
        return round((ask + bid) / 2, 4)

    def get_orderbook_depth(self, token_id: str, price_level: float, side: str = "ask") -> float:
        """
        Return available liquidity at a given price level.
        Helps avoid partial fills on entry.
        """
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
        """
        Execute a MARKET BUY for `usdc_amount` USDC worth of YES tokens.
        Uses FOK (Fill or Kill) — if not filled immediately, cancelled.
        Returns order response dict or None.
        """
        try:
            log.info(f"MARKET BUY: ${usdc_amount} USDC on token {token_id[:20]}...")

            order = self._client.create_market_order(
                MarketOrderArgs(
                    token_id = token_id,
                    amount   = usdc_amount,
                    side     = BUY,
                )
            )
            resp = self._client.post_order(order, OrderType.FOK)

            if resp and resp.get("success"):
                log.info(f"✅ BUY filled: orderID={resp.get('orderID', 'n/a')}")
            else:
                log.warning(f"BUY not filled (FOK): {resp}")

            return resp
        except Exception as e:
            log.error(f"Market buy failed: {e}")
            return None

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=5))
    def limit_sell(self, token_id: str, price: float, shares: float) -> Optional[Dict]:
        """
        Place a LIMIT SELL at `price` for `shares` amount.
        Used for stop loss execution — GTC order.
        """
        try:
            log.info(f"LIMIT SELL: {shares:.4f} shares @ ${price} on {token_id[:20]}...")

            order_args = OrderArgs(
                token_id = token_id,
                price    = round(price, 4),
                size     = round(shares, 4),
                side     = SELL,
            )
            signed_order = self._client.create_order(order_args)
            resp = self._client.post_order(signed_order, OrderType.GTC)

            if resp and resp.get("success"):
                log.info(f"✅ SELL order placed: orderID={resp.get('orderID', 'n/a')}")
            else:
                log.warning(f"SELL order failed: {resp}")

            return resp
        except Exception as e:
            log.error(f"Limit sell failed: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID."""
        try:
            resp = self._client.cancel(order_id)
            log.info(f"Cancelled order {order_id}: {resp}")
            return True
        except Exception as e:
            log.error(f"Cancel order {order_id} failed: {e}")
            return False

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5))
    def get_order_status(self, order_id: str) -> Optional[Dict]:
        """Check if an order is filled, open, or cancelled."""
        try:
            return self._client.get_order(order_id)
        except Exception as e:
            log.warning(f"Order status check failed ({order_id}): {e}")
            return None

    def get_positions(self) -> list:
        """Get all open positions from Polymarket."""
        try:
            return self._client.get_positions() or []
        except Exception as e:
            log.error(f"Get positions failed: {e}")
            return []

    @property
    def address(self) -> str:
        return self._address


# ── Singleton ──────────────────────────────────────────────────────────────
poly_client = PolymarketClient()
