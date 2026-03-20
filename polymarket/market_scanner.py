"""
polymarket/market_scanner.py — Discovers active cricket markets on Polymarket.
Fixed: better Gamma API queries + broader keyword matching.
"""

import requests
import json
from typing import List, Dict, Optional
from cachetools import TTLCache
from tenacity import retry, stop_after_attempt, wait_exponential

from config import config
from cricket.match_filter import match_filter
from logger import get_logger

log = get_logger("polymarket.scanner")

_market_cache = TTLCache(maxsize=200, ttl=300)


class MarketScanner:

    GAMMA_MARKETS = f"{config.GAMMA_API}/markets"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (compatible; PolymarketBot/1.0)",
        "Accept": "application/json",
    }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15))
    def get_active_cricket_markets(self) -> List[Dict]:
        """Fetch active cricket markets. Tries multiple query strategies."""
        all_markets = []

        # Strategy 1: tag_slug = cricket
        markets = self._fetch_with_params({"tag_slug": "cricket", "active": "true", "closed": "false", "limit": 100})
        all_markets.extend(markets)
        log.info(f"Strategy 1 (tag_slug=cricket): {len(markets)} markets")

        # Strategy 2: Sports category keyword search
        if len(all_markets) == 0:
            markets = self._fetch_with_params({"active": "true", "closed": "false", "limit": 200, "_c": "sports"})
            cricket_filtered = [m for m in markets if self._is_cricket(m)]
            all_markets.extend(cricket_filtered)
            log.info(f"Strategy 2 (sports+filter): {len(cricket_filtered)} cricket markets")

        # Deduplicate
        seen = set()
        unique = []
        for m in all_markets:
            mid = m.get("market_id", "") or m.get("id", "")
            if mid and mid not in seen:
                seen.add(mid)
                parsed = self._parse_market(m) if "market_id" not in m else m
                if parsed:
                    unique.append(parsed)

        log.info(f"Total valid cricket markets: {len(unique)}")
        return unique

    def _fetch_with_params(self, params: Dict) -> List[Dict]:
        try:
            r = requests.get(self.GAMMA_MARKETS, params=params, headers=self.HEADERS, timeout=15)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else data.get("data", data.get("markets", []))
        except Exception as e:
            log.warning(f"Gamma API fetch failed ({params}): {e}")
            return []

    def _is_cricket(self, m: Dict) -> bool:
        text = (
            (m.get("question") or "") + " " +
            (m.get("description") or "") + " " +
            (m.get("title") or "")
        ).lower()
        cricket_kw = ["cricket", "ipl", "t20", "odi", "test match", "wicket", "innings"]
        return any(kw in text for kw in cricket_kw)

    def get_sports_markets_broad(self) -> List[Dict]:
        """Broad fallback: fetch all sports and filter for cricket."""
        try:
            all_results = []
            # Search by cricket team names directly
            search_terms = ["India win", "Australia win", "Pakistan win", "England win", "IPL"]
            for term in search_terms:
                params = {"active": "true", "closed": "false", "limit": 50, "q": term}
                markets = self._fetch_with_params(params)
                for m in markets:
                    parsed = self._parse_market(m)
                    if parsed:
                        all_results.append(parsed)

            # Deduplicate
            seen = set()
            unique = []
            for m in all_results:
                mid = m.get("market_id", "")
                if mid and mid not in seen:
                    seen.add(mid)
                    unique.append(m)

            log.info(f"Broad search: {len(unique)} cricket markets")
            return unique

        except Exception as e:
            log.error(f"Broad scan failed: {e}")
            return []

    def _parse_market(self, raw: Dict) -> Optional[Dict]:
        question = raw.get("question") or raw.get("title") or ""
        if not question:
            return None

        is_valid, team, tournament = match_filter.is_valid_market(question)
        if not is_valid:
            return None

        # Extract tokens
        tokens = raw.get("tokens") or raw.get("clob_token_ids") or []
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except Exception:
                tokens = []

        yes_token_id = tokens[0] if tokens else ""
        no_token_id  = tokens[1] if len(tokens) > 1 else ""

        if not yes_token_id:
            return None

        price  = self._extract_price(raw)
        volume = float(raw.get("volume") or raw.get("volume24hr") or 0)

        if volume < 10:  # Lowered threshold for testing
            return None

        return {
            "market_id":    str(raw.get("id") or raw.get("market_id") or ""),
            "condition_id": str(raw.get("conditionId") or raw.get("condition_id") or ""),
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
        }

    def _extract_price(self, raw: Dict) -> float:
        for field in ["outcome_prices", "last_trade_price", "price", "bestAsk"]:
            val = raw.get(field)
            if val is not None:
                try:
                    if isinstance(val, str) and val.startswith("["):
                        val = json.loads(val)
                    if isinstance(val, list):
                        return float(val[0])
                    return float(val)
                except Exception:
                    continue
        return 0.0

    def get_market_by_id(self, market_id: str) -> Optional[Dict]:
        try:
            r = requests.get(f"{self.GAMMA_MARKETS}/{market_id}", headers=self.HEADERS, timeout=10)
            r.raise_for_status()
            return self._parse_market(r.json())
        except Exception as e:
            log.warning(f"Single market fetch failed: {e}")
            return None


market_scanner = MarketScanner()
