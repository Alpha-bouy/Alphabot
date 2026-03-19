"""
strategy/entry_logic.py — Decides WHEN to buy.

Combines three signals to make entry decision:
  1. Polymarket price at or below BUY_THRESHOLD (0.90)
  2. Cricket signal engine score above minimum (70+)
  3. Risk manager pre-trade gate (exposure, liquidity, etc.)

The edge: our cricket data updates faster than Polymarket's price oracle.
We see the win probability rising BEFORE the market reflects it.
"""

import asyncio
from typing import Optional, Dict, Tuple
from datetime import datetime

from config import config
from database import (
    Position, PositionStatus, insert_position, update_position,
    log_trade, TradeLog
)
from cricket.signal_engine import SignalEngine, SignalResult
from cricket.api_client import CricketClient, LiveMatchData
from polymarket.client import poly_client
from strategy.risk_manager import risk_manager
from logger import get_logger

log = get_logger("strategy.entry")

signal_engine = SignalEngine()


class EntryLogic:

    def __init__(self, cricket_client: CricketClient, notifier=None):
        self.cricket = cricket_client
        self.notifier = notifier  # Telegram notifier (injected after init)

    async def evaluate_market(
        self,
        market: Dict,
        live_match: Optional[LiveMatchData] = None,
    ) -> Optional[int]:
        """
        Evaluate a Polymarket market for entry.

        Args:
            market:     Parsed market dict from MarketScanner
            live_match: Optional pre-fetched cricket match data

        Returns:
            position DB id if trade was entered, else None
        """
        question  = market.get("question", "")
        team      = market.get("team", "")
        token_id  = market.get("token_id", "")
        market_id = market.get("market_id", "")

        # ── Step 1: Get fresh price from CLOB ─────────────────────────────
        # Fresh price is more accurate than Gamma API's cached price
        current_price = poly_client.get_token_price(token_id)
        if current_price is None:
            log.debug(f"No price data for {question[:40]}")
            return None

        market["price"] = current_price  # Update with fresh price

        log.debug(
            f"Evaluating: {question[:50]} | Price={current_price:.4f} | Team={team}"
        )

        # ── Step 2: Quick price check (fast reject before API calls) ──────
        if current_price > config.BUY_THRESHOLD:
            log.debug(f"Price {current_price:.4f} > threshold {config.BUY_THRESHOLD} — skip")
            return None

        if current_price < 0.80:
            log.debug(f"Price {current_price:.4f} < 0.80 — too low, skip")
            return None

        # ── Step 3: Get cricket signal ────────────────────────────────────
        signal = await self._compute_signal(market, team, live_match)

        if signal is None:
            # No live match data — use price-only entry (stricter threshold)
            if current_price <= config.PRICE_ONLY_ENTRY_THRESHOLD:
                log.info(
                    f"Price-only entry: {question[:40]} @ {current_price:.4f} "
                    f"(no live match data)"
                )
                signal_score = 55  # Neutral score for price-only
            else:
                log.debug("No signal + price above price-only threshold — skip")
                return None
        else:
            signal_score = signal.signal_score
            if signal.recommendation == "NO_SIGNAL":
                log.debug(
                    f"Signal score {signal_score}/100 below threshold — skip "
                    f"({question[:40]})"
                )
                return None

        # ── Step 4: Risk manager gate ─────────────────────────────────────
        balance = poly_client.get_usdc_balance()
        allowed, reason = risk_manager.can_trade(market, balance)
        if not allowed:
            log.debug(f"Risk check failed: {reason}")
            return None

        # ── Step 5: Liquidity check ───────────────────────────────────────
        available_liquidity = poly_client.get_orderbook_depth(
            token_id, current_price, side="ask"
        )
        shares_needed = config.TRADE_AMOUNT_USDC / current_price
        if available_liquidity < shares_needed * 0.5:
            log.warning(
                f"Low liquidity at {current_price:.4f}: "
                f"need {shares_needed:.2f} shares, available={available_liquidity:.2f}"
            )
            # Still proceed — market order will find best available price

        # ── Step 6: Execute buy ───────────────────────────────────────────
        position_id = await self._execute_buy(
            market       = market,
            signal_score = signal_score,
            signal       = signal,
            entry_price  = current_price,
        )

        return position_id

    async def _compute_signal(
        self,
        market: Dict,
        team: str,
        live_match: Optional[LiveMatchData],
    ) -> Optional[SignalResult]:
        """Compute cricket signal, fetching data if not provided."""
        try:
            # Fetch live match if not provided
            if live_match is None:
                # Try to find the live match for this team
                all_live = self.cricket.get_live_matches()
                live_match = self._match_market_to_live(market, all_live)

            if live_match is None:
                return None

            if live_match.status != "live":
                return None

            # Fetch form and H2H
            form = self.cricket.get_team_form(team, live_match.format)
            opponent = (
                live_match.bowling_team
                if live_match.batting_team == team
                else live_match.batting_team
            )
            h2h = self.cricket.get_head_to_head(team, opponent)

            signal = signal_engine.compute(live_match, team, form, h2h)
            return signal

        except Exception as e:
            log.error(f"Signal computation error: {e}")
            return None

    def _match_market_to_live(
        self,
        market: Dict,
        live_matches: list,
    ) -> Optional[LiveMatchData]:
        """
        Try to correlate a Polymarket market question with a live ESPN match.
        Uses team name overlap heuristic.
        """
        team = (market.get("team") or "").lower()
        question = (market.get("question") or "").lower()

        for match_info in live_matches:
            t1 = match_info.get("team1", "").lower()
            t2 = match_info.get("team2", "").lower()

            if team in t1 or team in t2 or t1 in question or t2 in question:
                # Fetch full scorecard for this match
                match_id = match_info.get("match_id", "")
                if match_id:
                    return self.cricket.get_match_live_data(match_id)

        return None

    async def _execute_buy(
        self,
        market: Dict,
        signal_score: int,
        signal: Optional[SignalResult],
        entry_price: float,
    ) -> Optional[int]:
        """
        Place the actual buy order and persist the position.
        """
        question  = market.get("question", "")
        team      = market.get("team", "")
        token_id  = market.get("token_id", "")
        market_id = market.get("market_id", "")

        log.info(
            f"🟢 ENTERING TRADE: {question[:50]} | "
            f"Price={entry_price:.4f} | Signal={signal_score}/100 | "
            f"Amount=${config.TRADE_AMOUNT_USDC}"
        )

        # Place market buy order
        resp = poly_client.market_buy(token_id, config.TRADE_AMOUNT_USDC)

        if not resp or not resp.get("success"):
            log.error(f"Buy order failed for {question[:40]}: {resp}")
            return None

        order_id = resp.get("orderID", "")

        # Calculate shares received (approx)
        shares = round(config.TRADE_AMOUNT_USDC / entry_price, 4)

        # Save position to DB
        pos = Position(
            market_id    = market_id,
            condition_id = market.get("condition_id", ""),
            token_id     = token_id,
            question     = question,
            team_name    = team,
            tournament   = market.get("tournament", ""),
            buy_price    = entry_price,
            shares       = shares,
            usdc_spent   = config.TRADE_AMOUNT_USDC,
            buy_order_id = order_id,
            status       = PositionStatus.OPEN,
            signal_score = signal_score,
            last_price   = entry_price,
        )
        position_id = insert_position(pos)

        # Log the trade
        log_trade(TradeLog(
            position_id = position_id,
            action      = "BUY",
            price       = entry_price,
            shares      = shares,
            usdc_amount = config.TRADE_AMOUNT_USDC,
            order_id    = order_id,
            timestamp   = datetime.utcnow().isoformat(),
        ))

        # Notify via Telegram
        if self.notifier:
            msg = (
                f"🟢 *NEW POSITION*\n"
                f"Match: `{question}`\n"
                f"Team: *{team}*\n"
                f"Entry: `{entry_price:.4f}` USDC\n"
                f"Amount: `${config.TRADE_AMOUNT_USDC}` → `{shares:.2f}` shares\n"
                f"Signal: `{signal_score}/100`\n"
                f"Stop Loss: `{config.STOP_LOSS_PRICE:.2f}` (−5%)\n"
                f"ID: `#{position_id}`"
            )
            if signal:
                msg += f"\n\n{signal.as_telegram_str()}"
            await self.notifier.send(msg)

        log.info(f"✅ Position #{position_id} opened: {team} @ {entry_price:.4f}")
        return position_id


# ── Factory (singleton created in main.py after deps are ready) ────────────
_entry_logic: Optional[EntryLogic] = None

def get_entry_logic(cricket_client=None, notifier=None) -> EntryLogic:
    global _entry_logic
    if _entry_logic is None:
        _entry_logic = EntryLogic(cricket_client, notifier)
    return _entry_logic
