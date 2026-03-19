"""
cricket/match_filter.py — Whitelist filter for teams and tournaments.

Only markets matching approved international teams or IPL are traded.
Rejects: club cricket, domestic leagues (except IPL), obscure tournaments.
"""

import re
from typing import Optional, Tuple
from config import config
from logger import get_logger

log = get_logger("cricket.filter")


class MatchFilter:
    """
    Checks a Polymarket market question or ESPN match title against
    our approved team/tournament whitelist.
    """

    def __init__(self):
        # Lowercase all for case-insensitive matching
        self.int_teams = [t.lower() for t in config.INTERNATIONAL_TEAMS]
        self.ipl_teams = [t.lower() for t in config.IPL_TEAMS]
        self.ipl_keywords = [k.lower() for k in config.IPL_KEYWORDS]

        # Women's cricket: same teams but with "women" qualifier
        self.womens_teams = [
            f"{t.lower()} women" for t in config.INTERNATIONAL_TEAMS
        ] + [
            f"women's {t.lower()}" for t in config.INTERNATIONAL_TEAMS
        ]

        # Reject patterns — always skip these even if team names match
        self.reject_patterns = [
            r"county",        # English county cricket
            r"ranji",         # Indian domestic
            r"sheffied shield", r"sheffield shield",  # Australian domestic
            r"plunket shield",  # NZ domestic
            r"csa t20",       # South Africa domestic
            r"super smash",   # NZ domestic T20
            r"big bash",      # Australian BBL domestic — not IPL caliber
            r"hundred",       # The Hundred (UK domestic)
            r"vitality blast", # England domestic
            r"\bu19\b",       # Under-19 matches
            r"u-19",
            r"practice",
            r"warm.?up",
            r"unofficial",
            r"a team",        # India A, Pakistan A etc.
        ]

    def is_valid_market(self, question: str) -> Tuple[bool, str, str]:
        """
        Check if a Polymarket market question is valid for trading.

        Returns:
            (is_valid, matched_team, tournament_type)
            e.g. (True, "India", "international") or (False, "", "")
        """
        q = question.lower()

        # ── Step 1: Reject obviously invalid markets ──────────────────────
        for pattern in self.reject_patterns:
            if re.search(pattern, q):
                log.debug(f"Rejected (blacklist pattern '{pattern}'): {question[:60]}")
                return False, "", ""

        # ── Step 2: Must contain "win" to be a win/loss market ────────────
        if not re.search(r"\bwin\b|\bwinner\b", q):
            log.debug(f"Rejected (no 'win' keyword): {question[:60]}")
            return False, "", ""

        # ── Step 3: Check for IPL keyword + IPL teams ─────────────────────
        is_ipl = any(kw in q for kw in self.ipl_keywords)
        if is_ipl:
            for team in self.ipl_teams:
                if team in q:
                    log.debug(f"Matched IPL team '{team}': {question[:60]}")
                    return True, self._canonical_team(team), "IPL"

        # ── Step 4: Check for international teams ─────────────────────────
        matched_teams = []
        for team in self.int_teams + self.womens_teams:
            if re.search(r'\b' + re.escape(team) + r'\b', q):
                matched_teams.append(team)

        # Need at least one approved team match
        if not matched_teams:
            log.debug(f"Rejected (no approved team found): {question[:60]}")
            return False, "", ""

        # The team we'd be buying on is typically the first team mentioned
        # (Polymarket questions are usually "Will X win vs Y?")
        primary_team = self._extract_primary_team(question, matched_teams)
        tournament   = self._guess_tournament(q)

        log.debug(f"Matched international '{primary_team}' [{tournament}]: {question[:60]}")
        return True, primary_team, tournament

    def is_valid_cricket_match(self, team1: str, team2: str, series: str = "") -> Tuple[bool, str]:
        """
        Check if an ESPN cricket match involves allowed teams.
        Returns (is_valid, tournament_type).
        """
        t1 = team1.lower()
        t2 = team2.lower()
        s  = series.lower()

        # IPL check
        is_ipl = any(kw in s for kw in self.ipl_keywords)
        if is_ipl:
            t1_ok = any(team in t1 for team in self.ipl_teams)
            t2_ok = any(team in t2 for team in self.ipl_teams)
            if t1_ok and t2_ok:
                return True, "IPL"

        # International check
        t1_ok = any(team in t1 for team in self.int_teams + self.womens_teams)
        t2_ok = any(team in t2 for team in self.int_teams + self.womens_teams)

        if t1_ok and t2_ok:
            return True, self._guess_tournament(s)

        return False, ""

    def extract_team_from_question(self, question: str) -> Optional[str]:
        """
        Given 'Will India win vs Australia?', extract 'India'.
        """
        q = question.lower()
        # Look for "Will X win" pattern first
        m = re.search(r"will ([a-z\s]+?) win", q)
        if m:
            candidate = m.group(1).strip()
            for team in self.int_teams + self.ipl_teams:
                if team in candidate:
                    return self._canonical_team(team)

        # Fallback: first team found in question
        for team in self.int_teams + self.ipl_teams:
            if re.search(r'\b' + re.escape(team) + r'\b', q):
                return self._canonical_team(team)

        return None

    def _extract_primary_team(self, question: str, matched_teams: list) -> str:
        """Extract the team the market is betting on (usually 'Will X win')."""
        q = question.lower()
        m = re.search(r"will ([a-z\s]+?) win", q)
        if m:
            subj = m.group(1).strip()
            for team in matched_teams:
                if team in subj:
                    return self._canonical_team(team)
        return self._canonical_team(matched_teams[0]) if matched_teams else ""

    def _canonical_team(self, team_lower: str) -> str:
        """Convert lowercase team name to proper case from config."""
        all_teams = config.INTERNATIONAL_TEAMS + config.IPL_TEAMS
        for t in all_teams:
            if t.lower() == team_lower or team_lower in t.lower():
                return t
        return team_lower.title()

    def _guess_tournament(self, q_lower: str) -> str:
        if "ipl" in q_lower or "indian premier league" in q_lower:
            return "IPL"
        if "world cup" in q_lower or "wc" in q_lower:
            return "ICC World Cup"
        if "champions trophy" in q_lower:
            return "Champions Trophy"
        if "t20i" in q_lower or "t20 international" in q_lower:
            return "T20I"
        if "odi" in q_lower:
            return "ODI"
        if "test" in q_lower:
            return "Test"
        return "International"


# ── Singleton ──────────────────────────────────────────────────────────────
match_filter = MatchFilter()
