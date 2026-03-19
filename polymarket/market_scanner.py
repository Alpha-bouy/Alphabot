"""
polymarket/market_scanner.py — Discovers active cricket markets on Polymarket.

Uses Gamma API (gamma-api.polymarket.com) to search for sport/cricket markets,
then filters by our team whitelist via match_filter.
"""

import requests
from typing import List, Dict, Optional
from cachetools import TTLCache
from tenacity import retry, stop_after_attempt, wait_exponential

from config import config
from cricket.match_filter import match_filter
from logger import get_logger

log = get_logger("polymarket.scanner")

# Cache market list for 5 minutes
_market_cache = TTLCache(maxsize=200, ttl=300)


class MarketScanner:
    """
    Periodically scans Polymarket for new cricket markets to trade.
    Filters by: sport=cricket, active=true, approved teams/tournaments.
    """

    GAMMA_MARKETS = f"{config.GAMMA_API}/markets"
    GAMMA_EVENTS  = f"{config.GAMMA_API}/events"

    HEADERS = {
        "User-Agent": "PolymarketCricketBot/1.0",
        "Accept": "application/json",
    }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15))
    def get_active_cricket_markets(self) -> List[Dict]:
        """
        Fetch all active cricket markets from Polymarket.
        Returns list of market dicts with: market_id, question, token_id,
        current_price, volume, etc.
        """
        try:
            params = {
                "tag":             "cricket",
                "active":          "true",
                "closed":          "false",
                "limit":           100,
                "order":           "volume",
                "ascending":       "false",
            }
            r = requests.get(self.GAMMA_MARKETS, params=params,
                             headers=self.HEADERS, timeout=15)
            r.raise_for_status()
            data = r.json()

            markets = data if isinstance(data, list) else data.get("data", [])
            log.debug(f"Gamma API returned {len(markets)} cricket markets")

            filtered = []
            for m in markets:
                parsed = self._parse_market(m)
                if parsed:
                    filtered.append(parsed)

            log.info(f"Scanner: {len(filtered)} valid cricket markets after filtering")
            return filtered

        except Exception as e:
            log.error(f"Market scan failed: {e}")
            return []

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15))
    def get_sports_markets_broad(self) -> List[Dict]:
        """
        Broader search: fetch sports markets and filter for cricket keywords.
        Fallback when tag=cricket returns nothing.
        """
        try:
            params = {
                "tag":    "sports",
                "active": "true",
                "closed": "false",
                "limit":  200,
            }
            r = requests.get(self.GAMMA_MARKETS, params=params,
                             headers=self.HEADERS, timeout=15)
            r.raise_for_status()
            data = r.json()
            markets = data if isinstance(data, list) else data.get("data", [])

            # Filter for cricket-related questions
            cricket_keywords = [
                "cricket", "ipl", "t20", "odi", "test match",
                "innings", "over", "wicket",
            ]
            cricket_markets = []
            for m in markets:
                q = (m.get("question") or "").lower()
                desc = (m.get("description") or "").lower()
                if any(kw in q or kw in desc for kw in cricket_keywords):
                    parsed = self._parse_market(m)
                    if parsed:
                        cricket_markets.append(parsed)

            log.info(f"Broad search: {len(cricket_markets)} cricket markets found")
            return cricket_markets

        except Exception as e:
            log.error(f"Broad market scan failed: {e}")
            return []

    def _parse_market(self, raw: Dict) -> Optional[Dict]:
        """
        Transform a raw Gamma API market response into our standard format.
        Returns None if market doesn't pass team/tournament filter.
        """
        question = raw.get("question") or raw.get("title") or ""
        if not question:
            return None

        # Apply whitelist filter
        is_valid, team, tournament = match_filter.is_valid_market(question)
        if not is_valid:
            return None

        # Extract token IDs for YES/NO
        tokens = raw.get("tokens") or raw.get("clob_token_ids") or []
        if isinstance(tokens, str):
            import json
            try:
                tokens = json.loads(tokens)
            except Exception:
                tokens = []

        # Polymarket YES token is always index 0
        yes_token_id = tokens[0] if tokens else ""
        no_token_id  = tokens[1] if len(tokens) > 1 else ""

        if not yes_token_id:
            log.debug(f"No token ID found for market: {question[:50]}")
            return None

        # Current price (probability)
        price = self._extract_price(raw, yes_token_id)

        # Volume (proxy for liquidity)
        volume = float(raw.get("volume") or raw.get("volume24hr") or 0)

        # Market ID
        market_id    = str(raw.get("id") or raw.get("market_id") or "")
        condition_id = str(raw.get("conditionId") or raw.get("condition_id") or "")

        # Skip very low-liquidity markets
        if volume < 50:
            log.debug(f"Skipped (low volume ${volume:.0f}): {question[:50]}")
            return None

        return {
            "market_id":    market_id,
            "condition_id": condition_id,
            "token_id":     yes_token_id,
            "no_token_id":  no_token_id,
            "question":     question,
            "team":         team,
            "tournament":   tournament,
            "price":        price,
            "volume":       volume,
            "active":       raw.get("active", True),
            "closed":       raw.get("closed", False),
            "end_date":     raw.get("end_date_iso") or raw.get("endDate", ""),
            "raw":          raw,   # Keep full raw for debugging
        }

    def _extract_price(self, raw: Dict, yes_token_id: str) -> float:
        """Extract YES token price from various possible API response formats."""
        # Direct price field
        if "outcome_prices" in raw:
            try:
                prices = raw["outcome_prices"]
                if isinstance(prices, str):
                    import json
                    prices = json.loads(prices)
                if prices:
                    return float(prices[0])
            except Exception:
                pass

        if "last_trade_price" in raw:
            try:
                return float(raw["last_trade_price"])
            except Exception:
                pass

        if "price" in raw:
            try:
                return float(raw["price"])
            except Exception:
                pass

        return 0.0

    def get_market_by_id(self, market_id: str) -> Optional[Dict]:
        """Fetch a single market's current state by ID."""
        try:
            r = requests.get(
                f"{self.GAMMA_MARKETS}/{market_id}",
                headers=self.HEADERS,
                timeout=10,
            )
            r.raise_for_status()
            raw = r.json()
            return self._parse_market(raw)
        except Exception as e:
            log.warning(f"Single market fetch failed ({market_id}): {e}")
            return None


# ── Singleton ──────────────────────────────────────────────────────────────
market_scanner = MarketScanner()
