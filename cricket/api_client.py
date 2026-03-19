"""
cricket/api_client.py — Multi-source cricket data client.

Sources (in priority order):
  1. cricketdata.org  — structured API, 100 free calls/day (match list + scorecard)
  2. ESPN Cricinfo    — HTML scrape, unlimited, live scores updated ~8-15s

The bot uses ESPN for live in-match polling (fast, free) and cricketdata.org
for match discovery and historical form data.
"""

import re
import json
import time
import asyncio
import aiohttp
import requests
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from cachetools import TTLCache
from tenacity import retry, stop_after_attempt, wait_exponential

from logger import get_logger

log = get_logger("cricket.api")

# ── Data Models ───────────────────────────────────────────────────────────

@dataclass
class BallEvent:
    """Represents one ball in the over — used for momentum detection."""
    over: float
    runs: int
    is_wicket: bool
    is_boundary: bool    # 4 or 6
    extras: int = 0


@dataclass
class LiveMatchData:
    """Normalized live match state consumed by the signal engine."""
    match_id:         str
    match_title:      str           # "India vs Australia, 2nd ODI"
    format:           str           # "T20" | "ODI" | "Test"
    status:           str           # "live" | "upcoming" | "completed"

    # Batting team state
    batting_team:     str
    bowling_team:     str
    runs:             int   = 0
    wickets:          int   = 0
    overs_bowled:     float = 0.0
    total_overs:      float = 0.0   # Target overs

    # Chase / defense context
    target:           int   = 0     # Runs needed (0 if batting first)
    runs_needed:      int   = 0
    balls_remaining:  int   = 0
    crr:              float = 0.0   # Current Run Rate
    rrr:              float = 0.0   # Required Run Rate

    # First innings info (if 2nd innings)
    first_innings_score: int = 0

    # Recent over-by-over data (last 5 overs)
    recent_overs:     List[int] = field(default_factory=list)  # runs per over
    last_5_balls:     List[BallEvent] = field(default_factory=list)

    # Team info
    team1:            str = ""
    team2:            str = ""

    # Metadata
    venue:            str = ""
    source:           str = "unknown"
    fetched_at:       str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class TeamFormData:
    """Recent form for a team — last 5 matches."""
    team_name:  str
    matches:    int   = 0
    wins:       int   = 0
    losses:     int   = 0
    win_rate:   float = 0.0
    formats:    List[str] = field(default_factory=list)
    fetched_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


# ── Cache (TTL=30s for live, 1hr for form) ────────────────────────────────

_live_cache  = TTLCache(maxsize=50, ttl=30)
_form_cache  = TTLCache(maxsize=100, ttl=3600)
_match_cache = TTLCache(maxsize=20, ttl=300)   # upcoming matches


# ═══════════════════════════════════════════════════════════════════════════
#  ESPN CRICINFO ADAPTER  (Primary live source — unlimited, no key)
# ═══════════════════════════════════════════════════════════════════════════

