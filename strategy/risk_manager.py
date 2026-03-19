"""
strategy/risk_manager.py — Pre-trade risk checks and circuit breaker.

Every potential trade must pass through here before execution.
Enforces: max concurrent positions, max total exposure, min liquidity,
circuit breaker on consecutive losses.
"""

from typing import Dict, Optional
from database import get_open_positions, get_pnl_summary
from config import config
from logger import get_logger

log = get_logger("strategy.risk")

# Circuit breaker: pause trading after N consecutive stop losses
CIRCUIT_BREAKER_THRESHOLD = 3
_consecutive_losses = 0
_circuit_broken = False


class RiskManager:

    def __init__(self):
        self.max_positions = config.MAX_CONCURRENT_POSITIONS
        self.max_exposure  = config.MAX_TOTAL_EXPOSURE_USDC
        self.trade_size    = config.TRADE_AMOUNT_USDC
        self.min_price_for_entry  = 0.80   # Never buy below 80c (too much downside)
        self.max_price_for_entry  = config.BUY_THRESHOLD  # 90c cap
        self.min_market_volume    = 100.0  # Min $100 volume for liquidity assurance

    def can_trade(
        self,
        market: Dict,
        usdc_balance: float,
        reason: str = "",
    ) -> tuple[bool, str]:
        """
        Master pre-trade gate. Returns (allowed: bool, reason: str).
        All checks must pass for a trade to proceed.
        """
        global _circuit_broken

        # ── 0. Circuit breaker ────────────────────────────────────────────
        if _circuit_broken:
            return False, "🚨 Circuit breaker active — too many consecutive losses. Use /resume in Telegram."

        # ── 1. Wallet balance ─────────────────────────────────────────────
        if usdc_balance < self.trade_size:
            return False, f"Insufficient balance: ${usdc_balance:.2f} < ${self.trade_size}"

        # ── 2. Concurrent position limit ──────────────────────────────────
        open_positions = get_open_positions()
        if len(open_positions) >= self.max_positions:
            return False, f"Max concurrent positions reached ({self.max_positions})"

        # ── 3. Total exposure cap ─────────────────────────────────────────
        current_exposure = sum(p["usdc_spent"] for p in open_positions)
        if current_exposure + self.trade_size > self.max_exposure:
            return False, (
                f"Exposure cap: ${current_exposure:.2f} + ${self.trade_size} "
                f"> ${self.max_exposure} limit"
            )

        # ── 4. Price range guard ──────────────────────────────────────────
        price = market.get("price", 0)
        if price < self.min_price_for_entry:
            return False, f"Price {price:.3f} below min entry {self.min_price_for_entry}"
        if price > self.max_price_for_entry:
            return False, f"Price {price:.3f} above buy threshold {self.max_price_for_entry}"

        # ── 5. Market volume / liquidity ──────────────────────────────────
        volume = market.get("volume", 0)
        if volume < self.min_market_volume:
            return False, f"Low volume (${volume:.0f}) — skip for liquidity risk"

        # ── 6. Duplicate position guard ───────────────────────────────────
        from database import position_exists_for_market
        if position_exists_for_market(market.get("market_id", "")):
            return False, "Already have an open position in this market"

        log.info(f"✅ Risk check passed: {market.get('question', '')[:50]}")
        return True, "ok"

    def check_stop_loss(self, position: Dict, current_price: float) -> bool:
        """
        Should we trigger stop loss on this position?
        Returns True if current price has hit or breached stop loss level.
        """
        return current_price <= config.STOP_LOSS_PRICE

    def on_stoploss_hit(self) -> None:
        """Called after every stop loss execution. Manages circuit breaker."""
        global _consecutive_losses, _circuit_broken
        _consecutive_losses += 1
        log.warning(f"Stop loss hit. Consecutive losses: {_consecutive_losses}")
        if _consecutive_losses >= CIRCUIT_BREAKER_THRESHOLD:
            _circuit_broken = True
            log.critical(
                f"🚨 CIRCUIT BREAKER TRIGGERED after {_consecutive_losses} consecutive losses! "
                "Trading paused. Use /resume in Telegram to restart."
            )

    def on_win(self) -> None:
        """Reset consecutive loss counter on any win."""
        global _consecutive_losses
        _consecutive_losses = 0
        log.info("Win recorded — consecutive loss counter reset")

    def reset_circuit_breaker(self) -> None:
        """Called from Telegram /resume command."""
        global _consecutive_losses, _circuit_broken
        _consecutive_losses = 0
        _circuit_broken = False
        log.info("Circuit breaker manually reset via Telegram")

    @property
    def is_circuit_broken(self) -> bool:
        return _circuit_broken

    @property
    def consecutive_losses(self) -> int:
        return _consecutive_losses


# ── Singleton ──────────────────────────────────────────────────────────────
risk_manager = RiskManager()
