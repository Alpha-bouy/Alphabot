"""
cricket/signal_engine.py — The brain of the bot.

Produces a 0-100 signal score for a team in a live cricket match.
Higher score = stronger belief that team will win = better entry signal.

Score Components:
  ┌─────────────────────────────────────────────┬────────┐
  │ Component                                   │ Weight │
  ├─────────────────────────────────────────────┼────────┤
  │ 1. Win probability estimate (base)          │   35   │
  │ 2. RRR vs CRR ratio                         │   25   │
  │ 3. Wickets in hand                          │   15   │
  │ 4. Momentum (recent overs / last 5 balls)   │   15   │
  │ 5. Team recent form (last 5 matches)        │    5   │
  │ 6. Head-to-head historical edge             │    5   │
  └─────────────────────────────────────────────┴────────┘
  TOTAL                                                100
"""

from dataclasses import dataclass
from typing import Optional, Dict
from cricket.api_client import LiveMatchData, TeamFormData
from logger import get_logger

log = get_logger("cricket.signal")


@dataclass
class SignalResult:
    """Full signal breakdown for one team in a match."""
    team:              str
    opponent:          str
    signal_score:      int          # 0–100 composite score
    win_prob_estimate: float        # 0.0–1.0 estimated win probability

    # Component scores (for Telegram debugging)
    score_win_prob:    int = 0      # Component 1 contribution
    score_rrr:        int = 0      # Component 2 contribution
    score_wickets:    int = 0      # Component 3 contribution
    score_momentum:   int = 0      # Component 4 contribution
    score_form:       int = 0      # Component 5 contribution
    score_h2h:        int = 0      # Component 6 contribution

    # Context
    match_phase:      str = ""     # "powerplay" | "middle" | "death"
    is_chasing:       bool = False
    recommendation:   str = ""     # "STRONG_BUY" | "BUY" | "HOLD" | "NO_SIGNAL"

    def as_telegram_str(self) -> str:
        bar = "█" * (self.signal_score // 10) + "░" * (10 - self.signal_score // 10)
        return (
            f"📊 *Signal Report — {self.team}*\n"
            f"`{bar}` {self.signal_score}/100\n\n"
            f"├ Win Prob:  {round(self.win_prob_estimate * 100, 1)}%\n"
            f"├ WinProb:  +{self.score_win_prob}pts\n"
            f"├ RRR/CRR:  +{self.score_rrr}pts\n"
            f"├ Wickets:  +{self.score_wickets}pts\n"
            f"├ Momentum: +{self.score_momentum}pts\n"
            f"├ Form:     +{self.score_form}pts\n"
            f"└ H2H:      +{self.score_h2h}pts\n\n"
            f"Phase: `{self.match_phase}` | {'Chasing' if self.is_chasing else 'Defending'}\n"
            f"Signal: *{self.recommendation}*"
        )


class SignalEngine:
    """
    Converts live match data + context into a numerical trading signal.
    Called by the entry logic before every potential buy decision.
    """

    # Min score to even consider a trade
    STRONG_BUY_THRESHOLD = 78
    BUY_THRESHOLD        = 68
    HOLD_THRESHOLD       = 50

    def compute(
        self,
        match:    LiveMatchData,
        team:     str,
        form:     Optional[TeamFormData] = None,
        h2h:      Optional[Dict]         = None,
    ) -> SignalResult:
        """
        Main entry point. Compute full signal for `team` in `match`.

        Args:
            match: Live match state
            team:  The team we're considering buying on Polymarket
            form:  Recent form data (optional)
            h2h:   Head-to-head stats (optional)
        """
        # Determine if team is batting or bowling
        is_batting  = match.batting_team == team
        is_chasing  = match.target > 0 and is_batting
        is_defending = match.target > 0 and not is_batting
        phase       = self._match_phase(match)

        log.debug(
            f"Computing signal: {team} vs {match.bowling_team if is_batting else match.batting_team} | "
            f"{'BAT' if is_batting else 'BOWL'} | Phase={phase} | "
            f"Score={match.runs}/{match.wickets} in {match.overs_bowled}ov"
        )

        # ── Component 1: Win Probability Estimate (35 pts) ────────────────
        win_prob      = self._estimate_win_prob(match, team, is_batting, is_chasing, phase)
        s_win_prob    = self._score_win_prob(win_prob)

        # ── Component 2: RRR vs CRR (25 pts) ─────────────────────────────
        s_rrr         = self._score_rrr(match, is_batting, is_chasing)

        # ── Component 3: Wickets in Hand (15 pts) ────────────────────────
        s_wickets     = self._score_wickets(match, is_batting)

        # ── Component 4: Momentum — Last 3 Overs (15 pts) ────────────────
        s_momentum    = self._score_momentum(match, is_batting, phase)

        # ── Component 5: Recent Form (5 pts) ──────────────────────────────
        s_form        = self._score_form(form, team)

        # ── Component 6: Head-to-Head (5 pts) ────────────────────────────
        opponent      = match.bowling_team if is_batting else match.batting_team
        s_h2h         = self._score_h2h(h2h, team)

        # ── Composite Score ───────────────────────────────────────────────
        total = s_win_prob + s_rrr + s_wickets + s_momentum + s_form + s_h2h
        total = min(100, max(0, total))

        # ── Recommendation ────────────────────────────────────────────────
        if total >= self.STRONG_BUY_THRESHOLD:
            rec = "STRONG_BUY"
        elif total >= self.BUY_THRESHOLD:
            rec = "BUY"
        elif total >= self.HOLD_THRESHOLD:
            rec = "HOLD"
        else:
            rec = "NO_SIGNAL"

        result = SignalResult(
            team              = team,
            opponent          = opponent,
            signal_score      = total,
            win_prob_estimate = win_prob,
            score_win_prob    = s_win_prob,
            score_rrr         = s_rrr,
            score_wickets     = s_wickets,
            score_momentum    = s_momentum,
            score_form        = s_form,
            score_h2h         = s_h2h,
            match_phase       = phase,
            is_chasing        = is_chasing,
            recommendation    = rec,
        )

        log.info(
            f"Signal [{team}]: {total}/100 → {rec} "
            f"(WP={s_win_prob} RRR={s_rrr} WKT={s_wickets} MOM={s_momentum})"
        )
        return result

    # ── Win Probability Estimate ──────────────────────────────────────────

    def _estimate_win_prob(
        self, match: LiveMatchData, team: str,
        is_batting: bool, is_chasing: bool, phase: str
    ) -> float:
        """
        Heuristic win probability based on match state.
        Not a full ML model — tuned cricket intuition encoded as rules.
        """
        # No live data yet (pre-match)
        if match.overs_bowled < 1:
            return 0.50

        # ── Team is bowling first (defending after 1st innings) ──
        if not is_batting and match.target == 0:
            # We're still in 1st innings bowling — assess run rate control
            wickets_taken = match.wickets
            overs_done    = match.overs_bowled
            runs_conceded = match.runs

            if overs_done < 6:
                # Powerplay: wickets critical
                if wickets_taken >= 3:
                    return 0.72
                elif wickets_taken >= 1:
                    return 0.60
                return 0.48

            econ = runs_conceded / overs_done if overs_done > 0 else 8
            if econ < 6.0 and wickets_taken >= 4:
                return 0.78
            elif econ < 7.0 and wickets_taken >= 3:
                return 0.65
            elif econ < 8.0:
                return 0.55
            return 0.40

        # ── Team is chasing (batting 2nd) ──
        if is_chasing:
            if match.runs_needed <= 0:
                return 0.97   # Already won
            if match.wickets >= 10:
                return 0.02   # All out
            if match.balls_remaining <= 0:
                return 0.02   # Overs done

            rrr = match.rrr
            crr = match.crr
            wkts_rem = 10 - match.wickets
            balls_rem = match.balls_remaining

            # Simple logistic-style scoring
            rrr_ratio = crr / rrr if rrr > 0 else 1.0

            base = 0.50
            # RRR factor
            if rrr_ratio > 1.3:   base += 0.22
            elif rrr_ratio > 1.1: base += 0.12
            elif rrr_ratio > 0.9: base += 0.02
            elif rrr_ratio > 0.7: base -= 0.12
            else:                 base -= 0.22

            # Wickets factor
            if wkts_rem >= 8:     base += 0.10
            elif wkts_rem >= 6:   base += 0.05
            elif wkts_rem <= 2:   base -= 0.15
            elif wkts_rem <= 4:   base -= 0.07

            # Balls factor (more balls = more chance)
            if balls_rem > 60:    base += 0.05
            elif balls_rem < 12:  base -= 0.05

            return max(0.02, min(0.98, base))

        # ── Team is defending (batting 1st completed) ──
        if is_defending and match.target > 0:
            runs_needed_by_opp = match.runs_needed
            balls_rem          = match.balls_remaining
            opp_wickets        = match.wickets

            if runs_needed_by_opp > balls_rem * 1.5:
                return 0.88   # Nearly impossible chase
            elif runs_needed_by_opp > balls_rem:
                return 0.73
            elif opp_wickets >= 7:
                return 0.80
            return 0.52

        return 0.50

    def _score_win_prob(self, wp: float) -> int:
        """Convert win probability to 0-35 pts."""
        if wp >= 0.92:  return 35
        if wp >= 0.88:  return 31
        if wp >= 0.84:  return 27
        if wp >= 0.80:  return 23
        if wp >= 0.75:  return 19
        if wp >= 0.70:  return 14
        if wp >= 0.65:  return 9
        if wp >= 0.60:  return 5
        return 0

    # ── RRR vs CRR ───────────────────────────────────────────────────────

    def _score_rrr(self, match: LiveMatchData, is_batting: bool, is_chasing: bool) -> int:
        """RRR/CRR ratio → 0-25 pts. Only meaningful for 2nd innings."""
        if not is_chasing:
            # Batting first: assess run rate vs par
            if match.overs_bowled < 1:
                return 12  # Neutral
            par_rate = self._par_rate(match.format)
            crr = match.crr
            if crr > par_rate * 1.2:   return 25
            if crr > par_rate * 1.05:  return 19
            if crr > par_rate * 0.9:   return 13
            if crr > par_rate * 0.75:  return 7
            return 3

        # Chasing: CRR/RRR ratio
        if match.rrr <= 0:
            return 13  # No RRR data yet

        ratio = match.crr / match.rrr if match.rrr > 0 else 1.0

        if ratio >= 1.35:   return 25
        if ratio >= 1.15:   return 20
        if ratio >= 1.00:   return 14
        if ratio >= 0.85:   return 8
        if ratio >= 0.70:   return 3
        return 0

    def _par_rate(self, fmt: str) -> float:
        if "T20" in fmt.upper(): return 8.0
        if "ODI" in fmt.upper(): return 5.5
        return 3.5

    # ── Wickets in Hand ───────────────────────────────────────────────────

    def _score_wickets(self, match: LiveMatchData, is_batting: bool) -> int:
        """Wickets → 0-15 pts."""
        if is_batting:
            wkts_rem = 10 - match.wickets
            if wkts_rem >= 9:  return 15
            if wkts_rem >= 7:  return 12
            if wkts_rem >= 5:  return 8
            if wkts_rem >= 3:  return 4
            if wkts_rem >= 1:  return 1
            return 0
        else:
            # Bowling: more wickets taken = better
            wkts_taken = match.wickets
            if wkts_taken >= 7:  return 15
            if wkts_taken >= 5:  return 12
            if wkts_taken >= 3:  return 8
            if wkts_taken >= 1:  return 4
            return 1

    # ── Momentum (Recent Overs + Last Balls) ──────────────────────────────

    def _score_momentum(
        self, match: LiveMatchData, is_batting: bool, phase: str
    ) -> int:
        """
        Momentum from recent overs. 0-15 pts.
        High-scoring recent overs for batting team = positive momentum.
        """
        recent = match.recent_overs
        if not recent:
            return 7  # Neutral

        last3    = recent[-3:] if len(recent) >= 3 else recent
        avg_last = sum(last3) / len(last3)
        par      = self._par_rate(match.format)

        # Death overs: higher expectations
        if phase == "death":
            par *= 1.4
        elif phase == "powerplay":
            par *= 0.9

        if is_batting:
            if avg_last >= par * 1.5:   return 15
            if avg_last >= par * 1.2:   return 12
            if avg_last >= par * 0.9:   return 8
            if avg_last >= par * 0.7:   return 4
            return 1
        else:
            # Bowling: we want batting team scoring LOW
            if avg_last < par * 0.6:    return 15
            if avg_last < par * 0.8:    return 11
            if avg_last < par:          return 7
            if avg_last < par * 1.3:    return 3
            return 0

    # ── Recent Form ───────────────────────────────────────────────────────

    def _score_form(self, form: Optional[TeamFormData], team: str) -> int:
        """Team form last 5 matches → 0-5 pts."""
        if not form:
            return 3  # Neutral default
        wr = form.win_rate
        if wr >= 0.80:   return 5
        if wr >= 0.60:   return 4
        if wr >= 0.50:   return 3
        if wr >= 0.40:   return 2
        return 1

    # ── Head-to-Head ──────────────────────────────────────────────────────

    def _score_h2h(self, h2h: Optional[Dict], team: str) -> int:
        """H2H historical advantage → 0-5 pts."""
        if not h2h:
            return 3  # Neutral
        win_pct = h2h.get(f"{team}_win_pct",
                  h2h.get("team1_win_pct" if h2h.get("team1") == team else "team2_win_pct", 0.50))
        if win_pct >= 0.70:  return 5
        if win_pct >= 0.55:  return 4
        if win_pct >= 0.50:  return 3
        if win_pct >= 0.40:  return 2
        return 1

    # ── Match Phase ───────────────────────────────────────────────────────

    def _match_phase(self, match: LiveMatchData) -> str:
        overs = match.overs_bowled
        total = match.total_overs or 20.0
        if overs <= 6:
            return "powerplay"
        elif overs <= total * 0.7:
            return "middle"
        return "death"
