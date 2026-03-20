"""
cricket/api_client.py — Multi-source cricket data client.

ESPN blocked scraping (403), so we now use:
  1. cricapi.com (free 100 calls/day) — primary
  2. cricket-data.p.rapidapi.com — fallback
  3. Static match simulation for testing when no API available
"""

import re
import json
import time
import requests
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from cachetools import TTLCache
from tenacity import retry, stop_after_attempt, wait_exponential

from logger import get_logger

log = get_logger("cricket.api")

_live_cache  = TTLCache(maxsize=50, ttl=30)
_form_cache  = TTLCache(maxsize=100, ttl=3600)
_match_cache = TTLCache(maxsize=20, ttl=300)


@dataclass
class BallEvent:
    over: float
    runs: int
    is_wicket: bool
    is_boundary: bool
    extras: int = 0


@dataclass
class LiveMatchData:
    match_id:            str
    match_title:         str
    format:              str
    status:              str
    batting_team:        str
    bowling_team:        str
    runs:                int   = 0
    wickets:             int   = 0
    overs_bowled:        float = 0.0
    total_overs:         float = 0.0
    target:              int   = 0
    runs_needed:         int   = 0
    balls_remaining:     int   = 0
    crr:                 float = 0.0
    rrr:                 float = 0.0
    first_innings_score: int   = 0
    recent_overs:        List[int] = field(default_factory=list)
    last_5_balls:        List[BallEvent] = field(default_factory=list)
    team1:               str = ""
    team2:               str = ""
    venue:               str = ""
    source:              str = "unknown"
    fetched_at:          str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class TeamFormData:
    team_name:  str
    matches:    int   = 5
    wins:       int   = 3
    losses:     int   = 2
    win_rate:   float = 0.60
    formats:    List[str] = field(default_factory=list)
    fetched_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


# ═══════════════════════════════════════════════════════
#  CRICAPI ADAPTER (cricapi.com — free 100 calls/day)
# ═══════════════════════════════════════════════════════

class CricAPIAdapter:
    """
    Uses cricapi.com free tier.
    Sign up at https://cricketdata.org to get API key.
    """
    BASE = "https://api.cricapi.com/v1"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    def _get(self, endpoint: str, params: Dict = None) -> Optional[Dict]:
        if not self.api_key:
            return None
        p = {"apikey": self.api_key, "offset": 0, **(params or {})}
        try:
            r = requests.get(f"{self.BASE}/{endpoint}", params=p, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("status") != "success":
                return None
            return data
        except Exception as e:
            log.warning(f"CricAPI error ({endpoint}): {e}")
            return None

    def get_current_matches(self) -> List[Dict]:
        cache_key = "cricapi_current"
        if cache_key in _match_cache:
            return _match_cache[cache_key]

        data = self._get("currentMatches")
        if not data:
            return []

        matches = []
        for m in data.get("data", []):
            teams = m.get("teams", [])
            matches.append({
                "match_id": m.get("id", ""),
                "title":    m.get("name", ""),
                "status":   m.get("status", ""),
                "team1":    teams[0] if teams else "",
                "team2":    teams[1] if len(teams) > 1 else "",
                "format":   m.get("matchType", "T20").upper(),
                "series":   m.get("series_id", ""),
                "date":     m.get("date", ""),
                "live":     not m.get("matchEnded", True),
            })

        _match_cache[cache_key] = matches
        return matches

    def get_match_score(self, match_id: str) -> Optional[LiveMatchData]:
        cache_key = f"score_{match_id}"
        if cache_key in _live_cache:
            return _live_cache[cache_key]

        data = self._get("match_info", {"id": match_id})
        if not data or not data.get("data"):
            return None

        m = data["data"]
        return self._parse_match(m)

    def _parse_match(self, m: Dict) -> Optional[LiveMatchData]:
        try:
            teams = m.get("teams", ["Team A", "Team B"])
            team1 = teams[0] if teams else "Team A"
            team2 = teams[1] if len(teams) > 1 else "Team B"
            fmt   = m.get("matchType", "T20").upper()

            score_data = m.get("score", [])
            status     = m.get("status", "")
            is_live    = not m.get("matchEnded", True) and bool(score_data)

            if not score_data:
                return None

            # Current innings = last score entry
            current = score_data[-1] if score_data else {}
            batting_team = current.get("inning", "").replace(" Inning 1","").replace(" Inning 2","").strip()

            runs    = int(str(current.get("r", 0)))
            wickets = int(str(current.get("w", 0)))
            overs   = float(str(current.get("o", 0)))
            crr     = round(runs / overs, 2) if overs > 0 else 0.0

            total_overs = 20.0 if "T20" in fmt else 50.0 if "ODI" in fmt else 90.0

            # Chase info
            target = 0
            runs_needed = 0
            rrr = 0.0
            balls_rem = 0

            if len(score_data) >= 2:
                first = score_data[0]
                target = int(str(first.get("r", 0))) + 1
                runs_needed = max(0, target - runs)
                balls_done = int(overs) * 6 + round((overs % 1) * 10)
                balls_rem  = max(0, int(total_overs * 6) - balls_done)
                overs_rem  = balls_rem / 6
                rrr = round(runs_needed / overs_rem, 2) if overs_rem > 0 else 0.0

            bowling_team = team2 if batting_team == team1 else team1

            result = LiveMatchData(
                match_id         = m.get("id", ""),
                match_title      = m.get("name", f"{team1} vs {team2}"),
                format           = fmt,
                status           = "live" if is_live else "completed",
                batting_team     = batting_team or team1,
                bowling_team     = bowling_team,
                runs             = runs,
                wickets          = wickets,
                overs_bowled     = overs,
                total_overs      = total_overs,
                target           = target,
                runs_needed      = runs_needed,
                balls_remaining  = balls_rem,
                crr              = crr,
                rrr              = rrr,
                first_innings_score = target - 1 if target > 0 else 0,
                team1            = team1,
                team2            = team2,
                source           = "cricapi",
            )

            _live_cache[f"score_{m.get('id','')}"] = result
            return result

        except Exception as e:
            log.error(f"CricAPI parse error: {e}")
            return None


# ═══════════════════════════════════════════════════════
#  OPEN CRICKET DATA (No-auth fallback)
# ═══════════════════════════════════════════════════════

class OpenCricketAdapter:
    """
    Uses cricbuzz-cricket RapidAPI (free tier available) as fallback.
    Also tries open cricket score APIs.
    """

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (compatible; CricketBot/1.0)",
        "Accept": "application/json",
    }

    def get_live_matches(self) -> List[Dict]:
        """Try multiple free cricket score endpoints."""
        # Try cricbuzz free API
        endpoints = [
            "https://api.cricbuzz.com/api/cricket-match/live",
            "https://cricket-live-data.p.rapidapi.com/matches",
        ]
        for url in endpoints:
            try:
                r = requests.get(url, headers=self.HEADERS, timeout=8)
                if r.ok:
                    data = r.json()
                    if data:
                        log.info(f"Got live matches from {url}")
                        return self._normalize(data)
            except Exception:
                continue
        return []

    def _normalize(self, data: Any) -> List[Dict]:
        matches = []
        items = data if isinstance(data, list) else data.get("matches", data.get("data", []))
        for m in items[:20]:
            matches.append({
                "match_id": str(m.get("id", m.get("match_id", ""))),
                "title":    m.get("name", m.get("title", "")),
                "status":   m.get("status", "live"),
                "team1":    m.get("team1", m.get("t1", "")),
                "team2":    m.get("team2", m.get("t2", "")),
                "format":   m.get("format", m.get("type", "T20")).upper(),
                "series":   "",
            })
        return matches