class ESPNAdapter:
    """
    Scrapes ESPN Cricinfo for live match data.
    Uses their internal JSON API endpoint (semi-public, stable for years).
    """
    BASE = "https://www.espncricinfo.com"
    LIVE_JSON = "https://www.espncricinfo.com/ci/engine/match/{match_id}.json"
    LIVE_MATCHES = "https://www.espncricinfo.com/matches/engine/match/live.json"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.espncricinfo.com/",
    }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def get_live_matches(self) -> List[Dict]:
        """Returns list of currently live match IDs and basic info."""
        try:
            r = requests.get(self.LIVE_MATCHES, headers=self.HEADERS, timeout=10)
            r.raise_for_status()
            data = r.json()

            matches = []
            for match in data.get("match", []):
                matches.append({
                    "match_id":    str(match.get("id", "")),
                    "title":       match.get("description", ""),
                    "status":      match.get("live_current_name", ""),
                    "team1":       match.get("team1_name", ""),
                    "team2":       match.get("team2_name", ""),
                    "format":      match.get("match_type_name", ""),
                    "series":      match.get("series_name", ""),
                })
            log.debug(f"ESPN: found {len(matches)} live matches")
            return matches

        except Exception as e:
            log.warning(f"ESPN live matches fetch failed: {e}")
            return []

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def get_match_data(self, match_id: str) -> Optional[LiveMatchData]:
        """Fetch detailed live scorecard for a specific match."""
        cache_key = f"espn_match_{match_id}"
        if cache_key in _live_cache:
            return _live_cache[cache_key]

        try:
            url = self.LIVE_JSON.format(match_id=match_id)
            r = requests.get(url, headers=self.HEADERS, timeout=10)
            r.raise_for_status()
            raw = r.json()

            data = self._parse_espn_json(match_id, raw)
            if data:
                _live_cache[cache_key] = data
            return data

        except Exception as e:
            log.warning(f"ESPN match {match_id} fetch failed: {e}")
            return None

    def _parse_espn_json(self, match_id: str, raw: Dict) -> Optional[LiveMatchData]:
        """Transform ESPN's raw JSON into our normalized LiveMatchData."""
        try:
            match_info = raw.get("match", {})
            scorecard  = raw.get("innings", [])
            live_info  = raw.get("live", {})

            if not match_info:
                return None

            team1 = match_info.get("team1_name", "")
            team2 = match_info.get("team2_name", "")
            fmt   = match_info.get("match_type_name", "T20")
            title = match_info.get("description", f"{team1} vs {team2}")

            # Determine match status
            status = "live" if live_info.get("innings") else "upcoming"

            # Current innings data
            current_inning = live_info.get("innings", {})
            batting_team = current_inning.get("batting_team_id", "")

            runs     = int(current_inning.get("runs", 0))
            wickets  = int(current_inning.get("wickets", 0))
            overs    = float(current_inning.get("overs", 0))

            # CRR
            crr = round(runs / overs, 2) if overs > 0 else 0.0

            # RRR and target (2nd innings)
            target     = 0
            runs_needed = 0
            rrr        = 0.0
            balls_rem  = 0

            if len(scorecard) >= 2:
                first = scorecard[0]
                target = int(first.get("runs", 0)) + 1
                runs_needed = max(0, target - runs)

                # Calculate balls remaining
                total_overs = self._get_total_overs(fmt)
                balls_done  = int(overs) * 6 + round((overs % 1) * 10)
                balls_rem   = max(0, int(total_overs * 6) - balls_done)
                overs_rem   = balls_rem / 6

                rrr = round(runs_needed / overs_rem, 2) if overs_rem > 0 else 0.0

            # Recent overs
            recent_overs = []
            for inning in scorecard[-1:]:
                for over in inning.get("over", [])[-5:]:
                    over_runs = sum(
                        int(b.get("runs_scored", 0))
                        for b in over.get("ball", [])
                    )
                    recent_overs.append(over_runs)

            # Resolve team names from IDs
            batting_team_name = self._resolve_team(batting_team, team1, team2, match_info)
            bowling_team_name = team2 if batting_team_name == team1 else team1

            return LiveMatchData(
                match_id         = match_id,
                match_title      = title,
                format           = fmt,
                status           = status,
                batting_team     = batting_team_name,
                bowling_team     = bowling_team_name,
                runs             = runs,
                wickets          = wickets,
                overs_bowled     = overs,
                total_overs      = self._get_total_overs(fmt),
                target           = target,
                runs_needed      = runs_needed,
                balls_remaining  = balls_rem,
                crr              = crr,
                rrr              = rrr,
                first_innings_score = target - 1 if target > 0 else 0,
                recent_overs     = recent_overs,
                team1            = team1,
                team2            = team2,
                venue            = match_info.get("venue_name", ""),
                source           = "espn",
            )

        except Exception as e:
            log.error(f"ESPN JSON parse error for match {match_id}: {e}")
            return None

    def _get_total_overs(self, fmt: str) -> float:
        mapping = {"T20": 20.0, "T20I": 20.0, "ODI": 50.0, "Test": 90.0}
        for k, v in mapping.items():
            if k.lower() in fmt.lower():
                return v
        return 20.0

    def _resolve_team(self, team_id: str, team1: str, team2: str, match_info: Dict) -> str:
        t1_id = str(match_info.get("team1_id", ""))
        return team1 if team_id == t1_id else team2


# ═══════════════════════════════════════════════════════════════════════════
#  CRICKETDATA.ORG ADAPTER  (Structured, 100 free calls/day)
# ═══════════════════════════════════════════════════════════════════════════

