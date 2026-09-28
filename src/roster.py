"""Season roster: a stable, persistent player list per team.

The contest sheet expects every player on a team's list every night, with
non-participants marked tier 4 rather than omitted. A slate built only from
who happened to get priced would be a different length each night and would
silently lose anyone scratched, so the roster is kept across the season and
each night's slate is projected onto it.

Identity is the whole game here. Players are keyed on the feed's player_id,
never on the display name: a formatting change between "JJ Peterka" and
"J.J. Peterka" would otherwise create a second entry that never matches a
price again and sits at tier 4 for the rest of the season. Names are carried
for display only, and refreshed whenever the feed shows a new one.

Hand-added rows are the exception, because nobody knows a player_id offhand.
Those match on a normalised name until the job sees a priced player with that
name, at which point the id is backfilled and they behave like everything
else. A typo in a manual row is invisible — it looks exactly like a player who
is simply not being priced — so unmatched manual rows are reported.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import Config

log = logging.getLogger(__name__)

SOURCE_API = "api"
SOURCE_MANUAL = "manual"


@dataclass
class RosterEntry:
    team: str
    player_id: str
    name: str
    source: str = SOURCE_API
    first_seen: str = ""
    last_priced: str = ""
    _row: int | None = None

    @property
    def is_manual(self) -> bool:
        return self.source == SOURCE_MANUAL

    @property
    def matched(self) -> bool:
        """A manual row is 'matched' once it has acquired a real player_id."""
        return bool(self.player_id)


@dataclass
class RosterChanges:
    # Players new to an existing team list (call-ups, signings). Excludes a
    # team's first load, which is counted in `seeded` instead.
    added: list[RosterEntry] = field(default_factory=list)
    moved: list[tuple[str, str, str]] = field(default_factory=list)  # name, from, to
    seeded: dict[str, int] = field(default_factory=dict)             # team -> players
    manual_linked: list[str] = field(default_factory=list)
    manual_unmatched: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.added or self.moved or self.seeded or self.manual_linked)


def normalise_name(name: str) -> str:
    """Fold a display name to something stable enough to match manual entries.

    Strips accents, punctuation and case, and collapses whitespace, so
    "J.J. Peterka", "JJ Peterka" and "jj  peterka" all agree. Deliberately
    crude — it exists only to give hand-typed rows a fighting chance, not to
    be a general identity system. Real identity is the player_id.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = re.sub(r"[^\w\s]", "", ascii_only)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def _today(cfg: Config) -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(cfg.slate.timezone)).date()


def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def reconcile(
    cfg: Config,
    roster: list[RosterEntry],
    priced_players: list,          # list[Player] seen tonight, with player_id set
    today: date | None = None,
    changes: RosterChanges | None = None,
) -> tuple[list[RosterEntry], RosterChanges]:
    """Fold tonight's priced players into the season roster.

    Nobody is ever removed. The contest sheet looks players up by name, so a
    player who vanished from the list would break its formulas; once a player
    is on a team's list they stay on it for the season, priced or not. The
    only way off a team is a team change, which moves them to the new team.
    """
    today = today or _today(cfg)
    today_iso = today.isoformat()
    changes = changes or RosterChanges()

    by_id = {e.player_id: e for e in roster if e.player_id}
    manual_by_name = {
        normalise_name(e.name): e for e in roster if e.is_manual and not e.player_id
    }

    for p in priced_players:
        pid = getattr(p, "player_id", "") or ""
        if not pid:
            # Without an id we cannot key the player safely. Skip rather than
            # fall back to name matching, which would create duplicates on the
            # first formatting change.
            log.debug("no player_id for %s — not added to roster", p.name)
            continue

        entry = by_id.get(pid)

        if entry is None:
            # A hand-added row may be waiting for exactly this player.
            manual = manual_by_name.get(normalise_name(p.name))
            if manual is not None:
                manual.player_id = pid
                if manual.team != p.team and p.team:
                    changes.moved.append((manual.name, manual.team, p.team))
                    manual.team = p.team
                manual.last_priced = today_iso
                by_id[pid] = manual
                del manual_by_name[normalise_name(p.name)]
                changes.manual_linked.append(p.name)
                continue

            entry = RosterEntry(
                team=p.team,
                player_id=pid,
                name=p.name,
                source=SOURCE_API,
                first_seen=today_iso,
                last_priced=today_iso,
            )
            roster.append(entry)
            by_id[pid] = entry
            changes.added.append(entry)
            continue

        if entry.team != p.team and p.team:
            changes.moved.append((entry.name, entry.team, p.team))
            entry.team = p.team

        # Refresh the display name so feed reformatting does not leave a stale
        # string in the paste your team reads.
        entry.name = p.name
        entry.last_priced = today_iso

    # Hand-added rows that have never matched anything. A typo looks identical
    # to a player who is simply never priced, so surface it rather than let it
    # sit at tier 4 all season.
    warn_cutoff = today - timedelta(days=cfg.roster.manual_unmatched_warn_days)
    for e in roster:
        if e.is_manual and not e.player_id:
            added = _parse_date(e.first_seen)
            if added is None or added <= warn_cutoff:
                changes.manual_unmatched.append(f"{e.name} ({e.team})")

    return roster, changes


