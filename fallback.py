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

2. The scratch check only counts games whose odds we successfully RETRIEVED.
   Every completed game was priced at some point, so a player missing from one
   really was scratched. But a game we could not fetch (API error, no retention
   on that fixture) tells us nothing about anyone, so it is skipped and the
   check falls through to the next game back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Config
from .optic_odds import Fixture, OpticOddsClient
from .tiering import Player

log = logging.getLogger(__name__)


@dataclass
class PriorAppearance:
    fixture_id: str
    start_date: str
    # None means the game's market was retrieved and this player was not in it —
    # i.e. a scratch. Games we could not retrieve produce no entry at all.
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
    client: OpticOddsClient,
    cfg: Config,
    team_id: str,
    team_name: str,
    before_date: str,
) -> dict[str, list[PriorAppearance]]:
    """Per-player appearance history for one team, newest game first.

    A player appears in the list for every sampled game that was priced —
    with a probability when they were in it, and None when they were not.
    That distinction is what the scratch check needs.
    """
    from .slate import _extract_player_name, _extract_price, _is_yes_side

    fixtures = client.recent_fixtures_for_team(
        team_id, before_date, cfg.fallback.lookback_games
    )
    if not fixtures:
        log.info("no completed fixtures found for %s before %s", team_name, before_date)
        return {}

    # Pass 1 — retrieve every game we can, keeping them in newest-first order.
    #
    # This has to complete before any per-player record is built. Building the
    # records incrementally while walking games would start a player's history
    # at the first game they appear in, so a player absent from the MOST RECENT
    # game would never get an "absent" entry for it — and the scratch check
    # below would read their next-oldest game as if it were their latest.
    retrieved: list[tuple[Fixture, dict[str, float]]] = []

    for fx in fixtures:
        try:
            rows = client.historical_goalscorer_odds(fx.id)
        except Exception:
            log.warning("historical odds failed for fixture %s — skipping", fx.id)
            continue

        priced_this_game: dict[str, float] = {}
        for row in rows:
            if not _is_yes_side(row):
                continue
            price = _extract_price(row)
            name = _extract_player_name(row)
            if price is None or not name or price <= 0:
                continue
            implied = 100.0 / (price + 100.0)
            priced_this_game[name] = implied * (1.0 - cfg.odds.devig)

        if not priced_this_game:
            # Could not retrieve a market for a completed game. That is a gap on
            # our side, not evidence about any player, so drop the game entirely —
            # otherwise every skater would look scratched.
            log.debug("no retrievable market for historical fixture %s; skipping", fx.id)
            continue

        retrieved.append((fx, priced_this_game))

    if not retrieved:
        return {}

    # Pass 2 — every player who appeared in any retrieved game gets an entry for
    # EVERY retrieved game, with None where they were absent. That absence is
    # the scratch signal.
    universe: set[str] = set()
    for _fx, priced in retrieved:
        universe.update(priced)

    history: dict[str, list[PriorAppearance]] = {name: [] for name in universe}
    for fx, priced in retrieved:
        for name in universe:
            history[name].append(
                PriorAppearance(fx.id, fx.start_date, priced.get(name))
            )

    return history


def estimate_for_fixture(
    client: OpticOddsClient,
    cfg: Config,
    fixture: Fixture,
    team_ids: dict[str, str],
    slate_date: str,
) -> FallbackOutcome:
    """Estimate a player pool for one unpriced game."""
    label = f"{fixture.away_team} @ {fixture.home_team}"
    outcome = FallbackOutcome(fixture_label=label)

    for team_name in (fixture.home_team, fixture.away_team):
        team_id = team_ids.get(team_name)
        if not team_id:
            log.warning("no team id for %s — cannot estimate its players", team_name)
            continue

        history = _collect_history(client, cfg, team_id, team_name, slate_date)
        if not history:
            continue

        sampled = max((len(v) for v in history.values()), default=0)
        outcome.games_sampled = max(outcome.games_sampled, sampled)
        outcome.priced_games_sampled = max(outcome.priced_games_sampled, sampled)

        opponent = fixture.opponent_of(team_name)

        for name, appearances in history.items():
            # Newest first.
            appearances.sort(key=lambda a: a.start_date, reverse=True)

            if cfg.fallback.require_recent_appearance and appearances:
                # _collect_history records only games whose market we actually
                # retrieved, so the first entry is the most recent game we have
                # real evidence about. Absent from it means scratched.
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
            )
            player.estimated = True
            player.estimate_games = len(probs)
            outcome.estimated.append(player)

    return outcome
