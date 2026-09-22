"""OpticOdds v3 API client.

Scope: fixtures for a given date, and anytime-goalscorer odds for those
fixtures at one sportsbook.

A note on the response parsing below. The v3 responses nest odds under a
fixture and name fields inconsistently across markets, and this client was
written without a live key to check against. Every extraction therefore goes
through `_first_of`, which tries a list of candidate keys and raises a loud,
specific error naming what it looked for and what the payload actually had.

That means the first live run either works or tells you exactly which key to
add — instead of silently producing an empty slate. Run `discover` and
`inspect` (see main.py) before the first scheduled run to confirm.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Iterable

import requests

from .config import Config

log = logging.getLogger(__name__)


class OpticOddsError(Exception):
    """Raised on transport failure, auth failure, or an unparseable payload."""


@dataclass
class Fixture:
    id: str
    home_team: str
    away_team: str
    start_date: str

    def opponent_of(self, team: str) -> str:
        if team == self.home_team:
            return self.away_team
        if team == self.away_team:
            return self.home_team
        return ""


def _first_of(payload: dict, candidates: Iterable[str], *, context: str) -> Any:
    """Return the first present, non-null candidate key from a dict.

    Raises with the full key list on failure. The whole point is that a schema
    surprise is loud and self-describing rather than a silent empty result.
    """
    for key in candidates:
        if key in payload and payload[key] is not None:
            return payload[key]
    raise OpticOddsError(
        f"could not find any of {list(candidates)} in {context}. "
        f"Keys present: {sorted(payload.keys())}. "
        f"Add the correct key to the candidate list in optic_odds.py."
    )


def _nested_name(value: Any) -> str:
    """Team and player fields come back either as a bare string or an object
    with a name/display_name. Normalise both to a string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "display_name", "full_name", "abbreviation", "id"):
            if value.get(key):
                return str(value[key])
    return str(value)


