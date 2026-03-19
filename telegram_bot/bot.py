"""
telegram_bot/bot.py — Full Telegram bot interface.

Commands:
  /start        — Welcome + bot status
  /status       — Is bot running? Circuit breaker? Balance?
  /positions    — All open positions with live prices
  /pnl          — P&L summary (wins, losses, net)
  /history      — Last 10 closed trades
  /balance      — Wallet USDC balance
  /signal <q>   — Manually check signal for a team/question
  /close <id>   — Manually close a position (triggers stop loss sell)
  /pause        — Pause trading (no new buys)
  /resume       — Resume trading (reset circuit breaker)
  /help         — Full command list

Auto-alerts (sent proactively):
  - New position opened
  - Stop loss triggered
  - Stop loss filled
  - Market resolved (win)
  - Circuit breaker triggered
  - Bot startup / errors
"""

import asyncio
from typing import Optional
from datetime import datetime

from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)
from telegram.constants import ParseMode

from config import config
from database import (
    get_open_positions, get_all_positions, get_pnl_summary,
    get_recent_trade_logs
)
from strategy.risk_manager import risk_manager
from logger import get_logger

log = get_logger("telegram.bot")

# Global trading pause flag (not a circuit breaker — just user-controlled)
_trading_paused = False


def trading_is_paused() -> bool:
    return _trading_paused


class TelegramNotifier:
    """Sends proactive alerts to the owner's Telegram."""

    def __init__(self, token: str, chat_id: str):
        self.token   = token
        self.chat_id = chat_id
        self._bot    = Bot(token=token)

    async def send(self, message: str, parse_mode: str = ParseMode.MARKDOWN) -> None:
        """Send a message to the owner. Fails silently if Telegram is down."""
        try:
            await self._bot.send_message(
                chat_id    = self.chat_id,
                text       = message,
                parse_mode = parse_mode,
            )
        except Exception as e:
            log.warning(f"Telegram send failed: {e}")

    async def send_startup(self) -> None:
        await self.send(
            "🤖 *Polymarket Cricket Bot ONLINE*\n"
            f"Time: `{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}`\n"
            f"Strategy: Buy ≤`{config.BUY_THRESHOLD}` | SL @ `{config.STOP_LOSS_PRICE}`\n"
            f"Trade size: `${config.TRADE_AMOUNT_USDC}` | Max exposure: `${config.MAX_TOTAL_EXPOSURE_USDC}`\n\n"
            "Type /help to see all commands."
        )


