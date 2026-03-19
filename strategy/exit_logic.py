"""
strategy/exit_logic.py — Monitors open positions and executes stop losses.

Key behaviors:
  - Poll every POSITION_MONITOR_INTERVAL seconds (default: 20s)
  - If current bid price <= STOP_LOSS_PRICE (0.85): place limit sell immediately
  - If limit sell doesn't fill in STOP_LOSS_RETRY_TIMEOUT seconds: drop price
  - Retry until STOP_LOSS_FLOOR or filled
  - On market resolution: detect and record win
"""

import asyncio
import time
from typing import Dict, List, Optional
from datetime import datetime, timedelta

from config import config
from database import (
    get_open_positions, update_position, log_trade,
    TradeLog, PositionStatus
)
from polymarket.client import poly_client
from strategy.risk_manager import risk_manager
from logger import get_logger

log = get_logger("strategy.exit")

# Track stop loss sell order attempts per position
# {position_id: {"order_id": str, "placed_at": float, "current_sl_price": float}}
_sl_orders: Dict[int, Dict] = {}


class ExitLogic:

    def __init__(self, notifier=None):
        self.notifier = notifier

    async def monitor_loop(self) -> None:
        """
        Main monitoring coroutine. Runs forever, polls every N seconds.
        Called from main.py as an asyncio task.
        """
        log.info(f"Exit monitor started (interval={config.POSITION_MONITOR_INTERVAL}s)")
        while True:
            try:
                await self._check_all_positions()
            except Exception as e:
                log.error(f"Exit monitor error: {e}")
            await asyncio.sleep(config.POSITION_MONITOR_INTERVAL)

    async def _check_all_positions(self) -> None:
        """Check every open/closing position for stop loss or resolution."""
        positions = get_open_positions()
        if not positions:
            return

        log.debug(f"Monitoring {len(positions)} open position(s)")

        for pos in positions:
            try:
                await self._check_position(pos)
            except Exception as e:
                log.error(f"Error checking position #{pos['id']}: {e}")

    async def _check_position(self, pos: Dict) -> None:
        """Evaluate a single position for exit conditions."""
        pos_id   = pos["id"]
        token_id = pos["token_id"]
        status   = pos["status"]

        # ── Case 1: Pending sell order — check if filled ──────────────────
        if status == PositionStatus.CLOSING:
            await self._check_sl_order_fill(pos)
            return

        # ── Case 2: Open position — get current price ─────────────────────
        # Use bid price for exit (what we can actually sell at)
        current_bid = poly_client.get_token_bid(token_id)

        if current_bid is None:
            log.warning(f"No bid price for position #{pos_id} — could be resolved")
            await self._check_market_resolution(pos)
            return

        # Update last seen price in DB
        update_position(pos_id, last_price=current_bid)

        log.debug(
            f"Position #{pos_id} ({pos['team_name']}): "
            f"bid={current_bid:.4f} | sl={config.STOP_LOSS_PRICE:.4f} | "
            f"buy_price={pos['buy_price']:.4f}"
        )

        # ── Check if market was resolved at 1.0 (win) ─────────────────────
        if current_bid >= 0.99:
            await self._handle_win(pos, current_bid)
            return

        # ── Check stop loss trigger ───────────────────────────────────────
        if risk_manager.check_stop_loss(pos, current_bid):
            log.warning(
                f"🔴 STOP LOSS TRIGGERED: Position #{pos_id} | "
                f"Current={current_bid:.4f} <= SL={config.STOP_LOSS_PRICE:.4f}"
            )
            await self._execute_stop_loss(pos, current_bid)

    async def _execute_stop_loss(self, pos: Dict, current_bid: float) -> None:
        """Place a limit sell order at stop loss price."""
        pos_id   = pos["id"]
        token_id = pos["token_id"]
        shares   = pos["shares"]
        sl_price = config.STOP_LOSS_PRICE

        # Don't double-place if already in _sl_orders
        if pos_id in _sl_orders:
            return

        resp = poly_client.limit_sell(token_id, sl_price, shares)

        if resp and resp.get("success"):
            order_id = resp.get("orderID", "")
            _sl_orders[pos_id] = {
                "order_id":       order_id,
                "placed_at":      time.time(),
                "current_sl_price": sl_price,
                "retry_count":    0,
            }
            update_position(pos_id, status=PositionStatus.CLOSING, sell_order_id=order_id)
            log.info(f"Stop loss sell placed for #{pos_id} @ {sl_price} | orderID={order_id}")

            if self.notifier:
                await self.notifier.send(
                    f"🔴 *STOP LOSS TRIGGERED*\n"
                    f"Match: `{pos['question']}`\n"
                    f"Team: *{pos['team_name']}*\n"
                    f"Trigger price: `{current_bid:.4f}`\n"
                    f"Sell order: `{sl_price:.4f}` (−5%)\n"
                    f"Shares: `{shares:.4f}`\n"
                    f"ID: `#{pos_id}`"
                )
        else:
            log.error(f"Failed to place stop loss for #{pos_id}: {resp}")

    async def _check_sl_order_fill(self, pos: Dict) -> None:
        """
        Check if a pending stop loss sell order was filled.
        If not filled within timeout, become more aggressive (lower price).
        """
        pos_id = pos["id"]
        sl_info = _sl_orders.get(pos_id)
        if not sl_info:
            # Inconsistent state — re-check
            update_position(pos_id, status=PositionStatus.OPEN)
            return

        order_id    = sl_info["order_id"]
        placed_at   = sl_info["placed_at"]
        sl_price    = sl_info["current_sl_price"]
        retry_count = sl_info["retry_count"]

        # Check fill status
        order_status = poly_client.get_order_status(order_id)
        if order_status:
            filled    = float(order_status.get("size_matched", 0))
            total     = float(order_status.get("original_size", pos["shares"]))
            o_status  = order_status.get("status", "").lower()

            if o_status in ("matched", "filled") or filled >= total * 0.95:
                # ✅ Stop loss executed
                fill_price = float(order_status.get("price", sl_price))
                await self._close_position_stoploss(pos, fill_price, filled)
                return

        # ── Timeout — become more aggressive ─────────────────────────────
        elapsed = time.time() - placed_at
        if elapsed >= config.STOP_LOSS_RETRY_TIMEOUT:
            new_sl = round(sl_price - config.STOP_LOSS_RETRY_STEP, 4)

            if new_sl < config.STOP_LOSS_FLOOR:
                new_sl = config.STOP_LOSS_FLOOR
                log.warning(
                    f"Stop loss at floor ({config.STOP_LOSS_FLOOR}) for #{pos_id} — "
                    "waiting for any fill"
                )

            # Cancel old order
            if order_id:
                poly_client.cancel_order(order_id)

            # Place new order at lower price
            log.warning(
                f"SL retry #{retry_count + 1} for position #{pos_id}: "
                f"{sl_price:.4f} → {new_sl:.4f}"
            )
            new_resp = poly_client.limit_sell(pos["token_id"], new_sl, pos["shares"])

            if new_resp and new_resp.get("success"):
                _sl_orders[pos_id] = {
                    "order_id":         new_resp.get("orderID", ""),
                    "placed_at":        time.time(),
                    "current_sl_price": new_sl,
                    "retry_count":      retry_count + 1,
                }
                update_position(pos_id, sell_order_id=new_resp.get("orderID", ""))

    async def _check_market_resolution(self, pos: Dict) -> None:
        """Check if market resolved (price disappears from book = settled)."""
        from polymarket.market_scanner import market_scanner
        market = market_scanner.get_market_by_id(pos["market_id"])

        if market and market.get("closed"):
            # Market closed — check outcome
            final_price = market.get("price", 0.0)
            if final_price >= 0.99:
                await self._handle_win(pos, 1.0)
            elif final_price <= 0.01:
                # We lost — record loss (no USDC recovered)
                pnl = -pos["usdc_spent"]
                update_position(
                    pos["id"],
                    status    = PositionStatus.CLOSED_STOPLOSS,
                    pnl_usdc  = pnl,
                    closed_at = datetime.utcnow().isoformat(),
                )
                risk_manager.on_stoploss_hit()
                log.warning(f"Market #{pos['market_id']} resolved AGAINST us. Loss: ${pnl:.4f}")

    async def _handle_win(self, pos: Dict, resolved_price: float) -> None:
        """Market resolved in our favor (price = 1.0)."""
        pos_id = pos["id"]

        # P&L: we get 1.0 per share, we paid buy_price per share
        pnl = round((1.0 - pos["buy_price"]) * pos["shares"], 4)

        update_position(
            pos_id,
            status    = PositionStatus.CLOSED_WIN,
            pnl_usdc  = pnl,
            last_price= resolved_price,
            closed_at = datetime.utcnow().isoformat(),
            notes     = "Market resolved — win",
        )

        # Clean up any dangling sl order
        _sl_orders.pop(pos_id, None)
        risk_manager.on_win()

        log_trade(TradeLog(
            position_id = pos_id,
            action      = "RESOLVED_WIN",
            price       = 1.0,
            shares      = pos["shares"],
            usdc_amount = pos["shares"],   # got 1.0 per share
            order_id    = "",
            timestamp   = datetime.utcnow().isoformat(),
        ))

        log.info(
            f"🏆 WIN: Position #{pos_id} ({pos['team_name']}) | PnL=+${pnl:.4f}"
        )

        if self.notifier:
            await self.notifier.send(
                f"🏆 *WIN — MARKET RESOLVED!*\n"
                f"Match: `{pos['question']}`\n"
                f"Team: *{pos['team_name']}*\n"
                f"Entry: `{pos['buy_price']:.4f}` → Resolved: `1.0000`\n"
                f"P&L: *+${pnl:.4f}* 🟢\n"
                f"ID: `#{pos_id}`"
            )

    async def _close_position_stoploss(
        self, pos: Dict, fill_price: float, shares_sold: float
    ) -> None:
        """Record a completed stop loss sale."""
        pos_id = pos["id"]

        proceeds = fill_price * shares_sold
        pnl      = round(proceeds - pos["usdc_spent"], 4)

        update_position(
            pos_id,
            status    = PositionStatus.CLOSED_STOPLOSS,
            pnl_usdc  = pnl,
            last_price= fill_price,
            closed_at = datetime.utcnow().isoformat(),
            notes     = f"Stop loss filled @ {fill_price:.4f}",
        )

        _sl_orders.pop(pos_id, None)
        risk_manager.on_stoploss_hit()

        log_trade(TradeLog(
            position_id = pos_id,
            action      = "SELL_STOPLOSS",
            price       = fill_price,
            shares      = shares_sold,
            usdc_amount = proceeds,
            order_id    = pos.get("sell_order_id", ""),
            timestamp   = datetime.utcnow().isoformat(),
        ))

        log.warning(
            f"🔴 STOP LOSS CLOSED: Position #{pos_id} ({pos['team_name']}) | "
            f"Fill={fill_price:.4f} | PnL={pnl:.4f}"
        )

        if self.notifier:
            await self.notifier.send(
                f"🔴 *STOP LOSS CLOSED*\n"
                f"Match: `{pos['question']}`\n"
                f"Team: *{pos['team_name']}*\n"
                f"Fill: `{fill_price:.4f}` | Shares: `{shares_sold:.4f}`\n"
                f"P&L: *${pnl:.4f}* 🔴\n"
                f"ID: `#{pos_id}`"
            )

    def manual_close(self, position_id: int) -> bool:
        """Called from Telegram /close command."""
        positions = get_open_positions()
        pos = next((p for p in positions if p["id"] == position_id), None)
        if not pos:
            return False

        # Fire and forget — async from sync context
        import asyncio
        asyncio.create_task(self._execute_stop_loss(pos, pos.get("last_price", 0.85)))
        return True


# ── Singleton ──────────────────────────────────────────────────────────────
exit_logic: Optional[ExitLogic] = None

def get_exit_logic(notifier=None) -> ExitLogic:
    global exit_logic
    if exit_logic is None:
        exit_logic = ExitLogic(notifier)
    return exit_logic
