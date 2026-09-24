"""Pre-flight check: prove the service works before real odds exist.

  check            Connections only. OpticOdds key, league and market; Google
                   login, workbook, template tab and write access; Slack token.
                   Posts nothing, leaves nothing behind.

  check --sample   Everything above, then a full rehearsal. A sample slate is
                   built from REAL NHL fixtures and rosters with made-up odds,
                   and pushed through the same publish code the nightly build
                   runs, then through the real drift run. Every write goes to
                   a TEST tab, never the real ones:

                     Slate TEST, Paste TEST            (visible, to eyeball)
                     _TEST roster, _TEST price_history,
                     _TEST estimate_log, _TEST drift_log   (hidden)

                   Each tab is then read back and checked: names, odds, ids
                   and dates survive the round trip, the template's own tier
                   formulas agree with the code, the roster seeded from the
                   API, the estimate log's date filter works.

                   The TEST tabs are left in place so you can look at them.
                   Nothing posts to Slack unless --post-slack is given.

  check --cleanup  Delete the TEST tabs.

Exit code 0 when everything passes, 1 otherwise.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("slate-builder.check")

SLATE_TAB = "Slate TEST"
PASTE_TAB = "Paste TEST"
HIDDEN_TABS = {
    "roster": "_TEST roster",
    "price_history": "_TEST price_history",
    "estimate_log": "_TEST estimate_log",
    "drift_log": "_TEST drift_log",
}
TEST_TABS = [SLATE_TAB, PASTE_TAB, *HIDDEN_TABS.values()]

# Spread across every tier band plus both sides of out-of-band, so each tier,
# the exclusion rule and the sheet's tier formulas all get exercised.
SAMPLE_ODDS = [165, 230, 285, 310, 420, 535, 560, 700, 890, 130, 1400]
PLAYERS_PER_TEAM = 6


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))
        print(f"  {status:<4}  {name}" + (f" — {detail}" if detail else ""), flush=True)

    def ok(self, name, detail=""):
        self.add("PASS", name, detail)

    def warn(self, name, detail=""):
        self.add("WARN", name, detail)

    def fail(self, name, detail=""):
        self.add("FAIL", name, detail)

    @property
    def failed(self) -> int:
        return sum(1 for s, _n, _d in self.rows if s == "FAIL")


def _test_cfg(cfg, post_slack: bool):
    """The real config with every sheet tab pointed at a TEST tab."""
    return dataclasses.replace(
        cfg,
        sheets=dataclasses.replace(
            cfg.sheets,
            tab_name_format=SLATE_TAB,
            paste_tab_format=PASTE_TAB,
            estimate_log_tab=HIDDEN_TABS["estimate_log"],
            price_history_tab=HIDDEN_TABS["price_history"],
            drift_log_tab=HIDDEN_TABS["drift_log"],
        ),
        roster=dataclasses.replace(cfg.roster, tab=HIDDEN_TABS["roster"]),
        # On, so estimated sample players get the marker and the estimate log.
        # No date floor: the sample games are before earliest_game_date, and
        # the price store's prune would otherwise drop every sample row.
        fallback=dataclasses.replace(cfg.fallback, enabled=True, earliest_game_date=None),
        slack=dataclasses.replace(cfg.slack, enabled=post_slack),
        drift_check=dataclasses.replace(cfg.drift_check, slack=False),
    )


# ---------------------------------------------------------------------------
# Connection checks
# ---------------------------------------------------------------------------

def _check_opticodds(cfg, rep: Report, date_iso: str):
    from .optic_odds import OpticOddsClient

    try:
        client = OpticOddsClient(cfg)
    except Exception as exc:
        rep.fail("OpticOdds key", str(exc))
        return None

    try:
        fixtures = client.fixtures_for_date(date_iso)
        rep.ok("OpticOdds key + league",
               f"league {cfg.optic_odds.league!r}: {len(fixtures)} game(s) on {date_iso}")
    except Exception as exc:
        rep.fail("OpticOdds key + league", str(exc))
        return None

    try:
        ids = {str(m.get("id")) for m in client.markets(
            cfg.optic_odds.sport, cfg.optic_odds.league, cfg.odds.sportsbook)}
        if cfg.optic_odds.market in ids:
            rep.ok("OpticOdds market", f"{cfg.optic_odds.market!r} listed at {cfg.odds.sportsbook}")
        else:
            rep.fail("OpticOdds market",
                     f"{cfg.optic_odds.market!r} not offered at {cfg.odds.sportsbook}")
    except Exception as exc:
        rep.fail("OpticOdds market", str(exc))

    return client


def _check_google(cfg, rep: Report):
    from .sheets import SheetsWriter

    has_json = bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))
    try:
        writer = SheetsWriter(cfg)
        title = writer.spreadsheet_title()
        rep.ok("Google login + workbook",
               f"opened {title!r} via "
               f"{'GOOGLE_SERVICE_ACCOUNT_JSON' if has_json else 'GOOGLE_APPLICATION_CREDENTIALS'}")
    except Exception as exc:
        rep.fail("Google login + workbook", str(exc))
        return None

    try:
        tabs = writer.tab_titles()
        if cfg.sheets.template_tab in tabs:
            rep.ok("Template tab", f"{cfg.sheets.template_tab!r} found")
        else:
            rep.fail("Template tab", f"{cfg.sheets.template_tab!r} missing. Tabs: {tabs}")
    except Exception as exc:
        rep.fail("Template tab", str(exc))

    # Write access: create a hidden scratch tab and delete it again.
    probe = "_check_write_probe"
    try:
        writer.delete_tabs([probe])
        writer._ensure_tab(writer._spreadsheet(), probe, ["probe"])
        writer.delete_tabs([probe])
        rep.ok("Google write access", "created and removed a scratch tab")
    except Exception as exc:
        rep.fail("Google write access",
                 f"{exc} — share the workbook with the service account as an Editor")
        return None

    return writer


def _check_slack(cfg, rep: Report) -> None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not cfg.slack.enabled:
        rep.warn("Slack", "slack.enabled is false in config.yaml — nothing will post")
        return
    if not token:
        rep.warn("Slack token", "SLACK_BOT_TOKEN not set — builds will run but not post")
        return
    try:
        resp = requests.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {token}"}, timeout=15,
        ).json()
    except Exception as exc:
        rep.fail("Slack token", str(exc))
        return
    if resp.get("ok"):
        rep.ok("Slack token",
               f"valid for bot {resp.get('user')!r} in workspace {resp.get('team')!r}; "
               f"posts to {cfg.slack.channel} (nothing posted)")
    else:
        rep.fail("Slack token", f"rejected: {resp.get('error')}")


# ---------------------------------------------------------------------------
# Rehearsal
# ---------------------------------------------------------------------------

def _sample_build(client, cfg, start_date: str, rep: Report):
    """Real fixtures and rosters, made-up odds."""
    from .slate import SlateBuild
    from .tiering import Player, enrich

    fixtures, date_iso = [], start_date
    day = datetime.strptime(start_date, "%Y-%m-%d")
    for offset in range(8):
        date_iso = (day + timedelta(days=offset)).strftime("%Y-%m-%d")
        fixtures = client.fixtures_for_date(date_iso)
        if len(fixtures) >= 2:
            break
    if len(fixtures) < 2:
        raise RuntimeError(f"no NHL date with 2+ games in the 8 days from {start_date}")
    # Preseason prices are deliberately never stored, so relabel the sample
    # games as regular season or the price store would be written empty and
    # its round trip would prove nothing. Only these in-memory copies change.
    fixtures = [dataclasses.replace(f, season_type="Regular Season") for f in fixtures[:3]]

    excluded = set(cfg.roster.exclude_positions)
    players, odds_i = [], 0
    estimated_label = ""
    for n, fx in enumerate(fixtures):
        estimated = n == len(fixtures) - 1   # last game plays the "unpriced" role
        if estimated:
            estimated_label = f"{fx.away_team} @ {fx.home_team}"
        for team_id, team, opp in ((fx.home_team_id, fx.home_team, fx.away_team),
                                   (fx.away_team_id, fx.away_team, fx.home_team)):
            skaters = [
                r for r in client.players_for_team(team_id)
                if str(r.get("position") or "").upper() not in excluded and r.get("id")
            ]
            for rec in sorted(skaters, key=lambda r: str(r.get("name")))[:PLAYERS_PER_TEAM]:
                p = Player(
                    name=str(rec["name"]), team=team, opponent=opp,
                    american_odds=SAMPLE_ODDS[odds_i % len(SAMPLE_ODDS)],
                    fixture_id=fx.id, player_id=str(rec["id"]),
                )
                odds_i += 1
                if estimated:
                    p.estimated = True
                    p.estimate_games = 3
                players.append(p)

    enrich(players, cfg)
    build = SlateBuild(
        date_iso=date_iso,
        players=players,
        fixtures_total=len(fixtures),
        fixtures_with_market=len(fixtures) - 1,
        estimated_fixtures=[estimated_label],
        fixtures=fixtures,
    )
    rep.ok("Sample slate",
           f"{len(fixtures)} real game(s) on {date_iso}, {len(players)} real players "
           f"({len(build.eligible)} in band, {len(build.estimated)} estimated)")
    return build


def _verify(writer, cfg, build, rep: Report) -> None:
    """Read every TEST tab back and compare it with what was written."""
    import re

    from .price_store import rows_from_build

    tabs = set(writer.tab_titles())
    missing = [t for t in TEST_TABS if t not in tabs]
    if missing:
        rep.fail("TEST tabs created", f"missing: {missing}")
    else:
        rep.ok("TEST tabs created", ", ".join(TEST_TABS))

    marker = cfg.fallback.marker
    eligible = build.eligible

    # Slate tab: rows, then the template cells around them.
    try:
        got = writer.read_slate(SLATE_TAB)
        want = {(p.name + (marker if p.estimated else ""), p.american_odds) for p in eligible}
        have = {(name, odds) for name, _t, _o, odds in got}
        if have == want:
            rep.ok("Slate tab rows", f"{len(got)} player rows read back exactly")
        else:
            rep.fail("Slate tab rows",
                     f"wrote {len(want)}, read {len(have)}; "
                     f"missing {sorted(want - have)[:3]}, unexpected {sorted(have - want)[:3]}")
    except Exception as exc:
        rep.fail("Slate tab rows", str(exc))

    sh = cfg.sheets
    first = sh.first_data_row
    try:
        date_val = writer.read_values(f"'{SLATE_TAB}'!{sh.date_cell}")
        games_val = writer.read_values(f"'{SLATE_TAB}'!{sh.games_cell}")
        date_s = date_val[0][0] if date_val and date_val[0] else ""
        games_s = games_val[0][0] if games_val and games_val[0] else ""
        detail = f"{sh.date_cell}={date_s!r}, {sh.games_cell}={games_s!r}"
        if date_s and str(games_s) == str(build.fixtures_with_market):
            rep.ok("Template date/games cells", detail)
        else:
            rep.fail("Template date/games cells", detail + " — check date_cell / games_cell")
    except Exception as exc:
        rep.fail("Template date/games cells", str(exc))

    # Header row above the data, and the template's own Tier column.
    try:
        header = (writer.read_values(f"'{SLATE_TAB}'!A{first - 1}:Z{first - 1}") or [[]])[0]
        labels = [str(h).strip().lower() for h in header]
        if labels[:1] == ["player"]:
            rep.ok("Template header row", f"row {first - 1}: {header[:8]}")
        else:
            rep.fail("Template header row",
                     f"row {first - 1} is {header[:8]} — expected it to start with 'Player'; "
                     f"check sheets.first_data_row")
        if "tier" in labels:
            col = chr(ord("A") + labels.index("tier"))
            values = writer.read_values(
                f"'{SLATE_TAB}'!A{first}:{col}{first + len(eligible) - 1}")
            idx = labels.index("tier")

            def _digits(v) -> str:
                m = re.search(r"\d+", str(v))
                return m.group(0) if m else ""

            sheet_tier = {r[0]: _digits(r[idx]) if len(r) > idx else "" for r in values if r}
            code_tier = {p.name + (marker if p.estimated else ""): str(p.tier) for p in eligible}
            diffs = [(n, code_tier[n], sheet_tier.get(n)) for n in code_tier
                     if sheet_tier.get(n) != code_tier[n]]
            if not diffs:
                rep.ok("Sheet tiers match code",
                       f"template's Tier column (col {col}) agrees on all {len(code_tier)}")
            else:
                rep.fail("Sheet tiers match code",
                         f"{len(diffs)} differ (player, code, sheet): {diffs[:3]}")
        else:
            rep.warn("Sheet tiers match code", "no 'Tier' column in the header row to compare")
    except Exception as exc:
        rep.fail("Template header row", str(exc))

    # Roster: seeded from the API for every team, ids intact.
    try:
        roster = writer.read_roster()
        teams = {p.team for p in build.players}
        per_team = {t: sum(1 for e in roster if e.team == t) for t in teams}
        ids_written = {p.player_id for p in build.players}
        ids_read = {e.player_id for e in roster}
        lost = ids_written - ids_read
        if all(per_team.values()) and not lost:
            rep.ok("Roster seeded + ids intact",
                   f"{len(roster)} players across {len(teams)} teams "
                   f"({min(per_team.values())}-{max(per_team.values())} per team)")
        else:
            rep.fail("Roster seeded + ids intact",
                     f"per team {per_team}; {len(lost)} sample ids not found: {sorted(lost)[:3]}")
    except Exception as exc:
        rep.fail("Roster seeded + ids intact", str(exc))

    # Paste tab.
    try:
        paste = writer.read_values(f"'{PASTE_TAB}'!A1:ZZ3")
        header = paste[0] if paste else []
        teams_in_paste = [c for c in header if c]
        if teams_in_paste:
            rep.ok("Paste tab", f"{len(teams_in_paste)} header cell(s): {teams_in_paste[:4]}")
        else:
            rep.fail("Paste tab", "empty")
    except Exception as exc:
        rep.fail("Paste tab", str(exc))

    # Price store: live players only, text intact.
    try:
        stored = writer.read_price_history()
        want = {(r.date, r.fixture_id, r.player_id, r.american_odds)
                for r in rows_from_build(build, cfg)}
        have = {(r.date, r.fixture_id, r.player_id, r.american_odds) for r in stored}
        n_est = len(build.estimated)
        if want and want <= have and not any(r.player_id in {p.player_id for p in build.estimated}
                                    for r in stored if r.date == build.date_iso):
            rep.ok("Price store round trip",
                   f"{len(want)} live rows read back exactly; {n_est} estimated players "
                   f"correctly not stored")
        else:
            rep.fail("Price store round trip",
                     f"wrote {len(want)}, matched {len(want & have)}")
    except Exception as exc:
        rep.fail("Price store round trip", str(exc))

    # Estimate log: the date filter is what drift relies on.
    try:
        entries = writer.read_estimate_log(build.date_iso)
        if len(entries) == len(build.estimated):
            scored = sum(1 for e in entries if e["actual_odds"])
            rep.ok("Estimate log + date filter",
                   f"{len(entries)} rows found by date {build.date_iso}; "
                   f"drift filled {scored}")
        else:
            rep.fail("Estimate log + date filter",
                     f"expected {len(build.estimated)} rows for {build.date_iso}, "
                     f"found {len(entries)}")
    except Exception as exc:
        rep.fail("Estimate log + date filter", str(exc))

    # Drift log: one row per locked player.
    try:
        rows = writer.read_values(f"'{HIDDEN_TABS['drift_log']}'!A:J")[1:]
        mine = [r for r in rows if r and r[0] == build.date_iso]
        flags = {}
        for r in mine:
            flag = r[9] if len(r) > 9 and r[9] else "unchanged"
            flags[flag] = flags.get(flag, 0) + 1
        if len(mine) == len(eligible):
            rep.ok("Drift run + drift log",
                   f"{len(mine)} rows (one per locked player): {flags}")
        else:
            rep.fail("Drift run + drift log",
                     f"expected {len(eligible)} rows for {build.date_iso}, found {len(mine)}")
    except Exception as exc:
        rep.fail("Drift run + drift log", str(exc))


def cmd_check(args, cfg) -> int:
    from . import main as main_mod

    tz = ZoneInfo(cfg.slate.timezone)
    tomorrow = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")
    rep = Report()

    if args.cleanup:
        writer = _check_google(cfg, rep)
        if writer is None:
            return 1
        deleted = writer.delete_tabs(TEST_TABS)
        rep.ok("Cleanup", f"deleted {deleted}" if deleted else "no TEST tabs found")
        return 0

    print("\nConnections\n", flush=True)
    client = _check_opticodds(cfg, rep, tomorrow)
    writer = _check_google(cfg, rep)
    _check_slack(cfg, rep)

    print(
        f"\n  Schedule: build at {cfg.slate.build_hour:02d}:xx and drift at "
        f"{cfg.slate.drift_hour:02d}:xx {cfg.slate.timezone} (with --scheduled)",
        flush=True,
    )

    if args.sample:
        print("\nRehearsal (TEST tabs only)\n", flush=True)
        if client is None or writer is None:
            rep.fail("Rehearsal", "skipped — fix the connection failures above first")
        else:
            tcfg = _test_cfg(cfg, args.post_slack)
            from .sheets import SheetsWriter

            twriter = SheetsWriter(tcfg)
            twriter.delete_tabs(TEST_TABS)   # always start from a clean slate
            try:
                build = _sample_build(client, tcfg, tomorrow, rep)
                rc = main_mod._publish(
                    SimpleNamespace(dry_run=False, overwrite=True),
                    tcfg, client, build, build.date_iso, SLATE_TAB,
                )
                (rep.ok if rc == 0 else rep.fail)("Build publish path", f"exit code {rc}")
                rc = main_mod.cmd_drift(SimpleNamespace(date=build.date_iso), tcfg)
                (rep.ok if rc == 0 else rep.fail)("Drift run", f"exit code {rc}")
                _verify(twriter, tcfg, build, rep)
                print(f"\n  Look at: {twriter.tab_url(SLATE_TAB)}", flush=True)
                print(f"           {twriter.tab_url(PASTE_TAB)}", flush=True)
                print("  Remove the TEST tabs afterwards: python -m src.main check --cleanup",
                      flush=True)
            except Exception as exc:
                log.exception("rehearsal failed")
                rep.fail("Rehearsal", str(exc))

    print(f"\n{'ALL PASSED' if not rep.failed else f'{rep.failed} FAILED'}\n", flush=True)
    return 0 if not rep.failed else 1