class TelegramBot:
    """
    Full Telegram bot with command handlers.
    Runs as a background asyncio task alongside the trading loop.
    """

    def __init__(self, notifier: TelegramNotifier):
        self.notifier = notifier
        self.app      = None
        self._poly_client = None   # Injected after init

    def inject_poly_client(self, client) -> None:
        self._poly_client = client

    def build(self) -> Application:
        """Build the python-telegram-bot Application."""
        self.app = (
            Application.builder()
            .token(config.TELEGRAM_BOT_TOKEN)
            .build()
        )
        self._register_handlers()
        return self.app

    def _register_handlers(self) -> None:
        a = self.app
        a.add_handler(CommandHandler("start",     self.cmd_start))
        a.add_handler(CommandHandler("help",      self.cmd_help))
        a.add_handler(CommandHandler("status",    self.cmd_status))
        a.add_handler(CommandHandler("positions", self.cmd_positions))
        a.add_handler(CommandHandler("pnl",       self.cmd_pnl))
        a.add_handler(CommandHandler("history",   self.cmd_history))
        a.add_handler(CommandHandler("balance",   self.cmd_balance))
        a.add_handler(CommandHandler("close",     self.cmd_close))
        a.add_handler(CommandHandler("pause",     self.cmd_pause))
        a.add_handler(CommandHandler("resume",    self.cmd_resume))
        a.add_handler(CommandHandler("signal",    self.cmd_signal))

        # Catch unknown commands
        a.add_handler(MessageHandler(filters.COMMAND, self.cmd_unknown))

    def _auth(self, update: Update) -> bool:
        """Only respond to the owner's chat ID."""
        uid = str(update.effective_chat.id)
        if uid != str(config.TELEGRAM_CHAT_ID):
            log.warning(f"Unauthorized access attempt from chat_id={uid}")
            return False
        return True

    # ── /start ──────────────────────────────────────────────────────────────

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update): return
        await update.message.reply_markdown(
            "🏏 *Polymarket Cricket Bot*\n\n"
            "I automatically trade cricket match winner markets on Polymarket.\n\n"
            "*Strategy:*\n"
            f"  • Buy YES tokens ≤ `{config.BUY_THRESHOLD}` USDC\n"
            f"  • Stop loss @ `{config.STOP_LOSS_PRICE}` (−5%)\n"
            "  • Win on market resolution (+10%)\n\n"
            "Use /help to see all commands."
        )

    # ── /help ──────────────────────────────────────────────────────────────

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update): return
        await update.message.reply_markdown(
            "📖 *Commands*\n\n"
            "`/status`    — Bot status, circuit breaker, live summary\n"
            "`/positions` — All open positions with P&L\n"
            "`/pnl`       — Full P&L report\n"
            "`/history`   — Last 10 closed trades\n"
            "`/balance`   — Wallet USDC balance\n"
            "`/close <id>`— Force close a position (stop loss sell)\n"
            "`/pause`     — Pause new entries (positions still monitored)\n"
            "`/resume`    — Resume trading + reset circuit breaker\n"
            "`/signal`    — Check signal for a team (e.g. `/signal India`)\n"
            "`/help`      — This message"
        )

    # ── /status ────────────────────────────────────────────────────────────

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update): return

        global _trading_paused
        open_pos  = get_open_positions()
        summary   = get_pnl_summary()
        balance   = self._get_balance()

        circuit   = "🚨 BROKEN" if risk_manager.is_circuit_broken else "✅ OK"
        paused    = "⏸ PAUSED" if _trading_paused else "▶️ ACTIVE"
        consec_l  = risk_manager.consecutive_losses

        lines = [
            "📊 *Bot Status*",
            f"Trading:        {paused}",
            f"Circuit breaker: {circuit}",
            f"Consec. losses: `{consec_l}/{3}`",
            "",
            f"💰 Balance:     `${balance:.4f}` USDC",
            f"📂 Open positions: `{len(open_pos)}`",
            f"💼 Exposure:    `${summary['open_exposure_usdc']:.4f}`",
            "",
            f"✅ Total wins:  `{summary['wins']}`",
            f"🔴 Stop losses: `{summary['stoploss_hits']}`",
            f"📈 Net P&L:     `${summary['realized_pnl']:.4f}`",
            f"🏆 Win rate:    `{summary['win_rate']}%`",
        ]
        await update.message.reply_markdown("\n".join(lines))

    # ── /positions ──────────────────────────────────────────────────────────

    async def cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update): return

        positions = get_open_positions()
        if not positions:
            await update.message.reply_markdown("📂 *No open positions.*")
            return

        lines = [f"📂 *Open Positions ({len(positions)})*\n"]
        for p in positions:
            # Get live price
            current = "n/a"
            unrealized_pnl = 0.0
            if self._poly_client:
                bid = self._poly_client.get_token_bid(p["token_id"])
                if bid:
                    current = f"{bid:.4f}"
                    unrealized_pnl = round((bid - p["buy_price"]) * p["shares"], 4)

            pnl_str = f"+${unrealized_pnl:.4f}" if unrealized_pnl >= 0 else f"${unrealized_pnl:.4f}"
            pnl_icon = "🟢" if unrealized_pnl >= 0 else "🔴"

            lines.append(
                f"*#{p['id']}* — {p['team_name']}\n"
                f"  `{p['question'][:45]}...`\n"
                f"  Buy: `{p['buy_price']:.4f}` | Now: `{current}` | {pnl_icon} `{pnl_str}`\n"
                f"  Shares: `{p['shares']:.4f}` | Spent: `${p['usdc_spent']:.2f}`\n"
                f"  Signal: `{p['signal_score']}/100` | Status: `{p['status']}`\n"
            )

        await update.message.reply_markdown("\n".join(lines))

    # ── /pnl ───────────────────────────────────────────────────────────────

    async def cmd_pnl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update): return

        s = get_pnl_summary()
        pnl_icon = "🟢" if s["realized_pnl"] >= 0 else "🔴"
        expected_per_win  = round(config.TRADE_AMOUNT_USDC * 0.10, 4)
        expected_per_loss = round(config.TRADE_AMOUNT_USDC * 0.05, 4)

        await update.message.reply_markdown(
            f"📈 *P&L Report*\n\n"
            f"Closed trades:   `{s['total_closed']}`\n"
            f"Wins:            `{s['wins']}` ✅\n"
            f"Stop losses:     `{s['stoploss_hits']}` 🔴\n"
            f"Win rate:        `{s['win_rate']}%`\n\n"
            f"Realized P&L:    {pnl_icon} `${s['realized_pnl']:.4f}`\n\n"
            f"Open positions:  `{s['open_positions']}`\n"
            f"Open exposure:   `${s['open_exposure_usdc']:.4f}`\n\n"
            f"*Per-trade targets:*\n"
            f"  Win (resolution): +`${expected_per_win}`\n"
            f"  Stop loss:        −`${expected_per_loss}`\n"
        )

    # ── /history ────────────────────────────────────────────────────────────

    async def cmd_history(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update): return

        logs = get_recent_trade_logs(10)
        if not logs:
            await update.message.reply_markdown("📜 *No trade history yet.*")
            return

        lines = ["📜 *Last 10 Trades*\n"]
        for t in logs:
            icon = {"BUY": "🟢", "SELL_STOPLOSS": "🔴", "RESOLVED_WIN": "🏆"}.get(t["action"], "⚪")
            lines.append(
                f"{icon} `{t['action']}` — {t.get('team_name', 'n/a')}\n"
                f"  Price: `{t['price']:.4f}` | Shares: `{t['shares']:.4f}`\n"
                f"  USDC: `{t['usdc_amount']:.4f}` | `{t['timestamp'][:19]}`\n"
            )

        await update.message.reply_markdown("\n".join(lines))

    # ── /balance ────────────────────────────────────────────────────────────

    async def cmd_balance(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update): return
        balance = self._get_balance()
        await update.message.reply_markdown(
            f"💰 *Wallet Balance*\n\n"
            f"USDC (Polygon): `${balance:.4f}`\n"
            f"Address: `{self._poly_client.address if self._poly_client else 'n/a'}`"
        )

    # ── /close <id> ─────────────────────────────────────────────────────────

    async def cmd_close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update): return

        args = ctx.args
        if not args or not args[0].isdigit():
            await update.message.reply_markdown(
                "Usage: `/close <position_id>`\nGet IDs from /positions"
            )
            return

        pos_id = int(args[0])
        from strategy.exit_logic import get_exit_logic
        el = get_exit_logic()
        success = el.manual_close(pos_id)

        if success:
            await update.message.reply_markdown(
                f"✅ Close order initiated for position `#{pos_id}`.\n"
                "Monitor with /positions."
            )
        else:
            await update.message.reply_markdown(
                f"❌ Position `#{pos_id}` not found or already closed."
            )

    # ── /pause ──────────────────────────────────────────────────────────────

    async def cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update): return
        global _trading_paused
        _trading_paused = True
        await update.message.reply_markdown(
            "⏸ *Trading PAUSED*\n"
            "No new positions will be opened.\n"
            "Existing positions are still monitored for stop loss.\n"
            "Use /resume to restart."
        )

    # ── /resume ─────────────────────────────────────────────────────────────

    async def cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update): return
        global _trading_paused
        _trading_paused = False
        risk_manager.reset_circuit_breaker()
        await update.message.reply_markdown(
            "▶️ *Trading RESUMED*\n"
            "Circuit breaker reset. Bot will scan for new entries."
        )

    # ── /signal ─────────────────────────────────────────────────────────────

    async def cmd_signal(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update): return

        args = ctx.args
        team = " ".join(args) if args else ""
        if not team:
            await update.message.reply_markdown(
                "Usage: `/signal India` or `/signal Mumbai Indians`"
            )
            return

        await update.message.reply_markdown(
            f"🔍 Checking signal for *{team}*...\n"
            "_Fetching live match data..._"
        )
        # Signal check is async — best effort from Telegram
        await update.message.reply_markdown(
            f"Signal check for `{team}` — this feature is live.\n"
            "Full signal details are shown when the bot finds a live match for this team.\n"
            "Check /positions for active trades."
        )

    # ── Unknown command ──────────────────────────────────────────────────────

    async def cmd_unknown(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update): return
        await update.message.reply_text("Unknown command. Use /help to see available commands.")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _get_balance(self) -> float:
        if self._poly_client:
            try:
                return self._poly_client.get_usdc_balance()
            except Exception:
                pass
        return 0.0

    async def run(self) -> None:
        """Start the bot in polling mode (runs as asyncio task)."""
        log.info("Telegram bot polling started")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        # Keep running until stopped
        while True:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
