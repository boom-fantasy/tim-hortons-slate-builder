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

    # Tier by odds band, before per-tier selection. `trimmed` marks a player
    # who was inside a band but not selected for the contest (see
    # select_submitted); their `tier` is then None, so they read as tier 4.
    band_tier: int | None = None
    trimmed: bool = False

    # From the injury list at build time ("" when not listed). A player whose
    # status is in injuries.exclude_statuses is kept out of the contest:
    # tier None, `injury_excluded` set, tier 4 in the paste tab.
    injury: str = ""
    injury_type: str = ""
    injury_excluded: bool = False

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


def ensure_min_tier_sizes(players: list[Player], cfg: Config) -> list[tuple[int, int]]:
    """Guarantee every tier at least slate.min_per_tier players, in place.

    On a small slate a tier's configured band can hold only a handful of
    players (a 2-game night had 3 in tier 1's +100-200). Working down the
    tiers in order, a tier short of the minimum stretches its upper bound to
    the next-longer prices until it reaches the minimum, taking them from the
    next tier, which then starts just after it; the last tier stretches into
    longer odds. Tiers stay contiguous and ordered by price. Players tied on
    odds move together, so a tier can end a little over the minimum.

    Returns that night's (min_odds, max_odds) per tier — the configured bands
    when nothing needed stretching. Injury-excluded players are ignored.
    """
    need = cfg.slate.min_per_tier
    odds = sorted(p.american_odds for p in players
                  if not p.injury_excluded and p.american_odds >= cfg.tiers[0].min_odds)
    bands: list[tuple[int, int]] = []
    lo = cfg.tiers[0].min_odds
    for band in cfg.tiers:
        hi = max(band.max_odds, lo - 1)
        count = sum(1 for o in odds if lo <= o <= hi)
        if count < need:
            for o in sorted({o for o in odds if o > hi}):
                hi = o
                count = sum(1 for x in odds if lo <= x <= hi)
                if count >= need:
                    break
        bands.append((lo, hi))
        lo = hi + 1

    for p in players:
        if p.injury_excluded:
            continue
        p.tier = next((t.tier for t, (a, b) in zip(cfg.tiers, bands) if a <= p.american_odds <= b),
                      None)
    return bands


