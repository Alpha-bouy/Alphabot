"""
config.py — Single source of truth for all bot parameters.
All values come from .env so nothing sensitive is hardcoded.
"""

import os
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:

    # ── Wallet / Polymarket ──────────────────────────────────────────────
    PRIVATE_KEY:         str = field(default_factory=lambda: os.getenv("PRIVATE_KEY", ""))
    CLOB_API_KEY:        str = field(default_factory=lambda: os.getenv("CLOB_API_KEY", ""))
    CLOB_API_SECRET:     str = field(default_factory=lambda: os.getenv("CLOB_API_SECRET", ""))
    CLOB_API_PASSPHRASE: str = field(default_factory=lambda: os.getenv("CLOB_API_PASSPHRASE", ""))

    # ── Telegram ─────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    TELEGRAM_CHAT_ID:   str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    # ── Cricket Data ─────────────────────────────────────────────────────
    CRICKET_DATA_API_KEY: str = field(default_factory=lambda: os.getenv("CRICKET_DATA_API_KEY", ""))

    # ── Strategy Parameters ───────────────────────────────────────────────
    TRADE_AMOUNT_USDC:       float = field(default_factory=lambda: float(os.getenv("TRADE_AMOUNT_USDC", "1.0")))
    BUY_THRESHOLD:           float = field(default_factory=lambda: float(os.getenv("BUY_THRESHOLD", "0.90")))
    STOP_LOSS_PRICE:         float = field(default_factory=lambda: float(os.getenv("STOP_LOSS_PRICE", "0.85")))
    MAX_CONCURRENT_POSITIONS: int  = field(default_factory=lambda: int(os.getenv("MAX_CONCURRENT_POSITIONS", "5")))
    MAX_TOTAL_EXPOSURE_USDC: float = field(default_factory=lambda: float(os.getenv("MAX_TOTAL_EXPOSURE_USDC", "5.0")))

    # ── Signal Engine Thresholds ──────────────────────────────────────────
    # Score must be >= this to enter a trade
    MIN_SIGNAL_SCORE_FOR_ENTRY: int = 70
    # If price is <= this, entry is allowed regardless (pure price play)
    PRICE_ONLY_ENTRY_THRESHOLD: float = 0.88

    # ── Stop Loss Aggression (if initial sell order doesn't fill) ─────────
    # After this many seconds, reduce sell price and retry
    STOP_LOSS_RETRY_TIMEOUT:  int   = 90
    STOP_LOSS_RETRY_STEP:     float = 0.02   # Drop 2c each retry
    STOP_LOSS_FLOOR:          float = 0.70   # Absolute minimum sell price

    # ── Polling Intervals (seconds) ───────────────────────────────────────
    MARKET_SCAN_INTERVAL:      int = field(default_factory=lambda: int(os.getenv("MARKET_SCAN_INTERVAL", "120")))
    POSITION_MONITOR_INTERVAL: int = field(default_factory=lambda: int(os.getenv("POSITION_MONITOR_INTERVAL", "20")))
    CRICKET_POLL_INTERVAL:     int = field(default_factory=lambda: int(os.getenv("CRICKET_POLL_INTERVAL", "15")))

    # ── Polymarket Endpoints ──────────────────────────────────────────────
    CLOB_HOST:    str = "https://clob.polymarket.com"
    GAMMA_API:    str = "https://gamma-api.polymarket.com"
    CHAIN_ID:     int = 137   # Polygon Mainnet

    # ── Health Server (for Render.com keepalive pings) ────────────────────
    HEALTH_PORT: int = field(default_factory=lambda: int(os.getenv("PORT", "8080")))

    # ── Allowed Teams ─────────────────────────────────────────────────────
    INTERNATIONAL_TEAMS: List[str] = field(default_factory=lambda: [
        "Afghanistan", "Australia", "Bangladesh", "England",
        "India", "Ireland", "New Zealand", "Pakistan",
        "South Africa", "Sri Lanka", "West Indies", "Zimbabwe",
    ])

    IPL_TEAMS: List[str] = field(default_factory=lambda: [
        "Mumbai Indians", "MI",
        "Chennai Super Kings", "CSK",
        "Royal Challengers Bangalore", "RCB",
        "Kolkata Knight Riders", "KKR",
        "Delhi Capitals", "DC",
        "Punjab Kings", "PBKS", "Kings XI Punjab",
        "Rajasthan Royals", "RR",
        "Sunrisers Hyderabad", "SRH",
        "Lucknow Super Giants", "LSG",
        "Gujarat Titans", "GT",
    ])

    IPL_KEYWORDS: List[str] = field(default_factory=lambda: [
        "IPL", "Indian Premier League",
    ])

    @property
    def ALL_ALLOWED_TEAMS(self) -> List[str]:
        return self.INTERNATIONAL_TEAMS + self.IPL_TEAMS

    def validate(self) -> None:
        """Called at startup — raises if critical env vars are missing."""
        missing = []
        if not self.PRIVATE_KEY or self.PRIVATE_KEY == "0xyour_private_key_here":
            missing.append("PRIVATE_KEY")
        if not self.TELEGRAM_BOT_TOKEN or "botfather" in self.TELEGRAM_BOT_TOKEN:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.TELEGRAM_CHAT_ID:
            missing.append("TELEGRAM_CHAT_ID")
        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}\n"
                f"Copy .env.example → .env and fill in the values."
            )


# ─── Singleton — import this everywhere ────────────────────────────────────
config = Config()
