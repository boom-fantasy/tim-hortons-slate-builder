"""Our own record of goalscorer prices, for the historical fallback.

The API key has no access to /fixtures/odds/historical, so past prices come
from what this service captured itself. Both scheduled runs contribute:

  * build (10pm)   — tomorrow's games, as priced the night before.
  * drift (9:30am) — today's games again. This is the one that matters for the
                    fallback: a game unpriced at 10pm is exactly the game the
                    fallback will later have no other record of, and the
                    morning run is usually the first time it has a market.

A game is keyed on (fixture_id, player_id) and the EARLIEST capture wins. The
10pm price is the closer analogue of what the fallback estimates — a price made
before goalies and lineups are confirmed — so a later capture of the same
player never overwrites it. A later capture does add players who were not
priced the first time.

Only live prices are stored. An estimated player is never written back, or the
fallback would start averaging its own output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Config

log = logging.getLogger(__name__)

HEADER = ["date", "fixture_id", "player_id", "player", "team", "american_odds"]


@dataclass(frozen=True)
class PriceRow:
    date: str          # slate date, YYYY-MM-DD
    fixture_id: str
    player_id: str
    player: str
    team: str
    american_odds: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.fixture_id, self.player_id)

    def as_values(self) -> list:
        return [self.date, self.fixture_id, self.player_id, self.player,
                self.team, self.american_odds]


def rows_from_build(build, cfg: Config) -> list[PriceRow]:
    """Live-priced players from a SlateBuild, as store rows.

    Out-of-band players are kept — the fallback averages over the whole market,
    not just the eligible slice. Players without an id or team cannot be keyed
    or attributed, so they are left out. Fixtures of an excluded season type
    (preseason, where the feed labels it) are left out too, as a second line of
    defence behind the earliest_game_date floor.
    """
    excluded = set(cfg.fallback.exclude_season_types)
    skip_fixtures = {
        f.id for f in build.fixtures
        if f.season_type and f.season_type.lower() in excluded
    }

    rows = []
    for p in build.players:
        if p.estimated or not p.player_id or not p.team:
            continue
        if p.fixture_id in skip_fixtures:
            continue
        rows.append(
            PriceRow(
                date=build.date_iso,
                fixture_id=p.fixture_id,
                player_id=p.player_id,
                player=p.name,
                team=p.team,
                american_odds=p.american_odds,
            )
        )
    return rows


def merge(existing: list[PriceRow], new: list[PriceRow]) -> tuple[list[PriceRow], list[PriceRow]]:
    """Append `new` to `existing`, keeping the earliest capture per key.

    `existing` is in capture order (the tab is only ever appended to, and
    rewrites preserve order), so first-seen is earliest. Returns the merged
    list and the rows that were actually added.
    """
    seen: set[tuple[str, str]] = set()
    merged: list[PriceRow] = []
    for row in existing:
        if row.key in seen:
            continue
        seen.add(row.key)
        merged.append(row)

    added = []
    for row in new:
        if row.key in seen:
            continue
        seen.add(row.key)
        merged.append(row)
        added.append(row)
    return merged, added


def prune(rows: list[PriceRow], cfg: Config) -> tuple[list[PriceRow], int]:
    """Keep only what the fallback can ever read.

    Per team, the newest `lookback_games` games; anything before
    earliest_game_date is dropped outright. The fallback never reaches further
    back than that, so older rows are dead weight in a tab that would otherwise
    grow by a few hundred rows a night.
    """
    floor = cfg.fallback.earliest_game_date
    keep_n = cfg.fallback.lookback_games

    # team -> fixture_id -> date
    games: dict[str, dict[str, str]] = {}
    for r in rows:
        if floor and r.date < floor:
            continue
        games.setdefault(r.team, {})[r.fixture_id] = r.date

    keep: set[tuple[str, str]] = set()
    for team, fixtures in games.items():
        newest = sorted(fixtures.items(), key=lambda kv: kv[1], reverse=True)[:keep_n]
        keep.update((team, fid) for fid, _date in newest)

    kept = [r for r in rows if (r.team, r.fixture_id) in keep]
    return kept, len(rows) - len(kept)
