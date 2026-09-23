"""Slack notifications.

The nightly message is meant to be readable on a phone without opening the
sheet. It answers, in order: did the job run, is the slate trustworthy, and is
it on budget. Anything that needs a decision goes above anything that is just
informational.
"""

from __future__ import annotations

import logging
import os

import requests

from .config import Config
from .slate import SlateBuild
from .tiering import Projection, TierStats, team_concentration, verdict

log = logging.getLogger(__name__)

SLACK_POST_URL = "https://slack.com/api/chat.postMessage"


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _send(cfg: Config, blocks: list[dict], fallback: str) -> None:
    if not cfg.slack.enabled:
        log.info("slack disabled; would have sent: %s", fallback)
        return

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        log.error("SLACK_BOT_TOKEN not set — skipping notification")
        return

    resp = requests.post(
        SLACK_POST_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": cfg.slack.channel, "text": fallback, "blocks": blocks},
        timeout=15,
    )
    body = resp.json() if resp.content else {}
    if not body.get("ok"):
        log.error("slack post failed: %s", body.get("error", resp.text[:200]))


def slate_summary(
    cfg: Config,
    build: SlateBuild,
    stats: dict[int, TierStats],
    projections: dict[str, Projection],
    sheet_url: str,
    tab_name: str,
    roster_changes=None,
    paste_url: str = "",
) -> None:
    adj = projections["sharp_adj"]
    target = cfg.contest.target_expected_points
    read = verdict(adj, cfg)
    delta = adj.expected_points - target

    header = f"Hockey Challenge — {tab_name}"

    estimated = build.estimated
    lines = [
        f"*{build.fixtures_with_market} of {build.fixtures_total} games* priced"
        f"  ·  *{len(build.eligible)} eligible players*",
    ]
    if estimated:
        lines.append(
            f"_{len(estimated)} of those estimated from past games "
            f"({len(build.estimated_fixtures)} unpriced game(s) rescued)_"
        )

    # Decisions first.
    alerts = []

    if build.fixtures_total == 0:
        alerts.append(":no_entry: No games found for this date.")
    elif build.coverage < cfg.slate.coverage_warn_threshold:
        alerts.append(
            f":warning: Only {_fmt_pct(build.coverage)} of games had a market. "
            f"Props post in waves and the early ones skew toward marquee games, "
            f"so this pool likely runs hotter than a full slate would."
        )

    if build.estimated_fixtures:
        share = len(estimated) / len(build.eligible) if build.eligible else 0
        note = (
            f":information_source: Estimated from past games: "
            f"{', '.join(build.estimated_fixtures)}"
        )
        if share > 0.35:
            note += (
                f"\n:warning: {_fmt_pct(share)} of the pool is estimated — the "
                f"projection below is correspondingly less reliable."
            )
        alerts.append(note)

    if build.dropped_recent_absence:
        names = build.dropped_recent_absence
        shown = ", ".join(names[:6]) + (f" +{len(names) - 6} more" if len(names) > 6 else "")
        alerts.append(
            f":heavy_minus_sign: Left out (absent from their last priced game, "
            f"likely scratched): {shown}"
        )

    if build.fallback_notes:
        alerts.append(
            ":hourglass: Fallback declined: "
            + "; ".join(build.fallback_notes)
        )

    if build.unresolved_team:
        names = build.unresolved_team
        shown = ", ".join(names[:6]) + (f" +{len(names) - 6} more" if len(names) > 6 else "")
        alerts.append(
            f":grey_question: Priced but no team in the feed's rosters (left out): {shown}"
        )

    if build.dropped_fixtures:
        alerts.append(
            f":grey_question: Dropped (no market, no usable history): "
            f"{', '.join(build.dropped_fixtures)}"
        )

    # Roster movement. Trades are indistinguishable from a data glitch, so they
    # are reported rather than applied silently.
    if roster_changes is not None:
        if roster_changes.moved:
            moves = ", ".join(
                f"{name} {old} -> {new}" for name, old, new in roster_changes.moved
            )
            alerts.append(f":arrows_counterclockwise: Team change: {moves}")
        if roster_changes.manual_unmatched:
            alerts.append(
                f":warning: Manual roster entries that have never matched a priced "
                f"player (check spelling): "
                f"{', '.join(roster_changes.manual_unmatched)}"
            )
        if roster_changes.manual_linked:
            alerts.append(
                f":white_check_mark: Manual entries now linked: "
                f"{', '.join(roster_changes.manual_linked)}"
            )

    concentration = team_concentration(build.eligible, cfg)
    hot_teams = [
        f"{team} {_fmt_pct(share)}"
        for team, share in concentration.items()
        if share > cfg.slate.team_concentration_warn
    ]
    if hot_teams:
        alerts.append(
            f":warning: Team concentration in T1+T2: {', '.join(hot_teams)}. "
            f"Correlated picks fatten the 3-correct tail, which now pays "
            f"{cfg.contest.payouts[3]}x a single hit."
        )

    for tier, st in sorted(stats.items()):
        if st.pool_size == 0:
            alerts.append(f":warning: Tier {tier} is empty.")

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]

    if alerts:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(alerts)}}
        )

    # Tier table.
    tier_rows = ["```", f"{'Tier':<6}{'Pool':>6}{'Top':>9}{'Eff Top':>10}{'Avg':>9}"]
    for tier, st in sorted(stats.items()):
        marker = "*" if st.diluted else " "
        tier_rows.append(
            f"{tier:<6}{st.pool_size:>6}{_fmt_pct(st.top_prob):>9}"
            f"{_fmt_pct(st.effective_top):>9}{marker}{_fmt_pct(st.avg_prob):>9}"
        )
    tier_rows.append("```")
    if any(st.diluted for st in stats.values()):
        tier_rows.append(
            f"_* pool exceeds the {cfg.contest.max_players_per_tier}-per-tier cap, "
            f"so the top player is not guaranteed to appear_"
        )

    blocks.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(tier_rows)}}
    )

    # Projection.
    sign = "+" if delta >= 0 else ""
    proj_lines = [
        f"*Projected payout:* {adj.expected_points:.2f} pts/user "
        f"(target {target:.2f}, {sign}{delta:.2f})",
        f"*Any-correct:* {_fmt_pct(adj.any_correct)} "
        f"(target {_fmt_pct(cfg.contest.target_any_correct)})",
        f"*Verdict:* {read}",
    ]
    blocks.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(proj_lines)}}
    )

    buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Open slate"},
            "url": sheet_url,
        }
    ]
    if paste_url:
        buttons.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Paste tab"},
                "style": "primary",
                "url": paste_url,
            }
        )
    blocks.append({"type": "actions", "elements": buttons})

    fallback = (
        f"{header}: {len(build.eligible)} players, "
        f"{adj.expected_points:.2f} pts/user — {read}"
    )
    _send(cfg, blocks, fallback)


def drift_summary(
    cfg: Config, tab_name: str, changes: list[str], accuracy_note: str = ""
) -> None:
    """Post-lock drift. Measurement only — the slate is already locked."""
    if not changes:
        text = f"*{tab_name}* — no material odds movement since lock."
    else:
        text = f"*{tab_name}* — {len(changes)} player(s) moved since lock:\n" + "\n".join(
            f"• {c}" for c in changes
        )

    if accuracy_note:
        text += f"\n\n*Fallback estimates:* {accuracy_note}"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Measurement only — the slate locked at generation. "
                            "This feeds the projected-vs-actual reconciliation.",
                }
            ],
        },
    ]
    _send(cfg, blocks, f"{tab_name}: {len(changes)} player(s) moved since lock")


def failure(cfg: Config, stage: str, error: str) -> None:
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":rotating_light: *Slate build failed* during _{stage}_\n```{error[:800]}```",
            },
        }
    ]
    _send(cfg, blocks, f"Slate build failed during {stage}: {error[:200]}")
