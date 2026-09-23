"""Config loading and validation.

Everything tunable lives in config.yaml. This module loads it, validates the
parts that would fail silently or confusingly at runtime, and exposes it as
plain dataclasses.

The validation here is deliberately noisy. A misconfigured tier band or a
targets list that doesn't sum to 1.0 produces a slate that looks fine and is
quietly wrong, which is the worst failure mode for this job.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised when config.yaml is malformed or internally inconsistent."""


@dataclass(frozen=True)
class TierBand:
    tier: int
    min_odds: int
    max_odds: int

    def contains(self, american_odds: int) -> bool:
        return self.min_odds <= american_odds <= self.max_odds


@dataclass(frozen=True)
class ContestConfig:
    payouts: list[int]
    targets: list[float]
    max_players_per_tier: int | None

    @property
    def target_expected_points(self) -> float:
        """The points budget the targets imply, per user per day."""
        return sum(p * t for p, t in zip(self.payouts, self.targets))

    @property
    def target_expected_correct(self) -> float:
        """Expected number of correct picks per user the targets imply."""
        return sum(i * t for i, t in enumerate(self.targets))

    @property
    def target_any_correct(self) -> float:
        return sum(self.targets[1:])


@dataclass(frozen=True)
class OddsConfig:
    devig: float
    sportsbook: str


@dataclass(frozen=True)
class OpticOddsConfig:
    base_url: str
    sport: str
    league: str
    market: str
    timeout_seconds: int
    max_retries: int


@dataclass(frozen=True)
class SlateConfig:
    drop_games_without_market: bool
    coverage_warn_threshold: float
    team_concentration_warn: float
    timezone: str
    # Local hour (in `timezone`) that `--scheduled` runs are allowed to go.
    build_hour: int
    drift_hour: int


@dataclass(frozen=True)
class FallbackConfig:
    enabled: bool
    lookback_games: int
    min_stored_games: int
    require_recent_appearance: bool
    earliest_game_date: str | None
    exclude_season_types: list[str]
    marker: str


@dataclass(frozen=True)
class RosterConfig:
    enabled: bool
    seed_from_api: bool
    exclude_positions: list[str]
    prune_after_days: int
    manual_unmatched_warn_days: int
    tab: str


@dataclass(frozen=True)
class SheetsConfig:
    spreadsheet_id: str
    template_tab: str
    estimate_log_tab: str
    price_history_tab: str
    drift_log_tab: str
    paste_tab_format: str
    align_tier_break: bool
    tab_name_format: str
    first_data_row: int
    columns: dict[str, str]
    date_cell: str
    games_cell: str


@dataclass(frozen=True)
class SlackConfig:
    enabled: bool
    channel: str


@dataclass(frozen=True)
class DriftCheckConfig:
    enabled: bool
    move_threshold: float
    slack: bool


@dataclass(frozen=True)
class Config:
    contest: ContestConfig
    tiers: list[TierBand]
    odds: OddsConfig
    optic_odds: OpticOddsConfig
    slate: SlateConfig
    fallback: FallbackConfig
    roster: RosterConfig
    sheets: SheetsConfig
    slack: SlackConfig
    drift_check: DriftCheckConfig
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    def tier_for(self, american_odds: int) -> int | None:
        """Return the tier an odds value falls in, or None if outside every band."""
        for band in self.tiers:
            if band.contains(american_odds):
                return band.tier
        return None

    def band_for(self, tier: int) -> TierBand | None:
        for band in self.tiers:
            if band.tier == tier:
                return band
        return None


def _require(d: dict, key: str, ctx: str) -> Any:
    if key not in d:
        raise ConfigError(f"config.yaml: missing '{key}' under {ctx}")
    return d[key]


def _validate_contest(c: ContestConfig) -> list[str]:
    problems = []

    if len(c.payouts) != 4:
        problems.append(
            f"contest.payouts must have exactly 4 entries (0-3 correct), got {len(c.payouts)}"
        )
    if len(c.targets) != 4:
        problems.append(
            f"contest.targets must have exactly 4 entries (0-3 correct), got {len(c.targets)}"
        )

    if len(c.targets) == 4:
        total = sum(c.targets)
        if abs(total - 1.0) > 1e-6:
            problems.append(
                f"contest.targets must sum to 1.0, got {total:.4f}. "
                f"Remember index 0 is the share of users getting NOTHING right — "
                f"if your targets only describe the winning buckets, the 0-correct "
                f"entry is 1 minus their sum."
            )
        if any(t < 0 for t in c.targets):
            problems.append("contest.targets contains a negative value")

    if len(c.payouts) == 4 and c.payouts[0] != 0:
        problems.append("contest.payouts[0] should be 0 — no points for zero correct picks")

    if c.max_players_per_tier is not None and c.max_players_per_tier < 1:
        problems.append("contest.max_players_per_tier must be >= 1 or null")

    return problems