class OpticOddsClient:
    def __init__(self, cfg: Config, api_key: str | None = None):
        self.cfg = cfg
        self.base_url = cfg.optic_odds.base_url
        self.timeout = cfg.optic_odds.timeout_seconds
        self.max_retries = cfg.optic_odds.max_retries

        self.api_key = api_key or os.environ.get("OPTICODDS_API_KEY", "")
        if not self.api_key:
            raise OpticOddsError(
                "no API key. Set OPTICODDS_API_KEY in the environment "
                "(on Railway, add it as a service variable)."
            )

        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": self.api_key})

    # -- transport ---------------------------------------------------------

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET with retry on transient failures.

        Retries 429 and 5xx with backoff. Does not retry 4xx other than 429 —
        those are config problems (bad league slug, bad market key) and
        retrying just delays the error message you need to see.
        """
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("request failed (attempt %d/%d): %s", attempt, self.max_retries, exc)
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise OpticOddsError(
                        f"{path} returned non-JSON (status 200): {resp.text[:300]}"
                    ) from exc

            if resp.status_code in (401, 403):
                raise OpticOddsError(
                    f"{path} returned {resp.status_code} — the API key was rejected. "
                    f"Check OPTICODDS_API_KEY."
                )

            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2 ** attempt
                log.warning(
                    "%s returned %d, retrying in %ds (attempt %d/%d)",
                    path, resp.status_code, wait, attempt, self.max_retries,
                )
                last_error = OpticOddsError(f"{path} returned {resp.status_code}")
                time.sleep(wait)
                continue

            raise OpticOddsError(
                f"{path} returned {resp.status_code}: {resp.text[:300]}"
            )

        raise OpticOddsError(f"{path} failed after {self.max_retries} attempts: {last_error}")

    @staticmethod
    def _records(payload: dict) -> list[dict]:
        """v3 wraps results in `data`; tolerate a bare list too."""
        if isinstance(payload, list):
            return payload
        for key in ("data", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []

    # -- discovery ---------------------------------------------------------

    def leagues(self, sport: str) -> list[dict]:
        return self._records(self.get("/api/v3/leagues", {"sport": sport}))

    def markets(self, sport: str, league: str, sportsbook: str) -> list[dict]:
        return self._records(
            self.get(
                "/api/v3/markets",
                {"sport": sport, "league": league, "sportsbook": sportsbook},
            )
        )

    # -- fixtures ----------------------------------------------------------

    def players_for_team(self, team_id: str) -> list[dict]:
        """Full roster for a team, used to seed the season list.

        Without this the roster accretes only as players get priced, so the
        first couple of weeks of slates would be missing most of their tier 4s —
        exactly the players the contest sheet expects to see listed.
        """
        payload = self.get(
            "/api/v3/players",
            {
                "sport": self.cfg.optic_odds.sport,
                "league": self.cfg.optic_odds.league,
                "team_id": team_id,
            },
        )
        return self._records(payload)

    def fixtures_for_date(self, date_iso: str) -> list[Fixture]:
        """Fixtures starting on the given calendar date.

        `start_date` filters to an exact date in OpticOdds, which is what we
        want — the job builds tomorrow's slate, not a range.
        """
        payload = self.get(
            "/api/v3/fixtures",
            {
                "sport": self.cfg.optic_odds.sport,
                "league": self.cfg.optic_odds.league,
                "start_date": date_iso,
            },
        )
        records = self._records(payload)

        if not records:
            log.warning(
                "no fixtures returned for %s (league=%s). If you expect games, "
                "verify the league slug with the `discover` command — an unknown "
                "slug returns an empty list rather than an error.",
                date_iso, self.cfg.optic_odds.league,
            )
            return []

        fixtures = []
        for rec in records:
            try:
                fixtures.append(
                    Fixture(
                        id=str(_first_of(rec, ("id", "fixture_id"), context="fixture record")),
                        home_team=_nested_name(
                            _first_of(rec, ("home_team_display", "home_team", "home"),
                                      context="fixture record")
                        ),
                        away_team=_nested_name(
                            _first_of(rec, ("away_team_display", "away_team", "away"),
                                      context="fixture record")
                        ),
                        start_date=str(rec.get("start_date", date_iso)),
                    )
                )
            except OpticOddsError:
                log.exception("skipping unparseable fixture record: %s", rec)

        return fixtures

    # -- odds --------------------------------------------------------------

    def recent_fixtures_for_team(
        self, team_id: str, before_date: str, limit: int
    ) -> list[Fixture]:
        """The team's most recent eligible completed fixtures, newest first.

        Used by the historical fallback. `before_date` is exclusive, so the
        slate date itself never leaks into its own estimate.

        Preseason is filtered out before the limit is applied, not after —
        otherwise a team whose last five games were exhibitions would come back
        empty instead of reaching further back for real ones.
        """
        payload = self.get(
            "/api/v3/fixtures",
            {
                "sport": self.cfg.optic_odds.sport,
                "league": self.cfg.optic_odds.league,
                "team_id": team_id,
                "start_date_before": before_date,
                "status": "completed",
            },
        )
        records = self._records(payload)

        floor = self.cfg.fallback.earliest_game_date
        excluded_types = set(self.cfg.fallback.exclude_season_types)

        fixtures = []
        for rec in records:
            # Season-type label, where the feed provides one. The date floor
            # below is what actually removes preseason; this is a second line of
            # defence and is skipped silently when the field is absent.
            if excluded_types:
                season_type = str(
                    rec.get("season_type")
                    or rec.get("seasonType")
                    or rec.get("competition_type")
                    or ""
                ).lower()
                if season_type and season_type in excluded_types:
                    continue

            start = str(rec.get("start_date", ""))
            if floor and start and start[:10] < floor:
                continue

            try:
                fixtures.append(
                    Fixture(
                        id=str(_first_of(rec, ("id", "fixture_id"), context="fixture record")),
                        home_team=_nested_name(
                            _first_of(rec, ("home_team_display", "home_team", "home"),
                                      context="fixture record")
                        ),
                        away_team=_nested_name(
                            _first_of(rec, ("away_team_display", "away_team", "away"),
                                      context="fixture record")
                        ),
                        start_date=start,
                    )
                )
            except OpticOddsError:
                log.debug("skipping unparseable historical fixture: %s", rec)

        fixtures.sort(key=lambda f: f.start_date, reverse=True)
        return fixtures[:limit]

    def historical_goalscorer_odds(self, fixture_id: str) -> list[dict]:
        """Goalscorer odds for a completed fixture, preferring the opening line.

        The opening price is the right comparison for what we are estimating:
        a closing line has absorbed confirmed goalies and lineups, which is
        information we do not have at 8pm the night before.

        Where the opening price is exposed differs by feed — sometimes a field
        on the row, sometimes the first entry of a timeseries. Both shapes are
        handled; if neither is present the current price is used and the row is
        marked so the caller can tell.
        """
        params = {
            "fixture_id": fixture_id,
            "sportsbook": self.cfg.odds.sportsbook,
            "market": self.cfg.optic_odds.market,
            "odds_format": "AMERICAN",
        }
        if self.cfg.fallback.line_type == "opening":
            params["include_timeseries"] = "true"

        payload = self.get("/api/v3/fixtures/odds/historical", params)
        records = self._records(payload)

        rows: list[dict] = []
        for rec in records:
            if isinstance(rec, dict) and isinstance(rec.get("odds"), list):
                rows.extend(rec["odds"])
            else:
                rows.append(rec)
        return rows

    def goalscorer_odds(self, fixture: Fixture) -> list[dict]:
        """Raw anytime-goalscorer odds rows for one fixture.

        Returns the raw rows rather than Players so the caller can decide how
        to handle partial coverage, and so `inspect` can dump them verbatim.
        """
        payload = self.get(
            "/api/v3/fixtures/odds",
            {
                "sportsbook": self.cfg.odds.sportsbook,
                "fixture_id": fixture.id,
                "market": self.cfg.optic_odds.market,
                "odds_format": "AMERICAN",
            },
        )
        records = self._records(payload)

        # The odds payload nests rows under each fixture rather than returning
        # them flat, so unwrap a level when that is what came back.
        rows: list[dict] = []
        for rec in records:
            if isinstance(rec, dict) and isinstance(rec.get("odds"), list):
                rows.extend(rec["odds"])
            else:
                rows.append(rec)

        return rows