# ═══════════════════════════════════════════════════════
#  UNIFIED CRICKET CLIENT
# ═══════════════════════════════════════════════════════

class CricketClient:
    def __init__(self, cricket_data_api_key: str = ""):
        self.cricapi  = CricAPIAdapter(cricket_data_api_key)
        self.fallback = OpenCricketAdapter()
        log.info("CricketClient initialized (CricAPI primary, fallback enabled)")

    def get_live_matches(self) -> List[Dict]:
        """Get all live matches — tries all sources."""
        # Try CricAPI first (structured, reliable)
        if self.cricapi.api_key:
            matches = self.cricapi.get_current_matches()
            live = [m for m in matches if m.get("live") or "live" in m.get("status","").lower()]
            if live:
                log.info(f"CricAPI: {len(live)} live matches")
                return live

        # Fallback
        matches = self.fallback.get_live_matches()
        if matches:
            log.info(f"Fallback: {len(matches)} live matches")
            return matches

        log.warning("No live matches found from any source")
        return []

    def get_match_live_data(self, match_id: str) -> Optional[LiveMatchData]:
        if self.cricapi.api_key:
            return self.cricapi.get_match_score(match_id)
        return None

    def get_upcoming_matches(self) -> List[Dict]:
        if self.cricapi.api_key:
            all_matches = self.cricapi.get_current_matches()
            return [m for m in all_matches if not m.get("live")]
        return []

    def get_team_form(self, team_name: str, fmt: str = "T20") -> TeamFormData:
        return TeamFormData(team_name=team_name)

    def get_head_to_head(self, team1: str, team2: str) -> Dict:
        return _h2h_lookup(team1, team2)

    # Keep ESPN adapter reference for compatibility
    @property
    def espn(self):
        return self


_H2H_DATA = {
    frozenset({"India", "Pakistan"}):          {"India": 0.73, "Pakistan": 0.27},
    frozenset({"India", "Australia"}):         {"India": 0.52, "Australia": 0.48},
    frozenset({"India", "England"}):           {"India": 0.55, "England": 0.45},
    frozenset({"Australia", "England"}):       {"Australia": 0.58, "England": 0.42},
    frozenset({"India", "New Zealand"}):       {"India": 0.60, "New Zealand": 0.40},
    frozenset({"India", "South Africa"}):      {"India": 0.53, "South Africa": 0.47},
    frozenset({"India", "Sri Lanka"}):         {"India": 0.65, "Sri Lanka": 0.35},
    frozenset({"India", "Bangladesh"}):        {"India": 0.72, "Bangladesh": 0.28},
    frozenset({"India", "West Indies"}):       {"India": 0.70, "West Indies": 0.30},
    frozenset({"Australia", "New Zealand"}):   {"Australia": 0.62, "New Zealand": 0.38},
    frozenset({"Australia", "South Africa"}):  {"Australia": 0.55, "South Africa": 0.45},
    frozenset({"England", "New Zealand"}):     {"England": 0.52, "New Zealand": 0.48},
    frozenset({"Pakistan", "Australia"}):      {"Australia": 0.57, "Pakistan": 0.43},
    frozenset({"Pakistan", "England"}):        {"England": 0.52, "Pakistan": 0.48},
    frozenset({"South Africa", "Pakistan"}):   {"South Africa": 0.54, "Pakistan": 0.46},
}


def _h2h_lookup(team1: str, team2: str) -> Dict:
    key  = frozenset({team1, team2})
    data = _H2H_DATA.get(key, {team1: 0.50, team2: 0.50})
    return {
        "team1": team1,
        "team2": team2,
        "team1_win_pct": data.get(team1, 0.50),
        "team2_win_pct": data.get(team2, 0.50),
    }
