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
    added: list[RosterEntry] = field(default_factory=list)
    moved: list[tuple[str, str, str]] = field(default_factory=list)  # name, from, to
    pruned: list[RosterEntry] = field(default_factory=list)
    manual_linked: list[str] = field(default_factory=list)
    manual_unmatched: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.added or self.moved or self.pruned or self.manual_linked)


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
    teams_playing: set[str],
    today: date | None = None,
) -> tuple[list[RosterEntry], RosterChanges]:
    """Fold tonight's priced players into the season roster.

    Returns the updated roster and a record of what changed. Only teams playing
    tonight are touched — a team idle tonight tells us nothing about its roster,
    so its entries are left alone and, importantly, not pruned.
    """
    today = today or _today(cfg)
    today_iso = today.isoformat()
    changes = RosterChanges()

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

    # Prune stale api-sourced entries, but only for teams that played tonight —
    # otherwise an idle team's whole roster ages out over a long break.
    cutoff = today - timedelta(days=cfg.roster.prune_after_days)
    kept: list[RosterEntry] = []
    for e in roster:
        if e.is_manual or e.team not in teams_playing:
            kept.append(e)
            continue
        last = _parse_date(e.last_priced) or _parse_date(e.first_seen)
        if last is None or last >= cutoff:
            kept.append(e)
        else:
            changes.pruned.append(e)

    # Hand-added rows that have never matched anything. A typo looks identical
    # to a player who is simply never priced, so surface it rather than let it
    # sit at tier 4 all season.
    warn_cutoff = today - timedelta(days=cfg.roster.manual_unmatched_warn_days)
    for e in kept:
        if e.is_manual and not e.player_id:
            added = _parse_date(e.first_seen)
            if added is None or added <= warn_cutoff:
                changes.manual_unmatched.append(f"{e.name} ({e.team})")

    return kept, changes


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


def seed_teams(
    client,
    cfg: Config,
    roster: list[RosterEntry],
    team_ids: dict[str, str],
    teams_playing: set[str],
    today: date | None = None,
) -> list[RosterEntry]:
    """Populate any playing team that has no roster entries yet.

    Runs once per team, the first night they appear. Seeded players carry no
    last_priced date, so they are subject to the normal prune window — a
    seeded player who never gets priced falls off after prune_after_days
    rather than lingering all season.
    """
    today = today or _today(cfg)
    today_iso = today.isoformat()

    have_team = {e.team for e in roster}
    known_ids = {e.player_id for e in roster if e.player_id}
    excluded = set(cfg.roster.exclude_positions)

    for team in sorted(teams_playing):
        if team in have_team:
            continue
        team_id = team_ids.get(team)
        if not team_id:
            log.warning("no team id for %s — cannot seed its roster", team)
            continue

        try:
            records = client.players_for_team(team_id)
        except Exception:
            log.exception("roster seed failed for %s", team)
            continue

        added = 0
        for rec in records:
            pid = str(rec.get("id") or rec.get("player_id") or "").strip()
            name = str(
                rec.get("name")
                or rec.get("display_name")
                or rec.get("full_name")
                or ""
            ).strip()
            if not pid or not name or pid in known_ids:
                continue

            position = str(rec.get("position") or rec.get("position_abbr") or "").upper()
            if position and position in excluded:
                continue

            roster.append(
                RosterEntry(
                    team=team,
                    player_id=pid,
                    name=name,
                    source=SOURCE_API,
                    first_seen=today_iso,
                    last_priced="",
                )
            )
            known_ids.add(pid)
            added += 1

        log.info("seeded %d player(s) for %s", added, team)

    return roster
