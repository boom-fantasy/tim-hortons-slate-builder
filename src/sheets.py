"""Google Sheets output.

Each night gets its own tab, created by duplicating the template so all of the
calibration formulas, conditional formatting and cell comments come with it.
The script writes only the four input columns (player, team, opponent, odds)
and lets the sheet compute implied probability, de-vigged probability and tier
from its own formulas.

That duplication is deliberate: tiering.py computes tiers in Python for the
Slack summary, and the sheet computes them independently from the same odds.
If the two ever disagree, one of them has drifted and you want to know.

Auth: a service account, not a personal Google account. Share the workbook with
the service account's email as an Editor. Credentials come from
GOOGLE_SERVICE_ACCOUNT_JSON (the full JSON blob, which is what Railway
variables hold comfortably) or GOOGLE_APPLICATION_CREDENTIALS (a file path).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build as build_service
from googleapiclient.errors import HttpError

from .config import Config
from .price_store import HEADER as PRICE_HISTORY_HEADER, PriceRow
from .roster import RosterEntry
from .tiering import Player

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsError(Exception):
    pass


@dataclass
class WriteResult:
    tab_name: str
    tab_id: int
    rows_written: int
    spreadsheet_url: str


def _credentials() -> Credentials:
    blob = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if blob:
        try:
            info = json.loads(blob)
        except json.JSONDecodeError as exc:
            raise SheetsError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is set but is not valid JSON. "
                "Paste the whole service-account key file contents, including braces."
            ) from exc
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path:
        return Credentials.from_service_account_file(path, scopes=SCOPES)

    raise SheetsError(
        "no Google credentials. Set GOOGLE_SERVICE_ACCOUNT_JSON to the service "
        "account key JSON, or GOOGLE_APPLICATION_CREDENTIALS to a path."
    )


class SheetsWriter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        if not cfg.sheets.spreadsheet_id:
            raise SheetsError("sheets.spreadsheet_id is empty in config.yaml")
        self.spreadsheet_id = cfg.sheets.spreadsheet_id
        self.service = build_service(
            "sheets", "v4", credentials=_credentials(), cache_discovery=False
        )

    # -- helpers -----------------------------------------------------------

    def _spreadsheet(self) -> dict:
        try:
            return self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id
            ).execute()
        except HttpError as exc:
            if exc.resp.status == 404:
                raise SheetsError(
                    f"spreadsheet {self.spreadsheet_id} not found. Check the ID, and "
                    f"confirm the workbook is shared with the service account as an Editor."
                ) from exc
            if exc.resp.status == 403:
                raise SheetsError(
                    f"permission denied on spreadsheet {self.spreadsheet_id}. Share it "
                    f"with the service account's email address as an Editor."
                ) from exc
            raise

    def _tab_id(self, spreadsheet: dict, title: str) -> int | None:
        for sheet in spreadsheet.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == title:
                return props.get("sheetId")
        return None

    def tab_titles(self) -> list[str]:
        return [
            sh["properties"]["title"] for sh in self._spreadsheet().get("sheets", [])
        ]

    def spreadsheet_title(self) -> str:
        return self._spreadsheet().get("properties", {}).get("title", "")

    def delete_tabs(self, titles: list[str]) -> list[str]:
        """Delete the named tabs that exist. Returns the ones deleted."""
        spreadsheet = self._spreadsheet()
        requests_, deleted = [], []
        for title in titles:
            tab_id = self._tab_id(spreadsheet, title)
            if tab_id is not None:
                requests_.append({"deleteSheet": {"sheetId": tab_id}})
                deleted.append(title)
        if requests_:
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id, body={"requests": requests_}
            ).execute()
        return deleted

    def read_values(self, a1_range: str) -> list[list]:
        resp = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id, range=a1_range
        ).execute()
        return resp.get("values", [])

    def tab_url(self, title: str) -> str:
        tab_id = self._tab_id(self._spreadsheet(), title)
        return (
            f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"
            f"/edit#gid={tab_id}"
        )

    # -- public ------------------------------------------------------------

    def write_slate(
        self,
        players: list[Player],
        date_iso: str,
        tab_name: str,
        games_count: int,
        overwrite: bool = False,
    ) -> WriteResult:
        """Duplicate the template tab and fill in the player pool.

        Players are written strongest-first within tier, so the top of each tier
        is at the top of its block and eyeballing the sheet matches how the
        model reasons about it.
        """
        spreadsheet = self._spreadsheet()

        template_id = self._tab_id(spreadsheet, self.cfg.sheets.template_tab)
        if template_id is None:
            titles = [s["properties"]["title"] for s in spreadsheet.get("sheets", [])]
            raise SheetsError(
                f"template tab {self.cfg.sheets.template_tab!r} not found. "
                f"Tabs present: {titles}"
            )

        existing_id = self._tab_id(spreadsheet, tab_name)
        if existing_id is not None:
            if not overwrite:
                raise SheetsError(
                    f"tab {tab_name!r} already exists. Re-run with --overwrite to "
                    f"replace it, or rename the existing tab if you want to keep it."
                )
            log.warning("deleting existing tab %s", tab_name)
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": [{"deleteSheet": {"sheetId": existing_id}}]},
            ).execute()

        # Duplicate the template so formulas, formatting and comments carry over.
        dup = self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "duplicateSheet": {
                            "sourceSheetId": template_id,
                            "insertSheetIndex": 1,
                            "newSheetName": tab_name,
                        }
                    }
                ]
            },
        ).execute()
        new_tab_id = dup["replies"][0]["duplicateSheet"]["properties"]["sheetId"]

        # Strongest first inside each tier; out-of-band players last so they are
        # easy to scan and delete.
        ordered = sorted(
            players,
            key=lambda p: (p.tier if p.tier is not None else 99, -p.true_prob),
        )

        first_row = self.cfg.sheets.first_data_row
        marker = self.cfg.fallback.marker if self.cfg.fallback.enabled else ""
        values = [
            [
                p.name + (marker if p.estimated else ""),
                p.team,
                p.opponent,
                p.american_odds,
            ]
            for p in ordered
        ]

        cols = self.cfg.sheets.columns
        start_col = cols["player"]
        end_col = cols["odds"]
        data_range = f"'{tab_name}'!{start_col}{first_row}:{end_col}{first_row + len(values) - 1}"

        body = {
            "valueInputOption": "USER_ENTERED",
            "data": [
                {"range": data_range, "values": values},
                {
                    "range": f"'{tab_name}'!{self.cfg.sheets.date_cell}",
                    "values": [[date_iso]],
                },
                {
                    "range": f"'{tab_name}'!{self.cfg.sheets.games_cell}",
                    "values": [[games_count]],
                },
            ],
        }
        self.service.spreadsheets().values().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body=body
        ).execute()

        url = (
            f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"
            f"/edit#gid={new_tab_id}"
        )
        log.info("wrote %d players to %s", len(values), tab_name)

        return WriteResult(
            tab_name=tab_name,
            tab_id=new_tab_id,
            rows_written=len(values),
            spreadsheet_url=url,
        )

    # -- season roster ------------------------------------------------------

    ROSTER_HEADER = [
        "team", "player_id", "name", "source", "first_seen", "last_priced",
    ]

    def _ensure_tab(self, spreadsheet: dict, title: str, header: list[str],
                    hidden: bool = True) -> int:
        tab_id = self._tab_id(spreadsheet, title)
        if tab_id is not None:
            return tab_id

        log.info("creating tab %r", title)
        resp = self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {
                "title": title,
                "hidden": hidden,
                "gridProperties": {"frozenRowCount": 1},
            }}}]},
        ).execute()
        tab_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]

        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{title}'!A1",
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()
        return tab_id

    def read_roster(self) -> list[RosterEntry]:
        """Load the season roster. Missing tab returns empty, which is correct
        on the first ever run."""
        title = self.cfg.roster.tab
        try:
            resp = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f"'{title}'!A:F"
            ).execute()
        except HttpError as exc:
            if exc.resp.status == 400:
                return []
            raise

        values = resp.get("values", [])
        if len(values) < 2:
            return []

        entries = []
        for idx, row in enumerate(values[1:], start=2):
            padded = row + [""] * (6 - len(row))
            team, pid, name, source, first_seen, last_priced = padded[:6]
            if not name.strip():
                continue
            entries.append(
                RosterEntry(
                    team=team.strip(),
                    player_id=pid.strip(),
                    name=name.strip(),
                    source=(source.strip().lower() or "api"),
                    first_seen=first_seen.strip(),
                    last_priced=last_priced.strip(),
                    _row=idx,
                )
            )
        return entries

    def write_roster(self, roster: list[RosterEntry]) -> int:
        """Rewrite the roster tab in full.

        A full rewrite rather than incremental edits, because pruning removes
        rows and tracking row indices through that is a reliable way to corrupt
        someone's hand-edits. Ordered by team then name so the tab stays
        readable for anyone adding a row by hand.
        """
        spreadsheet = self._spreadsheet()
        title = self.cfg.roster.tab
        self._ensure_tab(spreadsheet, title, self.ROSTER_HEADER)

        ordered = sorted(roster, key=lambda e: (e.team, e.name))
        values = [self.ROSTER_HEADER] + [
            [e.team, e.player_id, e.name, e.source, e.first_seen, e.last_priced]
            for e in ordered
        ]

        self.service.spreadsheets().values().clear(
            spreadsheetId=self.spreadsheet_id, range=f"'{title}'!A:F"
        ).execute()
        # RAW, not USER_ENTERED. player_ids are hex strings, and one that is all
        # digits or digits-E-digits ("4944873069E1") would be parsed as a
        # number, lose its identity, and fork the player into a second entry.
        # RAW also keeps first_seen / last_priced as plain YYYY-MM-DD strings
        # rather than letting Sheets turn them into locale-formatted dates.
        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{title}'!A1",
            valueInputOption="RAW",
            body={"values": values},
        ).execute()

        log.info("wrote %d roster entries", len(ordered))
        return len(ordered)

    # -- price history ------------------------------------------------------
    #
    # Written RAW, not USER_ENTERED: ids are hex strings, and one that happens
    # to be all digits or digits-E-digits would be parsed as a number.

    def read_price_history(self) -> list[PriceRow]:
        """All stored prices, in capture order. Missing tab returns empty."""
        title = self.cfg.sheets.price_history_tab
        try:
            resp = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f"'{title}'!A:F"
            ).execute()
        except HttpError as exc:
            if exc.resp.status == 400:
                return []  # tab does not exist yet
            raise

        values = resp.get("values", [])
        rows = []
        for row in values[1:]:
            padded = [str(v).strip() for v in row] + [""] * (6 - len(row))
            date, fixture_id, player_id, player, team, odds = padded[:6]
            if not (date and fixture_id and player_id and team):
                continue
            try:
                price = int(round(float(odds)))
            except ValueError:
                continue
            rows.append(PriceRow(date, fixture_id, player_id, player, team, price))
        return rows

    def write_price_history(self, rows: list[PriceRow]) -> int:
        """Rewrite the store in full, preserving the given (capture) order."""
        spreadsheet = self._spreadsheet()
        title = self.cfg.sheets.price_history_tab
        self._ensure_tab(spreadsheet, title, PRICE_HISTORY_HEADER)

        values = [PRICE_HISTORY_HEADER] + [r.as_values() for r in rows]
        self.service.spreadsheets().values().clear(
            spreadsheetId=self.spreadsheet_id, range=f"'{title}'!A:F"
        ).execute()
        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{title}'!A1",
            valueInputOption="RAW",
            body={"values": values},
        ).execute()
        log.info("wrote %d price history row(s)", len(rows))
        return len(rows)

    def append_price_history(self, rows: list[PriceRow]) -> int:
        if not rows:
            return 0
        spreadsheet = self._spreadsheet()
        title = self.cfg.sheets.price_history_tab
        self._ensure_tab(spreadsheet, title, PRICE_HISTORY_HEADER)

        self.service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{title}'!A:F",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [r.as_values() for r in rows]},
        ).execute()
        log.info("appended %d price history row(s)", len(rows))
        return len(rows)

    # -- drift log ----------------------------------------------------------

    DRIFT_LOG_HEADER = [
        "date", "player", "team", "source", "locked_odds", "locked_tier",
        "current_odds", "current_tier", "prob_move", "flag",
    ]

    def append_drift_log(self, rows: list[list], date_iso: str) -> int:
        """Append one morning's drift rows. Never pruned.

        Skipped when the date is already logged, so a re-run of drift (by hand,
        or a retry) does not double-count a day. The first run of the morning
        is the one kept, matching the price store's earliest-capture rule.
        """
        if not rows:
            return 0
        spreadsheet = self._spreadsheet()
        title = self.cfg.sheets.drift_log_tab
        self._ensure_tab(spreadsheet, title, self.DRIFT_LOG_HEADER)

        resp = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id, range=f"'{title}'!A:A"
        ).execute()
        logged_dates = {str(r[0]).strip() for r in resp.get("values", [])[1:] if r}
        if date_iso in logged_dates:
            log.info("drift log already has %s — not appending again", date_iso)
            return 0

        # RAW keeps dates as YYYY-MM-DD text, so the check above keeps working.
        self.service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{title}'!A:J",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
        log.info("logged %d drift row(s) for %s", len(rows), date_iso)
        return len(rows)

    # -- paste tab ----------------------------------------------------------

    def write_paste_tab(self, rows: list[list[str]], tab_name: str,
                        overwrite: bool = False) -> str:
        """Write the team-by-team layout your contest sheet is pasted from."""
        spreadsheet = self._spreadsheet()

        existing = self._tab_id(spreadsheet, tab_name)
        if existing is not None:
            if not overwrite:
                raise SheetsError(
                    f"paste tab {tab_name!r} already exists. Re-run with --overwrite."
                )
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": [{"deleteSheet": {"sheetId": existing}}]},
            ).execute()

        width = max((len(r) for r in rows), default=1)
        resp = self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {
                "title": tab_name,
                "index": 1,
                "gridProperties": {
                    "rowCount": max(len(rows) + 10, 50),
                    "columnCount": max(width + 2, 26),
                    "frozenRowCount": 2,
                },
            }}}]},
        ).execute()
        tab_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]

        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{tab_name}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        ).execute()

        # Bold the two header rows so the layout reads at a glance.
        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{
                "repeatCell": {
                    "range": {"sheetId": tab_id, "startRowIndex": 0, "endRowIndex": 2},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }
            }]},
        ).execute()

        log.info("wrote paste tab %s (%d rows)", tab_name, len(rows))
        return (
            f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"
            f"/edit#gid={tab_id}"
        )

    # -- estimate accuracy log --------------------------------------------

    ESTIMATE_LOG_HEADER = [
        "date", "player", "team", "est_odds", "est_tier", "games_used",
        "actual_odds", "actual_tier", "tier_match",
    ]

    def _ensure_estimate_log(self, spreadsheet: dict) -> int:
        """Create the log tab with headers if it does not exist. Returns its id."""
        title = self.cfg.sheets.estimate_log_tab
        tab_id = self._tab_id(spreadsheet, title)
        if tab_id is not None:
            return tab_id

        log.info("creating estimate log tab %r", title)
        resp = self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": title,
                                "hidden": True,
                                "gridProperties": {"frozenRowCount": 1},
                            }
                        }
                    }
                ]
            },
        ).execute()
        tab_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]

        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{title}'!A1",
            valueInputOption="RAW",
            body={"values": [self.ESTIMATE_LOG_HEADER]},
        ).execute()
        return tab_id

    def append_estimates(self, players: list[Player], date_iso: str) -> int:
        """Record each estimated player's estimate, with the actuals left blank.

        Written at build time so the estimate is captured exactly as it was
        made — reconstructing it later from the slate tab would lose how many
        prior games fed it, which is what tells you whether a one-game estimate
        is meaningfully worse than a five-game one.
        """
        estimated = [p for p in players if p.estimated]
        if not estimated:
            return 0

        spreadsheet = self._spreadsheet()
        self._ensure_estimate_log(spreadsheet)
        title = self.cfg.sheets.estimate_log_tab

        rows = [
            [date_iso, p.name, p.team, p.american_odds, p.tier, p.estimate_games,
             "", "", ""]
            for p in estimated
        ]

        # RAW: USER_ENTERED would turn the date into a Sheets date, which can
        # read back in a different format and miss read_estimate_log's filter.
        self.service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{title}'!A:I",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()

        log.info("logged %d estimate(s) for %s", len(rows), date_iso)
        return len(rows)

    def read_estimate_log(self, date_iso: str | None = None) -> list[dict]:
        """Read the log, optionally filtered to one date.

        Each entry carries `_row`, the 1-based sheet row, so pending rows can
        be filled in later without re-matching on content.
        """
        title = self.cfg.sheets.estimate_log_tab
        try:
            resp = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f"'{title}'!A:I"
            ).execute()
        except HttpError as exc:
            if exc.resp.status == 400:
                return []  # tab does not exist yet
            raise

        values = resp.get("values", [])
        if len(values) < 2:
            return []

        out = []
        for idx, row in enumerate(values[1:], start=2):
            padded = row + [""] * (9 - len(row))
            if not padded[0]:
                continue
            if date_iso and padded[0] != date_iso:
                continue
            out.append(
                {
                    "_row": idx,
                    "date": padded[0],
                    "player": padded[1],
                    "team": padded[2],
                    "est_odds": padded[3],
                    "est_tier": padded[4],
                    "games_used": padded[5],
                    "actual_odds": padded[6],
                    "actual_tier": padded[7],
                    "tier_match": padded[8],
                }
            )
        return out

    def fill_estimate_actuals(self, updates: list[tuple[int, int | str, int | str, str]]) -> int:
        """Fill columns G:I for already-logged rows.

        `updates` is (sheet_row, actual_odds, actual_tier, tier_match).
        """
        if not updates:
            return 0

        title = self.cfg.sheets.estimate_log_tab
        data = [
            {
                "range": f"'{title}'!G{row}:I{row}",
                "values": [[odds, tier, match]],
            }
            for row, odds, tier, match in updates
        ]

        self.service.spreadsheets().values().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()

        log.info("filled actuals for %d estimate(s)", len(updates))
        return len(updates)

    def read_slate(self, tab_name: str) -> list[tuple[str, str, str, int]]:
        """Read back a written slate. Used by the drift check."""
        cols = self.cfg.sheets.columns
        first_row = self.cfg.sheets.first_data_row
        rng = f"'{tab_name}'!{cols['player']}{first_row}:{cols['odds']}"

        try:
            resp = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=rng
            ).execute()
        except HttpError as exc:
            raise SheetsError(f"could not read tab {tab_name!r}: {exc}") from exc

        out = []
        for row in resp.get("values", []):
            if len(row) < 4 or not row[0]:
                continue
            try:
                out.append((row[0], row[1], row[2], int(row[3])))
            except (ValueError, IndexError):
                continue
        return out
