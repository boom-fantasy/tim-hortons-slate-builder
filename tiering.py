"""Odds -> probability -> tier, and the slate projection.

This module is the model. It mirrors the maths in the Calibration v2 sheet so
the Slack summary and the spreadsheet agree; if they ever disagree that is a
useful signal that one of them drifted.

No network, no I/O — everything here is pure functions over plain data, which
makes it the only part of the service that is cheap to test.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from .config import Config


@dataclass
class Player:
    name: str
    team: str
    opponent: str
    american_odds: int
    fixture_id: str = ""
    player_id: str = ""

    implied_prob: float = 0.0
    true_prob: float = 0.0
    tier: int | None = None

    # Set when the price was inferred from past games rather than read from a
    # live market for this fixture. estimate_games is how many prior games fed
    # the average — 1 is a thin estimate, 5 is the configured maximum.
    estimated: bool = False
    estimate_games: int = 0

    @property
    def eligible(self) -> bool:
        return self.tier is not None


@dataclass
class TierStats:
    tier: int
    pool_size: int
    top_prob: float
    avg_prob: float
    min_prob: float
    effective_top: float

    @property
    def diluted(self) -> bool:
        """True when the pool is larger than the contest cap, so the top of the
        tier is not guaranteed to appear."""
        return abs(self.effective_top - self.top_prob) > 1e-9


@dataclass
class Projection:
    """Outcome distribution and payout for one modelled user type."""

    label: str
    p1: float
    p2: float
    p3: float
    outcome_probs: list[float]  # index = number correct
    expected_points: float
    expected_correct: float

    @property
    def any_correct(self) -> float:
        return sum(self.outcome_probs[1:])


def american_to_implied(american_odds: int) -> float:
    """Implied probability from American odds.

    Handles both signs even though this tool expects underdog prices — a
    negative value here almost certainly means bad input, and returning a
    silently wrong number would be worse than handling it.
    """
    if american_odds == 0:
        raise ValueError("American odds of 0 is not a valid price")
    if american_odds > 0:
        return 100.0 / (american_odds + 100.0)
    return abs(american_odds) / (abs(american_odds) + 100.0)


def enrich(players: list[Player], cfg: Config) -> list[Player]:
    """Fill in implied probability, de-vigged probability, and tier."""
    for p in players:
        p.implied_prob = american_to_implied(p.american_odds)
        p.true_prob = p.implied_prob * (1.0 - cfg.odds.devig)
        p.tier = cfg.tier_for(p.american_odds)
    return players


def tier_stats(players: list[Player], cfg: Config) -> dict[int, TierStats]:
    """Per-tier pool statistics, including the dilution-adjusted top.

    Dilution: only `max_players_per_tier` players actually make the contest. If
    the pool is bigger than that, the strongest player is not guaranteed to
    appear, so the top-of-tier probability a sharp user actually sees sits
    between the pool top and the pool average.

    The blend below is the same approximation the sheet uses:

        effective_top = (cap/N) * top + (1 - cap/N) * avg

    It is deliberately conservative. The true expectation is a little higher,
    because when the top player is dropped the replacement is the second-best,
    not the average. Erring low here means the projection understates payout
    slightly, which is the safe direction for a budget.
    """
    cap = cfg.contest.max_players_per_tier
    out: dict[int, TierStats] = {}

    for band in cfg.tiers:
        probs = [p.true_prob for p in players if p.tier == band.tier]
        n = len(probs)

        if n == 0:
            out[band.tier] = TierStats(band.tier, 0, 0.0, 0.0, 0.0, 0.0)
            continue

        top = max(probs)
        avg = sum(probs) / n
        lo = min(probs)

        if cap is None or n <= cap:
            effective = top
        else:
            share = cap / n
            effective = share * top + (1.0 - share) * avg

        out[band.tier] = TierStats(band.tier, n, top, avg, lo, effective)

    return out


def outcome_distribution(pick_probs: list[float]) -> list[float]:
    """P(exactly k correct) for independent picks, k = 0..len(pick_probs).

    Generalised rather than hardcoded to three picks so the contest format can
    change without touching this.
    """
    n = len(pick_probs)
    dist = [0.0] * (n + 1)

    for k in range(n + 1):
        total = 0.0
        for hit_set in combinations(range(n), k):
            term = 1.0
            hits = set(hit_set)
            for i, p in enumerate(pick_probs):
                term *= p if i in hits else (1.0 - p)
            total += term
        dist[k] = total

    return dist


def project(label: str, pick_probs: list[float], cfg: Config) -> Projection:
    dist = outcome_distribution(pick_probs)

    payouts = cfg.contest.payouts
    expected_points = sum(
        dist[k] * (payouts[k] if k < len(payouts) else 0) for k in range(len(dist))
    )
    expected_correct = sum(k * dist[k] for k in range(len(dist)))

    p1, p2, p3 = (list(pick_probs) + [0.0, 0.0, 0.0])[:3]

    return Projection(
        label=label,
        p1=p1,
        p2=p2,
        p3=p3,
        outcome_probs=dist,
        expected_points=expected_points,
        expected_correct=expected_correct,
    )


def project_all(stats: dict[int, TierStats], cfg: Config) -> dict[str, Projection]:
    """Project the three user models the sheet shows.

    sharp      — picks the strongest player in each tier, ignoring dilution.
    sharp_adj  — same, but accounting for the per-tier contest cap. This is the
                 honest number on multi-game slates and equals `sharp` whenever
                 every pool fits under the cap.
    average    — picks an average player in each tier. Unaffected by dilution,
                 since the expected average of a random sample equals the
                 population average.
    """
    tiers_in_order = sorted(stats.keys())

    sharp_probs = [stats[t].top_prob for t in tiers_in_order]
    adj_probs = [stats[t].effective_top for t in tiers_in_order]
    avg_probs = [stats[t].avg_prob for t in tiers_in_order]

    return {
        "sharp": project("Sharp", sharp_probs, cfg),
        "sharp_adj": project("Sharp (adj)", adj_probs, cfg),
        "average": project("Average", avg_probs, cfg),
    }


def verdict(projection: Projection, cfg: Config) -> str:
    """One-line read on whether the slate is on budget.

    Judged against the adjusted-sharp projection, which is the worst realistic
    case for payout exposure.
    """
    target = cfg.contest.target_expected_points
    delta = projection.expected_points - target

    if projection.expected_points == 0:
        return "No eligible players — nothing to judge"
    if delta > 2:
        return "HOT — pull back the top of T1 or T2"
    if delta > 1:
        return "Slightly hot — review the top of each tier"
    if delta < -2:
        return "COLD — add stronger players to T1"
    if delta < -1:
        return "Slightly cold — within the usual variance cushion"
    return "On target"


def team_concentration(players: list[Player], cfg: Config) -> dict[str, float]:
    """Share of T1+T2 players held by each team.

    These are the tiers users actually pick from most, so concentration here is
    what drives correlated outcomes. Correlation fattens both tails, and with a
    3-correct bucket paying 10x the 1-correct bucket, the upper tail is now the
    expensive one.
    """
    top_two = [p for p in players if p.tier in (1, 2)]
    if not top_two:
        return {}

    counts: dict[str, int] = {}
    for p in top_two:
        counts[p.team] = counts.get(p.team, 0) + 1

    total = len(top_two)
    return {team: n / total for team, n in sorted(counts.items(), key=lambda kv: -kv[1])}
