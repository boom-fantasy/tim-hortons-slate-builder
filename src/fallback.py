"""Historical fallback: estimate a tier for players in games with no market yet.

Scope is deliberately narrow. This runs at the GAME level only — when an entire
fixture has no goalscorer market at pull time. It never fills in an individual
player who is missing from a game that WAS priced, because those two absences
mean opposite things:

  * Game priced, player missing  -> the book made a judgement on every skater it
                                    expects to dress. The player is scratched or
                                    hurt. Real signal. Drop them.
  * Game not priced at all       -> no information about any individual player.
                                    Safe to estimate.

Two details that are easy to get wrong:

1. Averaging happens in PROBABILITY space, never in odds space. Averaging
   American odds directly is not the same operation and the error is large
   enough to move players across tier boundaries. A player at +200 and +600
   averages to 23.8% (+320) correctly, versus 20.0% (+400) if you average the
   odds — nearly four points of probability, easily a different tier.

2. The scratch check only counts games we actually CAPTURED. History comes
   from our own price store (price_store.py), not the API. A game in the store
   was priced, so a player missing from it really was scratched. A game we
   never captured (unpriced at both runs, or a failed run) tells us nothing
   about anyone. It is simply absent, and the check falls through to the next
   game back.

The store only starts filling when the service does, so for the first weeks a
team may have fewer games stored than lookback_games. Below min_stored_games
the team is not estimated at all, and the reason is reported. The alternative
is quietly averaging one game while the config says five.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Config
from .optic_odds import Fixture
from .price_store import PriceRow
from .tiering import Player

log = logging.getLogger(__name__)


@dataclass
class PriorAppearance:
    fixture_id: str
    start_date: str
    # Implied probability, vig included. None means the game is in the store
    # and this player was not in it — i.e. a scratch. Games we never captured
    # produce no entry at all.
    prob: float | None


@dataclass
class FallbackOutcome:
    """What the fallback decided for one unpriced game."""

    fixture_label: str
    estimated: list[Player] = field(default_factory=list)
    dropped_no_history: list[str] = field(default_factory=list)
    dropped_recent_absence: list[str] = field(default_factory=list)
    games_sampled: int = 0
    priced_games_sampled: int = 0
    # Teams not estimated because the store is too thin, as display strings.
    insufficient_history: list[str] = field(default_factory=list)


def prob_to_american(prob: float) -> int:
    """Inverse of the implied-probability conversion, underdog side.

    Clamped well away from 0 and 1: a probability at either extreme produces an
    absurd price, and anything that extreme is outside every tier band anyway.
    """
    prob = max(min(prob, 0.95), 0.001)
    if prob >= 0.5:
        # Favourite pricing. Not expected for a goalscorer market, but returning
        # a coherent number beats returning nonsense — it will fall outside the
        # bands and be excluded on its own.
        return -int(round(prob / (1 - prob) * 100))
    return int(round((100 - 100 * prob) / prob))


def _collect_history(
    store: list[PriceRow],
    cfg: Config,
    team_name: str,
    before_date: str,
) -> tuple[dict[str, list[PriorAppearance]], dict[str, str], int]:
    """Per-player appearance history for one team, newest game first.

    A player appears in the list for every sampled game — with a probability
    when they were in it, and None when they were not. That distinction is
    what the scratch check needs.

    Keyed on player_id. Returns (history, latest display name per id, number
    of games sampled).
    """
    floor = cfg.fallback.earliest_game_date

    # fixture_id -> (date, {player_id: prob})
    games: dict[str, tuple[str, dict[str, float]]] = {}
    names: dict[str, tuple[str, str]] = {}  # player_id -> (date, name)
    for row in store:
        if row.team != team_name or row.date >= before_date:
            continue
        if floor and row.date < floor:
            continue
        if row.american_odds <= 0:
            continue
        # Implied (with vig), not de-vigged: the average is converted back to
        # a book-style price, and enrich() de-vigs that like any live price.
        # De-vigging here as well would take the vig out twice and push every
        # estimate ~6% long.
        implied = 100.0 / (row.american_odds + 100.0)
        _date, priced = games.setdefault(row.fixture_id, (row.date, {}))
        priced[row.player_id] = implied
        if row.player_id not in names or row.date > names[row.player_id][0]:
            names[row.player_id] = (row.date, row.player)

    if not games:
        log.info("no stored games for %s before %s", team_name, before_date)
        return {}, {}, 0

    retrieved = sorted(games.items(), key=lambda kv: kv[1][0], reverse=True)
    retrieved = retrieved[: cfg.fallback.lookback_games]

    # Every player who appeared in any sampled game gets an entry for EVERY
    # sampled game, with None where they were absent. That absence is the
    # scratch signal. The universe has to be built from all sampled games
    # first: building it incrementally would start a player's history at the
    # first game they appear in, so a player absent from the MOST RECENT game
    # would never get an "absent" entry for it.
    universe: set[str] = set()
    for _fid, (_date, priced) in retrieved:
        universe.update(priced)

    history: dict[str, list[PriorAppearance]] = {pid: [] for pid in universe}
    for fid, (date, priced) in retrieved:
        for pid in universe:
            history[pid].append(PriorAppearance(fid, date, priced.get(pid)))

    latest_names = {pid: names[pid][1] for pid in universe}
    return history, latest_names, len(retrieved)


def estimate_for_fixture(
    cfg: Config,
    fixture: Fixture,
    store: list[PriceRow],
    slate_date: str,
) -> FallbackOutcome:
    """Estimate a player pool for one unpriced game from the price store."""
    label = f"{fixture.away_team} @ {fixture.home_team}"
    outcome = FallbackOutcome(fixture_label=label)
    need = cfg.fallback.min_stored_games

    for team_name in (fixture.home_team, fixture.away_team):
        history, names, sampled = _collect_history(store, cfg, team_name, slate_date)

        if sampled < need:
            outcome.insufficient_history.append(
                f"{team_name} ({sampled} of {need} games stored)"
            )
            continue

        outcome.games_sampled = max(outcome.games_sampled, sampled)
        outcome.priced_games_sampled = max(outcome.priced_games_sampled, sampled)

        opponent = fixture.opponent_of(team_name)

        for pid, appearances in history.items():
            name = names.get(pid, pid)
            # Newest first.
            appearances.sort(key=lambda a: a.start_date, reverse=True)

            if cfg.fallback.require_recent_appearance and appearances:
                # The store holds only games we actually captured, so the first
                # entry is the most recent game we have real evidence about.
                # Absent from it means scratched.
                if appearances[0].prob is None:
                    outcome.dropped_recent_absence.append(name)
                    continue

            probs = [a.prob for a in appearances if a.prob is not None]
            if not probs:
                outcome.dropped_no_history.append(name)
                continue

            mean_prob = sum(probs) / len(probs)
            american = prob_to_american(mean_prob)

            player = Player(
                name=name,
                team=team_name,
                opponent=opponent,
                american_odds=american,
                fixture_id=fixture.id,
                player_id=pid,
            )
            player.estimated = True
            player.estimate_games = len(probs)
            outcome.estimated.append(player)

    return outcome
