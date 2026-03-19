"""
main.py — Polymarket Cricket Trading Bot
=========================================
Entry point. Spins up three concurrent async loops:

  1. market_scan_loop     — Scans Polymarket for new cricket markets every 2 min
  2. position_monitor_loop — Monitors open positions for stop loss every 20s
  3. health_server        — Simple HTTP server for Render.com keepalive pings
  4. telegram_bot         — Telegram command handler (polling)

Run: python main.py
"""

import asyncio
import signal
import sys
import os
from aiohttp import web

from config import config
from database import init_db
from logger import get_logger
from cricket.api_client import CricketClient
from polymarket.client import poly_client
from polymarket.market_scanner import market_scanner
from strategy.entry_logic import EntryLogic
from strategy.exit_logic import get_exit_logic
from strategy.risk_manager import risk_manager
from telegram_bot.bot import TelegramBot, TelegramNotifier, trading_is_paused

log = get_logger("main")


# ═══════════════════════════════════════════════════════════════════════════
#  MARKET SCAN LOOP
# ═══════════════════════════════════════════════════════════════════════════

async def market_scan_loop(
    entry_logic: EntryLogic,
    cricket: CricketClient,
) -> None:
    """
    Every MARKET_SCAN_INTERVAL seconds:
      1. Fetch all active cricket markets from Polymarket
      2. Cross-reference with live ESPN matches
      3. Evaluate each market for entry (signal + price + risk)
    """
    log.info(f"Market scan loop started (every {config.MARKET_SCAN_INTERVAL}s)")

    while True:
        try:
            if trading_is_paused() or risk_manager.is_circuit_broken:
                log.info("Trading paused — skipping market scan")
                await asyncio.sleep(config.MARKET_SCAN_INTERVAL)
                continue

            log.info("🔍 Scanning Polymarket cricket markets...")

            # Get active cricket markets
            markets = market_scanner.get_active_cricket_markets()

            if not markets:
                # Fallback: broader sports search
                markets = market_scanner.get_sports_markets_broad()

            # Get all live matches for correlation
            live_matches = cricket.get_live_matches()
            log.info(
                f"Found {len(markets)} valid markets | "
                f"{len(live_matches)} live matches"
            )

            # Build match lookup by team name
            match_data_cache = {}
            for match_info in live_matches:
                match_id = match_info.get("match_id", "")
                if match_id:
                    live_data = cricket.get_match_live_data(match_id)
                    if live_data:
                        t1 = live_data.team1.lower()
                        t2 = live_data.team2.lower()
                        match_data_cache[t1] = live_data
                        match_data_cache[t2] = live_data

            # Evaluate each market
            for market in markets:
                try:
                    team = (market.get("team") or "").lower()
                    live_match = match_data_cache.get(team)

                    pos_id = await entry_logic.evaluate_market(
                        market     = market,
                        live_match = live_match,
                    )
                    if pos_id:
                        log.info(f"✅ New position #{pos_id} opened")

                    # Small delay between market evaluations
                    await asyncio.sleep(0.5)

                except Exception as e:
                    log.error(f"Error evaluating market {market.get('market_id')}: {e}")

        except Exception as e:
            log.error(f"Market scan loop error: {e}")

        await asyncio.sleep(config.MARKET_SCAN_INTERVAL)


# ═══════════════════════════════════════════════════════════════════════════
#  CRICKET DATA REFRESH LOOP
# ═══════════════════════════════════════════════════════════════════════════

async def cricket_refresh_loop(cricket: CricketClient) -> None:
    """
    Polls ESPN every CRICKET_POLL_INTERVAL (15s) during live matches.
    Updates are cached in CricketClient — entry/exit logic reads from cache.
    This loop ensures our cache stays fresh without hammering ESPN.
    """
    log.info(f"Cricket data refresh loop started (every {config.CRICKET_POLL_INTERVAL}s)")

    while True:
        try:
            # Refresh live match list
            live_matches = cricket.espn.get_live_matches()

            for match_info in live_matches:
                match_id = match_info.get("match_id", "")
                if match_id:
                    # This updates the TTL cache automatically
                    cricket.get_match_live_data(match_id)

        except Exception as e:
            log.error(f"Cricket refresh error: {e}")

        await asyncio.sleep(config.CRICKET_POLL_INTERVAL)