def _validate_tiers(tiers: list[TierBand]) -> list[str]:
    problems = []

    if not tiers:
        problems.append("tiers is empty — nothing would ever be assigned")
        return problems

    seen = set()
    for band in tiers:
        if band.tier in seen:
            problems.append(f"tier {band.tier} is defined more than once")
        seen.add(band.tier)

        if band.min_odds > band.max_odds:
            problems.append(
                f"tier {band.tier}: min_odds ({band.min_odds}) > max_odds ({band.max_odds})"
            )
        if band.min_odds <= 0:
            problems.append(
                f"tier {band.tier}: min_odds must be positive. This tool assumes "
                f"underdog (positive) American odds throughout — a favourite priced "
                f"at -150 to score is not a realistic anytime-goalscorer line."
            )

    # Overlapping bands mean the first match wins, which is defined behaviour but
    # almost always a typo.
    ordered = sorted(tiers, key=lambda b: b.min_odds)
    for a, b in zip(ordered, ordered[1:]):
        if a.max_odds >= b.min_odds:
            problems.append(
                f"tiers {a.tier} and {b.tier} overlap "
                f"({a.min_odds}-{a.max_odds} vs {b.min_odds}-{b.max_odds}); "
                f"a player in the overlap lands in whichever is listed first"
            )

    return problems


def _validate_odds(o: OddsConfig) -> list[str]:
    problems = []
    if not 0.0 <= o.devig < 1.0:
        problems.append(f"odds.devig must be in [0, 1), got {o.devig}")
    if o.devig > 0.20:
        problems.append(
            f"odds.devig of {o.devig} is implausibly high for a goalscorer market "
            f"(typical is 0.05-0.08) — check this is not a typo"
        )
    if not o.sportsbook:
        problems.append("odds.sportsbook is empty")
    return problems


def _validate_fallback(f: FallbackConfig) -> list[str]:
    problems = []
    if f.lookback_games < 1:
        problems.append("fallback.lookback_games must be >= 1")
    if f.lookback_games > 20:
        problems.append(
            f"fallback.lookback_games of {f.lookback_games} is very long — role and "
            f"line changes will dominate the average"
        )
    if not 1 <= f.min_stored_games <= f.lookback_games:
        problems.append(
            f"fallback.min_stored_games must be between 1 and lookback_games "
            f"({f.lookback_games}), got {f.min_stored_games}"
        )
    if f.earliest_game_date:
        try:
            datetime.strptime(f.earliest_game_date, "%Y-%m-%d")
        except ValueError:
            problems.append(
                f"fallback.earliest_game_date must be YYYY-MM-DD, "
                f"got {f.earliest_game_date!r}"
            )
    return problems


def _validate_slate(s: SlateConfig) -> list[str]:
    problems = []
    for name in ("build_hour", "drift_hour"):
        value = getattr(s, name)
        if not 0 <= value <= 23:
            problems.append(f"slate.{name} must be an hour 0-23, got {value}")
    return problems


def _validate_roster(r: RosterConfig) -> list[str]:
    problems = []
    if r.prune_after_days < 1:
        problems.append("roster.prune_after_days must be >= 1")
    if r.prune_after_days < 14:
        problems.append(
            f"roster.prune_after_days of {r.prune_after_days} is short — a player "
            f"out with a typical injury would fall off the paste list and reappear "
            f"when they return, which looks like a bug to whoever is pasting"
        )
    if r.manual_unmatched_warn_days < 1:
        problems.append("roster.manual_unmatched_warn_days must be >= 1")
    return problems


