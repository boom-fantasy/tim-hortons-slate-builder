"""Slate assembly: fixtures + raw odds rows -> an eligible player pool.

The rules that matter here are about what NOT to include, because a slate that
silently includes the wrong players is worse than one that fails loudly:

  * Only the "Yes"/"to score" side of the market. Anytime-goalscorer is a
    two-way market and the "No" side is a heavy favourite — including it would
    inject ~-300 prices into a pool that assumes underdog odds throughout.
  * Only positive American odds. A player priced shorter than even money to
    score is either a data error or a market this model was not built for.
  * Whole games get dropped when their market is missing, rather than partially
    populated. Props post in waves, and the games priced first skew toward
    marquee matchups with higher totals — a partial pull is not a random subset
    of the night, it is a subset weighted toward the best scorers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Config
from .optic_odds import Fixture, OpticOddsClient, _nested_name
from .tiering import Player

log = logging.getLogger(__name__)

# Side markers that represent "this player scores". Anything else on the market
# (the No side, under variants) is discarded.
_YES_SELECTIONS = {"yes", "to score", "over", "1+", "anytime"}


@dataclass
class SlateBuild:
    date_iso: str
    players: list[Player]
    fixtures_total: int
    fixtures_with_market: int
    dropped_fixtures: list[str] = field(default_factory=list)
    skipped_rows: int = 0

    # Games rescued by the historical fallback, and who it declined to rescue.
    estimated_fixtures: list[str] = field(default_factory=list)
    dropped_recent_absence: list[str] = field(default_factory=list)
    dropped_no_history: list[str] = field(default_factory=list)
    # Why the fallback declined, where it did — a thin price store, or no store.
    fallback_notes: list[str] = field(default_factory=list)

    # Priced players whose team could not be resolved from either roster.
    unresolved_team: list[str] = field(default_factory=list)

    fixtures: list[Fixture] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        if self.fixtures_total == 0:
            return 0.0
        return self.fixtures_with_market / self.fixtures_total

    @property
    def eligible(self) -> list[Player]:
        return [p for p in self.players if p.eligible]

    @property
    def estimated(self) -> list[Player]:
        return [p for p in self.players if p.eligible and p.estimated]

    @property
    def priced_fixture_count(self) -> int:
        return self.fixtures_with_market

    @property
    def out_of_band(self) -> list[Player]:
        return [p for p in self.players if not p.eligible]


def _is_yes_side(row: dict) -> bool:
    """True for the "scores" side of the market.

    The side lives in `selection_line`. `selection` and `name` are the player's
    name on player props ("Jordan Morris"), so they say nothing about the side.
    DraftKings' anytime-goalscorer market is one-way and leaves selection_line
    null on every row (verified 2026-09-22), which counts as the Yes side.
    """
    side = str(row.get("selection_line") or "").strip().lower()
    if not side:
        return True
    if side in _YES_SELECTIONS:
        return True
    return any(token in side for token in ("yes", "to score", "anytime"))


def _extract_price(row: dict) -> int | None:
    for key in ("price", "odds", "american_odds", "american_price"):
        value = row.get(key)
        if value is None:
            continue
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            continue
    return None


def _extract_player_name(row: dict) -> str:
    for key in ("player", "player_name", "selection", "name", "participant"):
        value = row.get(key)
        name = _nested_name(value)
        if name:
            return name
    return ""


def _extract_player_id(row: dict) -> str:
    """The feed's stable player identifier.

    This is what the season roster keys on, so a formatting change in the
    display name cannot fork one player into two entries.
    """
    from .optic_odds import _nested_name

    for key in ("player_id", "playerId", "selection_player_id"):
        value = row.get(key)
        if value:
            return str(value)
    # Some feeds nest the id inside the player object rather than exposing it
    # at the top level.
    player = row.get("player")
    if isinstance(player, dict):
        for key in ("id", "player_id"):
            if player.get(key):
                return str(player[key])
    return ""


def _extract_team(row: dict) -> str:
    for key in ("team", "team_name", "player_team", "team_abbreviation"):
        value = row.get(key)
        name = _nested_name(value)
        if name:
            return name
    return ""


def _player_teams(client: OpticOddsClient, fixture: Fixture) -> dict[str, str]:
    """player_id -> team display name, for the two teams in a fixture.

    Player-prop odds rows carry no team (`team_id` is null on them), so the
    team comes from the players endpoint. The names used are the fixture's own
    team strings, so opponent_of() and the paste tab agree with them exactly.
    """
    out: dict[str, str] = {}
    for team_id, team_name in (
        (fixture.home_team_id, fixture.home_team),
        (fixture.away_team_id, fixture.away_team),
    ):
        if not team_id:
            log.warning("no team id for %s on fixture %s", team_name, fixture.id)
            continue
        try:
            records = client.players_for_team(team_id)
        except Exception:
            log.exception("player lookup failed for %s", team_name)
            continue
        for rec in records:
            pid = str(rec.get("id") or rec.get("player_id") or "")
            if pid:
                out[pid] = team_name
    return out


def _team_id_index(client: OpticOddsClient, cfg: Config) -> dict[str, str]:
    """Map team display name -> team id, for the fallback's history lookups.

    One call, cached for the run. Names are indexed under every alias the API
    exposes so a fixture's team string matches regardless of which form it uses.
    """
    from .optic_odds import _nested_name

    index: dict[str, str] = {}
    try:
        payload = client.get(
            "/api/v3/teams",
            {"sport": cfg.optic_odds.sport, "league": cfg.optic_odds.league},
        )
    except Exception:
        log.exception("team lookup failed — fallback cannot resolve team ids")
        return index

    for rec in client._records(payload):
        team_id = rec.get("id") or rec.get("team_id")
        if not team_id:
            continue
        for key in ("name", "display_name", "full_name", "abbreviation", "mascot"):
            label = _nested_name(rec.get(key))
            if label:
                index[label] = str(team_id)

    log.debug("indexed %d team name(s)", len(index))
    return index


def build(
    client: OpticOddsClient,
    cfg: Config,
    date_iso: str,
    price_history: list | None = None,
    use_fallback: bool = True,
) -> SlateBuild:
    """Pull fixtures and odds for a date and assemble the player pool.

    `price_history` is the stored price list the fallback reads (see
    price_store.py). None means it could not be loaded, which the fallback
    reports rather than treating as "no history". `use_fallback=False` is for
    the drift run, which must compare against real prices only.
    """
    from .tiering import enrich  # local import keeps the module import graph flat

    fixtures = client.fixtures_for_date(date_iso)
    log.info("found %d fixture(s) for %s", len(fixtures), date_iso)

    players: list[Player] = []
    dropped: list[str] = []
    unpriced: list[Fixture] = []
    with_market = 0
    skipped = 0
    unresolved: list[str] = []

    for fixture in fixtures:
        label = f"{fixture.away_team} @ {fixture.home_team}"

        try:
            rows = client.goalscorer_odds(fixture)
        except Exception:
            log.exception("odds pull failed for %s", label)
            dropped.append(label)
            unpriced.append(fixture)
            continue

        yes_rows = [row for row in rows if _is_yes_side(row)]
        teams_by_pid = _player_teams(client, fixture) if yes_rows else {}

        game_players: list[Player] = []
        for row in yes_rows:
            price = _extract_price(row)
            name = _extract_player_name(row)

            if price is None or not name:
                skipped += 1
                continue

            if price <= 0:
                # Not a realistic anytime-goalscorer price; almost certainly the
                # wrong side of the market or a bad row.
                skipped += 1
                continue

            player_id = _extract_player_id(row)
            if not player_id:
                # Not a player: DraftKings lists a "No goal" outcome on this
                # market with no player_id.
                log.debug("skipping row with no player_id: %s", name)
                skipped += 1
                continue

            team = _extract_team(row) or teams_by_pid.get(player_id, "")
            if team not in (fixture.home_team, fixture.away_team):
                # Not on either team's roster in the feed (a fresh call-up).
                # A blank team would break the paste tab and the concentration
                # check, so the player is left out and reported.
                unresolved.append(f"{name} ({label})")
                skipped += 1
                continue

            game_players.append(
                Player(
                    name=name,
                    team=team,
                    opponent=fixture.opponent_of(team),
                    american_odds=price,
                    fixture_id=fixture.id,
                    player_id=player_id,
                )
            )

        if not game_players:
            log.warning("no goalscorer market for %s", label)
            dropped.append(label)
            unpriced.append(fixture)
            continue

        with_market += 1
        players.extend(game_players)
        log.info("%s: %d player(s)", label, len(game_players))

    enrich(players, cfg)

    # Games with no live market: estimate their players from recent prices
    # rather than losing the whole game. Strictly game-level — a player missing
    # from a game that WAS priced is a scratch and stays dropped.
    estimated_fixtures: list[str] = []
    dropped_recent: list[str] = []
    dropped_no_hist: list[str] = []
    fallback_notes: list[str] = []

    if unresolved:
        log.warning(
            "left out %d priced player(s) with no resolvable team: %s",
            len(unresolved), ", ".join(unresolved),
        )

    if use_fallback and cfg.fallback.enabled and unpriced and price_history is None:
        fallback_notes.append("price store could not be read — no games estimated")
        log.warning("fallback skipped: price store could not be read")

    elif use_fallback and cfg.fallback.enabled and unpriced:
        from . import fallback as fallback_mod

        for fixture in unpriced:
            label = f"{fixture.away_team} @ {fixture.home_team}"
            try:
                outcome = fallback_mod.estimate_for_fixture(
                    cfg, fixture, price_history, date_iso
                )
            except Exception:
                log.exception("fallback failed for %s — leaving the game out", label)
                continue

            if outcome.insufficient_history:
                note = f"{label}: {', '.join(outcome.insufficient_history)}"
                fallback_notes.append(note)
                log.warning("fallback: not enough stored history — %s", note)

            if not outcome.estimated:
                log.warning("fallback produced no players for %s", label)
                continue

            enrich(outcome.estimated, cfg)
            players.extend(outcome.estimated)
            estimated_fixtures.append(label)
            dropped_recent.extend(outcome.dropped_recent_absence)
            dropped_no_hist.extend(outcome.dropped_no_history)

            log.info(
                "%s: estimated %d player(s) from %d prior game(s); "
                "dropped %d for recent absence, %d with no history",
                label, len(outcome.estimated), outcome.games_sampled,
                len(outcome.dropped_recent_absence), len(outcome.dropped_no_history),
            )

            if label in dropped:
                dropped.remove(label)

    if cfg.slate.drop_games_without_market and dropped:
        log.warning(
            "dropped %d game(s) with no market and no usable history: %s",
            len(dropped), ", ".join(dropped),
        )

    return SlateBuild(
        date_iso=date_iso,
        players=players,
        fixtures_total=len(fixtures),
        fixtures_with_market=with_market,
        dropped_fixtures=dropped,
        skipped_rows=skipped,
        estimated_fixtures=estimated_fixtures,
        dropped_recent_absence=dropped_recent,
        dropped_no_history=dropped_no_hist,
        fallback_notes=fallback_notes,
        unresolved_team=unresolved,
        fixtures=fixtures,
    )