# ═══════════════════════════════════════════════════════════════════════════
#  HEALTH SERVER (Render.com keepalive)
# ═══════════════════════════════════════════════════════════════════════════

async def health_server() -> None:
    """
    Lightweight HTTP server for Render.com + UptimeRobot pings.
    GET /health → 200 OK (bot is alive)
    GET /        → 200 OK
    """
    from database import get_open_positions, get_pnl_summary
    import json as _json

    async def health(request):
        open_pos = len(get_open_positions())
        pnl = get_pnl_summary()
        body = _json.dumps({
            "status":        "ok",
            "open_positions": open_pos,
            "circuit_breaker": risk_manager.is_circuit_broken,
            "win_rate":      pnl["win_rate"],
            "realized_pnl":  pnl["realized_pnl"],
        })
        return web.Response(text=body, content_type="application/json")

    app = web.Application()
    app.router.add_get("/",       health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.HEALTH_PORT)
    await site.start()
    log.info(f"Health server running on port {config.HEALTH_PORT}")

    # Keep alive
    while True:
        await asyncio.sleep(60)


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

async def main() -> None:
    log.info("=" * 60)
    log.info("  POLYMARKET CRICKET BOT — Starting up")
    log.info("=" * 60)

    # ── Validate config ───────────────────────────────────────────────────
    try:
        config.validate()
    except EnvironmentError as e:
        log.critical(f"Config error:\n{e}")
        sys.exit(1)

    # ── Init DB ───────────────────────────────────────────────────────────
    init_db()
    log.info("Database initialized ✓")

    # ── Init Polymarket client ────────────────────────────────────────────
    try:
        poly_client.initialize()
        balance = poly_client.get_usdc_balance()
        log.info(f"Polymarket connected | Balance: ${balance:.4f} USDC")
    except Exception as e:
        log.critical(f"Polymarket init failed: {e}")
        sys.exit(1)

    # ── Init Cricket client ───────────────────────────────────────────────
    cricket = CricketClient(config.CRICKET_DATA_API_KEY)

    # ── Init Telegram ─────────────────────────────────────────────────────
    notifier = TelegramNotifier(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
    tg_bot   = TelegramBot(notifier)
    tg_bot.inject_poly_client(poly_client)
    tg_app   = tg_bot.build()

    # ── Wire up strategies ────────────────────────────────────────────────
    entry_logic = EntryLogic(cricket, notifier)
    exit_logic  = get_exit_logic(notifier)

    # ── Send startup notification ─────────────────────────────────────────
    await notifier.send_startup()

    # ── Launch all async tasks ────────────────────────────────────────────
    log.info("Launching bot loops...")

    tasks = [
        asyncio.create_task(health_server(),                           name="health"),
        asyncio.create_task(cricket_refresh_loop(cricket),             name="cricket_refresh"),
        asyncio.create_task(market_scan_loop(entry_logic, cricket),    name="market_scan"),
        asyncio.create_task(exit_logic.monitor_loop(),                 name="exit_monitor"),
        asyncio.create_task(tg_bot.run(),                              name="telegram"),
    ]

    log.info("✅ All systems GO. Bot is live.")

    # ── Graceful shutdown handler ─────────────────────────────────────────
    loop = asyncio.get_event_loop()

    def shutdown():
        log.info("Shutdown signal received...")
        for task in tasks:
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown)

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    finally:
        await tg_bot.stop()
        await notifier.send("⚠️ *Bot OFFLINE* — process ended")
        log.info("Bot shut down cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