def load(path: str | Path | None = None) -> Config:
    """Load and validate config.yaml.

    Raises ConfigError listing every problem found, rather than the first, so a
    misconfigured file takes one round trip to fix instead of five.
    """
    if path is None:
        path = os.environ.get("SLATE_CONFIG", "config.yaml")
    path = Path(path)

    if not path.exists():
        raise ConfigError(f"config file not found: {path.resolve()}")

    with path.open() as fh:
        raw = yaml.safe_load(fh) or {}

    try:
        contest = ContestConfig(
            payouts=list(_require(raw["contest"], "payouts", "contest")),
            targets=[float(x) for x in _require(raw["contest"], "targets", "contest")],
            max_players_per_tier=raw["contest"].get("max_players_per_tier"),
        )
        tiers = [
            TierBand(
                tier=int(t["tier"]),
                min_odds=int(t["min_odds"]),
                max_odds=int(t["max_odds"]),
            )
            for t in raw["tiers"]
        ]
        odds = OddsConfig(
            devig=float(_require(raw["odds"], "devig", "odds")),
            sportsbook=str(_require(raw["odds"], "sportsbook", "odds")),
        )
        oo = raw["optic_odds"]
        optic_odds = OpticOddsConfig(
            base_url=oo.get("base_url", "https://api.opticodds.com").rstrip("/"),
            sport=str(_require(oo, "sport", "optic_odds")),
            league=str(_require(oo, "league", "optic_odds")),
            market=str(_require(oo, "market", "optic_odds")),
            timeout_seconds=int(oo.get("timeout_seconds", 30)),
            max_retries=int(oo.get("max_retries", 3)),
        )
        sl = raw["slate"]
        slate = SlateConfig(
            drop_games_without_market=bool(sl.get("drop_games_without_market", True)),
            coverage_warn_threshold=float(sl.get("coverage_warn_threshold", 0.80)),
            team_concentration_warn=float(sl.get("team_concentration_warn", 0.60)),
            timezone=str(sl.get("timezone", "America/New_York")),
            build_hour=int(sl.get("build_hour", 22)),
            drift_hour=int(sl.get("drift_hour", 9)),
        )
        rs = raw.get("roster", {})
        roster = RosterConfig(
            enabled=bool(rs.get("enabled", False)),
            seed_from_api=bool(rs.get("seed_from_api", True)),
            exclude_positions=[str(x).upper() for x in rs.get("exclude_positions", [])],
            prune_after_days=int(rs.get("prune_after_days", 30)),
            manual_unmatched_warn_days=int(rs.get("manual_unmatched_warn_days", 14)),
            tab=str(rs.get("tab", "_roster")),
        )
        fb = raw.get("fallback", {})
        fallback = FallbackConfig(
            enabled=bool(fb.get("enabled", False)),
            lookback_games=int(fb.get("lookback_games", 5)),
            min_stored_games=int(
                fb.get("min_stored_games", fb.get("lookback_games", 5))
            ),
            require_recent_appearance=bool(fb.get("require_recent_appearance", True)),
            earliest_game_date=(
                str(fb["earliest_game_date"])
                if fb.get("earliest_game_date")
                else None
            ),
            exclude_season_types=[
                str(x).lower() for x in fb.get("exclude_season_types", [])
            ],
            marker=str(fb.get("marker", " (est)")),
        )
        sh = raw["sheets"]
        sheets = SheetsConfig(
            spreadsheet_id=str(sh.get("spreadsheet_id", "")),
            template_tab=str(_require(sh, "template_tab", "sheets")),
            estimate_log_tab=str(sh.get("estimate_log_tab", "_estimate_log")),
            price_history_tab=str(sh.get("price_history_tab", "_price_history")),
            drift_log_tab=str(sh.get("drift_log_tab", "_drift_log")),
            paste_tab_format=str(sh.get("paste_tab_format", "Paste %Y-%m-%d")),
            align_tier_break=bool(sh.get("align_tier_break", True)),
            tab_name_format=str(sh.get("tab_name_format", "Slate %Y-%m-%d")),
            first_data_row=int(sh.get("first_data_row", 8)),
            columns=dict(_require(sh, "columns", "sheets")),
            date_cell=str(sh.get("date_cell", "B3")),
            games_cell=str(sh.get("games_cell", "E3")),
        )
        sk = raw.get("slack", {})
        slack = SlackConfig(
            enabled=bool(sk.get("enabled", False)),
            channel=str(sk.get("channel", "")),
        )
        dc = raw.get("drift_check", {})
        drift_check = DriftCheckConfig(
            enabled=bool(dc.get("enabled", False)),
            move_threshold=float(dc.get("move_threshold", 0.20)),
            slack=bool(dc.get("slack", False)),
        )
    except KeyError as exc:
        raise ConfigError(f"config.yaml: missing required section {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"config.yaml: malformed value — {exc}") from exc

    problems = (
        _validate_contest(contest)
        + _validate_tiers(tiers)
        + _validate_odds(odds)
        + _validate_slate(slate)
        + _validate_fallback(fallback)
        + _validate_roster(roster)
    )
    if problems:
        bullets = "\n".join(f"  - {p}" for p in problems)
        raise ConfigError(f"config.yaml has {len(problems)} problem(s):\n{bullets}")

    return Config(
        contest=contest,
        tiers=tiers,
        odds=odds,
        optic_odds=optic_odds,
        slate=slate,
        fallback=fallback,
        roster=roster,
        sheets=sheets,
        slack=slack,
        drift_check=drift_check,
        raw=raw,
    )