def build_paste_rows(
    cfg: Config,
    roster: list[RosterEntry],
    tiers_by_id: dict[str, int],
    tiers_by_name: dict[str, int],
    teams_playing: set[str],
) -> list[list[str]]:
    """Lay the night's slate out as the contest sheet expects.

    Two columns per team (Player Name, Tier), one spacer column between, teams
    left-to-right alphabetically. Within a team: tiers 1-3 ascending, then a
    blank row, then the tier 4s.

    Teams carry different numbers of selectable players. With `align_tier_break`
    on, every column is padded so the blank row falls at the same height and the
    tier 4 blocks line up across the sheet; with it off, each column closes up
    tight and the break lands wherever that team's tier 3 ends.

    Aligned reads far better when scanning across teams — ragged puts one
    column's tier 4 alongside another's gap — and costs only a few blank cells.
    """
    teams = sorted(t for t in teams_playing if t)

    columns: list[tuple[str, list[tuple[str, int]]]] = []
    for team in teams:
        entries = [e for e in roster if e.team == team]

        rated: list[tuple[str, int]] = []
        for e in entries:
            tier = tiers_by_id.get(e.player_id) if e.player_id else None
            if tier is None:
                tier = tiers_by_name.get(normalise_name(e.name))
            rated.append((e.name, tier if tier in (1, 2, 3) else 4))

        selectable = sorted([r for r in rated if r[1] != 4], key=lambda r: (r[1], r[0]))
        excluded = sorted([r for r in rated if r[1] == 4], key=lambda r: r[0])
        columns.append((team, selectable, excluded))

    if cfg.sheets.align_tier_break:
        # Pad every column's selectable block to the longest, so the blank row
        # and the tier 4 block start at the same height in every team.
        tallest = max((len(sel) for _t, sel, _ex in columns), default=0)
        columns = [
            (team, sel + [("", 0)] * (tallest - len(sel)), ex)
            for team, sel, ex in columns
        ]

    columns = [(team, sel + [("", 0)] + ex) for team, sel, ex in columns]

    height = max((len(body) for _t, body in columns), default=0)
    width = len(columns) * 3 - 1 if columns else 0

    rows: list[list[str]] = []

    header = [""] * width
    subhead = [""] * width
    for i, (team, _body) in enumerate(columns):
        header[i * 3] = team
        subhead[i * 3] = "Player Name"
        subhead[i * 3 + 1] = "Tier"
    rows.append(header)
    rows.append(subhead)

    for r in range(height):
        line = [""] * width
        for i, (_team, body) in enumerate(columns):
            if r < len(body):
                name, tier = body[r]
                if name:
                    line[i * 3] = name
                    line[i * 3 + 1] = str(tier)
        rows.append(line)

    return rows


def sync_teams(
    client,
    cfg: Config,
    roster: list[RosterEntry],
    changes: RosterChanges,
    today: date | None = None,
) -> list[RosterEntry]:
    """Bring the roster in line with the feed's team lists, every night.

    Every team, not only tonight's, so a trade is caught the first night the
    feed reflects it even if neither team plays. Three rules:

      * A player on a team list we have never seen is added to that team.
        On a team's very first load that is the whole list (counted in
        `seeded`); after that it is a call-up or signing (in `added`).
      * A player the feed now lists on a different team is MOVED: off the old
        team's list, onto the new one, and reported.
      * A player missing from the feed's lists (sent down, injured, released)
        is left exactly where they are. Nobody is removed.

    A player listed on two teams at once — the feed mid-trade — is left alone
    that night rather than moved, so a glitch cannot bounce them back and
    forth. A team whose list could not be fetched is simply skipped.
    """
    today = today or _today(cfg)
    today_iso = today.isoformat()
    excluded = set(cfg.roster.exclude_positions)

    feed_team: dict[str, str] = {}
    feed_name: dict[str, str] = {}
    ambiguous: set[str] = set()

    for team in client.teams():
        team_name, team_id = team["name"], team["id"]
        try:
            records = client.players_for_team(team_id)
        except Exception:
            log.exception("roster sync: could not fetch %s — skipped tonight", team_name)
            continue
        for rec in records:
            pid = str(rec.get("id") or rec.get("player_id") or "").strip()
            name = str(rec.get("name") or rec.get("display_name")
                       or rec.get("full_name") or "").strip()
            position = str(rec.get("position") or rec.get("position_abbr") or "").upper()
            if not pid or not name or (position and position in excluded):
                continue
            if pid in feed_team and feed_team[pid] != team_name:
                ambiguous.add(pid)
            feed_team[pid] = team_name
            feed_name[pid] = name

    for pid in ambiguous:
        log.warning("roster sync: %s is listed on more than one team — not moved tonight",
                    feed_name.get(pid, pid))
        feed_team.pop(pid, None)

    had_team = {e.team for e in roster}
    by_id = {e.player_id: e for e in roster if e.player_id}
    manual_by_key = {
        (e.team, normalise_name(e.name)): e
        for e in roster if e.is_manual and not e.player_id
    }

    for pid, team in feed_team.items():
        entry = by_id.get(pid)
        if entry is not None:
            if entry.team != team:
                changes.moved.append((entry.name, entry.team, team))
                entry.team = team
            continue

        # A hand-added row on this team with this name: link it, don't duplicate.
        manual = manual_by_key.pop((team, normalise_name(feed_name[pid])), None)
        if manual is not None:
            manual.player_id = pid
            by_id[pid] = manual
            changes.manual_linked.append(manual.name)
            continue

        entry = RosterEntry(
            team=team, player_id=pid, name=feed_name[pid],
            source=SOURCE_API, first_seen=today_iso, last_priced="",
        )
        roster.append(entry)
        by_id[pid] = entry
        if team in had_team:
            changes.added.append(entry)
        else:
            changes.seeded[team] = changes.seeded.get(team, 0) + 1

    for team, n in sorted(changes.seeded.items()):
        log.info("roster: first load of %s — %d player(s)", team, n)
    if changes.added:
        log.info("roster: %d new player(s): %s", len(changes.added),
                 ", ".join(f"{e.name} ({e.team})" for e in changes.added))
    for name, old, new in changes.moved:
        log.info("roster: %s moved %s -> %s", name, old, new)

    return roster