def _spread_pick(candidates: list[Player], count: int) -> list[Player]:
    """`count` players spread evenly from strongest to weakest of `candidates`
    (already sorted strongest first)."""
    if count <= 0:
        return []
    if len(candidates) <= count:
        return list(candidates)
    if count == 1:
        return [candidates[len(candidates) // 2]]
    step = (len(candidates) - 1) / (count - 1)
    return [candidates[round(i * step)] for i in range(count)]


def ensure_late_game_coverage(players: list[Player], cfg: Config,
                              late_fixture_ids: set[str]) -> dict[int, int]:
    """Make sure every tier has players from the night's final game(s).

    Once the earlier games start, the contest shows only players from games
    that have not started — so a tier with no submitted players from the last
    start time cannot be picked in at all late in the evening. Covering the
    last start time covers every earlier point too, since those players stay
    available all evening.

    For each tier short of slate.late_game_min_per_tier, unselected players
    from the final game(s) in that tier's band are ADDED back (spread from
    strongest to weakest, like the main selection). Nobody already selected is
    removed. Returns the final count per tier from the final game(s); a tier
    still under the minimum means the final game simply has too few players
    in that band.
    """
    need = cfg.slate.late_game_min_per_tier
    counts: dict[int, int] = {}
    for band in cfg.tiers:
        late_band = sorted(
            (p for p in players if p.band_tier == band.tier and p.fixture_id in late_fixture_ids),
            key=lambda p: (-p.true_prob, p.name),
        )
        have = [p for p in late_band if p.tier == band.tier]
        short = need - len(have)
        if short > 0:
            spare = [p for p in late_band if p.trimmed]
            for p in _spread_pick(spare, short):
                p.tier = band.tier
                p.trimmed = False
            have = [p for p in late_band if p.tier == band.tier]
        counts[band.tier] = len(have)
    return counts


def select_submitted(players: list[Player], cfg: Config) -> None:
    """Choose who is submitted to the contest in each tier, in place.

    slate.selection:
      all     everyone in the band (the tier pool as-is)
      top     the `submitted_per_tier` most likely to score
      spread  the `spread_top` most likely, then the rest of the slots spread
              evenly down the remaining pool to its weakest player — so each
              tier keeps a deliberate mix of strong, middling and weak options

    A pool at or under `submitted_per_tier` is submitted whole. Players tied
    on odds at the top cut are all kept, rather than choosing between equal
    prices arbitrarily. Unselected players get tier None and `trimmed` set:
    out of the contest, listed as tier 4 in the paste tab.

    Order is deterministic (probability, then name), so the same odds always
    give the same selection.
    """
    sc = cfg.slate
    for p in players:
        p.band_tier = p.tier
        p.trimmed = False
    if sc.selection == "all":
        return

    n = sc.submitted_per_tier
    for band in cfg.tiers:
        pool = sorted((p for p in players if p.tier == band.tier),
                      key=lambda p: (-p.true_prob, p.name))
        if len(pool) <= n:
            continue

        k = n if sc.selection == "top" else sc.spread_top
        if k > 0:
            cut = pool[k - 1].american_odds
            top = [p for p in pool if p.american_odds <= cut]   # keep ties at the cut
        else:
            top = []
        top = top[:max(n, k)] if len(top) > n else top
        rest = [p for p in pool if p not in top]
        slots = n - len(top)

        if sc.selection == "top" or slots <= 0:
            chosen = top
        elif len(rest) <= slots:
            chosen = top + rest
        elif slots == 1:
            chosen = top + [rest[len(rest) // 2]]
        else:
            step = (len(rest) - 1) / (slots - 1)          # first to last, evenly
            chosen = top + [rest[round(i * step)] for i in range(slots)]

        keep = {id(p) for p in chosen}
        for p in pool:
            if id(p) not in keep:
                p.tier = None
                p.trimmed = True
    # Late-game coverage is applied afterwards by the caller, which knows the
    # fixtures' start times — see ensure_late_game_coverage.


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


# Verdict thresholds as a share of the target. Originally absolute ±1 / ±2
# points against a 13.00 budget, i.e. ±1/13 (~7.7%) and ±2/13 (~15.4%); kept
# proportional so they mean the same at any target. Against the 60%
# any-correct target that is ±4.6 / ±9.2 points.
_SLIGHT_SHARE = 1 / 13
_STRONG_SHARE = 2 / 13


def estimated_actual(projections: dict[str, Projection], cfg: Config) -> float:
    """Estimated real any-correct rate for a slate.

    Real results track the AVERAGE-user line (a random picker) plus a small
    offset, not the sharp lines. With odds.devig calibrated to actual goals,
    the average line matches real scoring within ~1.5 points a week, and
    reported results ran 1.2-4.8 points above it over weeks 1-3.
    contest.actual_offset holds that gap; re-measure it as weeks come in.
    """
    return projections["average"].any_correct + cfg.contest.actual_offset


def verdict(projections: dict[str, Projection], cfg: Config) -> str:
    """One-line read on whether the slate is on target.

    Judged on the estimated real win rate (average line + measured offset)
    against the any-correct target — the number that is actually reported.
    The sharp lines overstate real results, and with the strongest players
    always submitted they would read HOT on most busy nights.
    """
    if projections["average"].expected_points == 0:
        return "No eligible players — nothing to judge"

    target = cfg.contest.target_any_correct
    est = estimated_actual(projections, cfg)
    delta = est - target
    slight = _SLIGHT_SHARE * target
    strong = _STRONG_SHARE * target
    read = f"est. {est:.1%} vs {target:.0%} target"

    if delta > strong:
        return f"HOT — {read}; lower slate.spread_top or lengthen the bands"
    if delta > slight:
        return f"Slightly hot — {read}"
    if delta < -strong:
        return f"COLD — {read}; raise slate.spread_top or shorten the bands"
    if delta < -slight:
        return f"Slightly cold — {read}"
    return f"On target — {read}"


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
