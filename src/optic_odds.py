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
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

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
    # Team ids come from the fixture's competitor lists. Odds rows for player
    # props carry no team at all, so these are what resolve a player's team.
    home_team_id: str = ""
    away_team_id: str = ""
    season_type: str = ""

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


def _competitor_id(rec: dict, side: str) -> str:
    """Team id from `home_competitors` / `away_competitors`, a one-item list."""
    competitors = rec.get(f"{side}_competitors")
    if isinstance(competitors, list) and competitors and isinstance(competitors[0], dict):
        return str(competitors[0].get("id") or "")
    return ""


def _local_date(start: Any, tz: ZoneInfo) -> str:
    """YYYY-MM-DD of a UTC ISO timestamp in the given timezone, or "" if unparseable."""
    try:
        dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%Y-%m-%d")


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

        # team_id -> player records. Slate assembly and roster seeding both
        # need the same rosters in one run.
        self._players_cache: dict[str, list[dict]] = {}

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
        """Full roster for a team.

        Used to resolve each priced player's team (odds rows do not carry one)
        and to seed the season roster, so the first couple of weeks of slates
        are not missing most of their tier 4s.

        Follows the cursor — the endpoint pages at 100 — and caches per team
        for the life of the client.
        """
        if team_id in self._players_cache:
            return self._players_cache[team_id]

        params = {
            "sport": self.cfg.optic_odds.sport,
            "league": self.cfg.optic_odds.league,
            "team_id": team_id,
        }
        records: list[dict] = []
        for _ in range(20):  # hard stop; a team roster is well under one page
            payload = self.get("/api/v3/players", params)
            records.extend(self._records(payload))
            cursor = payload.get("cursor") if isinstance(payload, dict) else None
            if not (isinstance(payload, dict) and payload.get("has_more") and cursor):
                break
            params = {**params, "cursor": cursor}

        self._players_cache[team_id] = records
        return records

    def fixtures_for_date(self, date_iso: str) -> list[Fixture]:
        """Fixtures starting on the given calendar date in `slate.timezone`.

        OpticOdds start times are UTC, and its `start_date` filter matches the
        UTC date. For North American evening games that is the wrong day: a
        7pm Central start on the 21st is 00:00Z on the 22nd. So the query asks
        for the UTC range covering the local day, padded an hour each side
        because the range bounds are exclusive, and the result is filtered back
        down to games whose LOCAL start date is `date_iso`.
        """
        tz = ZoneInfo(self.cfg.slate.timezone)
        day = datetime.strptime(date_iso, "%Y-%m-%d").replace(tzinfo=tz)
        start_utc = day.astimezone(timezone.utc)
        end_utc = (day + timedelta(days=1)).astimezone(timezone.utc)
        pad = timedelta(hours=1)
        fmt = "%Y-%m-%dT%H:%M:%SZ"

        payload = self.get(
            "/api/v3/fixtures",
            {
                "sport": self.cfg.optic_odds.sport,
                "league": self.cfg.optic_odds.league,
                "start_date_after": (start_utc - pad).strftime(fmt),
                "start_date_before": (end_utc + pad).strftime(fmt),
            },
        )
        records = [
            rec for rec in self._records(payload)
            if _local_date(rec.get("start_date"), tz) == date_iso
        ]

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
                        home_team_id=_competitor_id(rec, "home"),
                        away_team_id=_competitor_id(rec, "away"),
                        season_type=str(rec.get("season_type") or ""),
                    )
                )
            except OpticOddsError:
                log.exception("skipping unparseable fixture record: %s", rec)

        return fixtures

    # -- odds --------------------------------------------------------------

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