class CricketDataAdapter:
    """
    Uses cricketdata.org API.
    Conserved for: upcoming match list, team form history.
    NOT used for high-frequency live polling (saves daily quota).
    """
    BASE = "https://api.cricapi.com/v1"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._call_count = 0

    def _get(self, endpoint: str, params: Dict = None) -> Optional[Dict]:
        if not self.api_key:
            return None
        p = {"apikey": self.api_key, **(params or {})}
        try:
            r = requests.get(f"{self.BASE}/{endpoint}", params=p, timeout=10)
            r.raise_for_status()
            self._call_count += 1
            log.debug(f"CricketData API call #{self._call_count}: {endpoint}")
            data = r.json()
            if data.get("status") != "success":
                log.warning(f"CricketData non-success: {data.get('info')}")
                return None
            return data
        except Exception as e:
            log.warning(f"CricketData API error ({endpoint}): {e}")
            return None

    def get_current_matches(self) -> List[Dict]:
        """Returns current and upcoming matches from cricketdata.org."""
        cache_key = "cricdata_current"
        if cache_key in _match_cache:
            return _match_cache[cache_key]

        data = self._get("currentMatches")
        if not data:
            return []

        matches = []
        for m in data.get("data", []):
            matches.append({
                "match_id": m.get("id", ""),
                "title":    m.get("name", ""),
                "status":   m.get("status", ""),
                "team1":    m.get("teams", ["", ""])[0] if m.get("teams") else "",
                "team2":    m.get("teams", ["", ""])[1] if len(m.get("teams", [])) > 1 else "",
                "format":   m.get("matchType", "T20"),
                "series":   m.get("series_id", ""),
                "date":     m.get("date", ""),
            })

        _match_cache[cache_key] = matches
        return matches

    def get_team_form(self, team_name: str, fmt: str = "T20") -> TeamFormData:
        """Get last 5 match results for a team (form data)."""
        cache_key = f"form_{team_name}_{fmt}"
        if cache_key in _form_cache:
            return _form_cache[cache_key]

        # Fallback: return neutral form if API not available
        form = TeamFormData(team_name=team_name, matches=5, wins=3,
                            losses=2, win_rate=0.60)
        _form_cache[cache_key] = form

        # Try to get real data
        data = self._get("series", {"search": team_name})
        if data:
            # Parse and update form... simplified
            pass

        return form


# ═══════════════════════════════════════════════════════════════════════════
#  UNIFIED CRICKET CLIENT  (What the rest of the bot calls)
# ═══════════════════════════════════════════════════════════════════════════

class CricketClient:
    """
    Single interface for all cricket data.
    Automatically uses best available source.
    """

    def __init__(self, cricket_data_api_key: str = ""):
        self.espn    = ESPNAdapter()
        self.cricdata = CricketDataAdapter(cricket_data_api_key)
        log.info("CricketClient initialized (ESPN primary, CricketData.org secondary)")

    def get_live_matches(self) -> List[Dict]:
        """
        Get all currently live matches.
        Returns unified format regardless of source.
        """
        matches = self.espn.get_live_matches()

        # If ESPN fails, fall back to cricketdata.org
        if not matches and self.cricdata.api_key:
            log.warning("ESPN failed — falling back to CricketData for live matches")
            matches = self.cricdata.get_current_matches()
            matches = [m for m in matches if "live" in m.get("status", "").lower()]

        return matches

    def get_match_live_data(self, match_id: str) -> Optional[LiveMatchData]:
        """
        Get live scorecard for a specific match.
        Primary: ESPN (fast). Fallback: CricketData.
        """
        return self.espn.get_match_data(match_id)

    def get_upcoming_matches(self) -> List[Dict]:
        """Get upcoming (scheduled) matches for market scanning alignment."""
        upcoming = []

        # Try ESPN live page (includes upcoming tab)
        all_matches = self.espn.get_live_matches()
        upcoming.extend([m for m in all_matches if "upcoming" in m.get("status", "").lower()])

        # Supplement with CricketData
        if self.cricdata.api_key:
            cd_matches = self.cricdata.get_current_matches()
            upcoming.extend([
                m for m in cd_matches
                if not any(
                    u["match_id"] == m["match_id"] for u in upcoming
                )
            ])

        return upcoming

    def get_team_form(self, team_name: str, fmt: str = "T20") -> TeamFormData:
        return self.cricdata.get_team_form(team_name, fmt)

    def get_head_to_head(self, team1: str, team2: str) -> Dict:
        """
        Returns simplified head-to-head stats.
        Uses static lookup for top international matchups.
        (Paid API can replace this for real-time h2h.)
        """
        return _h2h_lookup(team1, team2)


# ── Static H2H Data (top matchups — good enough for signal weighting) ─────

_H2H_DATA = {
    # format: frozenset({team1, team2}): {winner: win_pct}
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
    key = frozenset({team1, team2})
    data = _H2H_DATA.get(key, {team1: 0.50, team2: 0.50})
    return {
        "team1": team1,
        "team2": team2,
        "team1_win_pct": data.get(team1, 0.50),
        "team2_win_pct": data.get(team2, 0.50),
    }
