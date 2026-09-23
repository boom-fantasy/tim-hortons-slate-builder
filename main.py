"""CLI entrypoint.

Commands:

  discover   Print valid league slugs and market keys. Run this FIRST — the
             league and market values in config.yaml are guesses and an unknown
             slug returns an empty list rather than an error.

  inspect    Pull one game's raw odds payload and print it. Use this to confirm
             the field names in optic_odds.py / slate.py match the live schema.

  build      The nightly job. Pull tomorrow's odds, tier them, write a tab,
             post to Slack.

  drift      Compare a written slate against current odds. Measurement only —
             the slate is locked at generation and cannot be changed.

  simulate   Run the projection against a CSV of odds with no network and no
             writes. Useful for retuning tier bands offline.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config as config_mod
from . import notify, slate as slate_mod
from .optic_odds import OpticOddsClient
from .tiering import Player, enrich, project_all, team_concentration, tier_stats, verdict

log = logging.getLogger("slate-builder")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _outside_run_hour(args, cfg) -> bool:
    """True when a `--scheduled` run has fired in the wrong local hour.

    Railway cron is UTC-only, so a fixed local time is scheduled at BOTH UTC
    hours it can fall on (e.g. 02:00 and 03:00 UTC for 10pm Eastern), and this
    lets through only the one that matches the local hour today. The other
    exits immediately. That keeps the run at the same local time across
    daylight-saving changes with no schedule edits.

    Only the hour is checked, so a cron that starts a few minutes late still
    runs. Manual runs (no --scheduled) are never blocked.
    """
    if not getattr(args, "scheduled", False):
        return False

    wanted = cfg.slate.build_hour if args.command == "build" else cfg.slate.drift_hour
    now = datetime.now(ZoneInfo(cfg.slate.timezone))
    if now.hour == wanted:
        return False

    log.info(
        "scheduled %s: local time is %s %s, runs at %02d:xx — skipping this trigger "
        "(the other UTC trigger covers the other side of daylight saving)",
        args.command, now.strftime("%H:%M"), cfg.slate.timezone, wanted,
    )
    return True


def _target_date(cfg, explicit: str | None) -> str:
    if explicit:
        return explicit
    now = datetime.now(ZoneInfo(cfg.slate.timezone))
    return (now + timedelta(days=1)).strftime("%Y-%m-%d")


def _print_projection_table(cfg, stats, projections) -> None:
    target = cfg.contest.target_expected_points

    print("\nTier pools")
    print(f"  {'Tier':<6}{'Pool':>6}{'Top':>10}{'Eff Top':>10}{'Avg':>10}{'Min':>10}")
    for tier, st in sorted(stats.items()):
        flag = " *" if st.diluted else "  "
        print(
            f"  {tier:<6}{st.pool_size:>6}{st.top_prob:>9.1%}"
            f"{st.effective_top:>9.1%}{flag}{st.avg_prob:>9.1%}{st.min_prob:>10.1%}"
        )
    if any(st.diluted for st in stats.values()):
        print(f"  * pool exceeds the {cfg.contest.max_players_per_tier}-per-tier cap")

    print("\nProjections")
    header = f"  {'':<22}" + "".join(f"{p.label:>13}" for p in projections.values())
    print(header)

    rows = [
        ("p1 / p2 / p3", lambda p: f"{p.p1:.0%}/{p.p2:.0%}/{p.p3:.0%}"),
        ("Any correct", lambda p: f"{p.any_correct:.1%}"),
        ("E[correct]", lambda p: f"{p.expected_correct:.3f}"),
        ("E[points]", lambda p: f"{p.expected_points:.2f}"),
        ("vs target", lambda p: f"{p.expected_points - target:+.2f}"),
    ]
    for label, fn in rows:
        print(f"  {label:<22}" + "".join(f"{fn(p):>13}" for p in projections.values()))

    for k in range(4):
        label = f"P({k} correct)"
        actual = "".join(
            f"{p.outcome_probs[k]:>13.1%}" if k < len(p.outcome_probs) else f"{'-':>13}"
            for p in projections.values()
        )
        tgt = cfg.contest.targets[k]
        print(f"  {label:<22}{actual}   (target {tgt:.1%})")

    print(f"\n  Target budget: {target:.2f} pts/user")
    print(f"  Verdict:       {verdict(projections['sharp_adj'], cfg)}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_discover(args, cfg) -> int:
    client = OpticOddsClient(cfg)

    sport = args.sport or cfg.optic_odds.sport
    print(f"\nLeagues for sport={sport!r}:\n")
    for league in client.leagues(sport):
        name = league.get("name", "?")
        lid = league.get("id", "?")
        print(f"  {name:<40} id={lid}")

    print(
        f"\nMarkets for league={cfg.optic_odds.league!r} "
        f"at {cfg.odds.sportsbook!r}:\n"
    )
    try:
        markets = client.markets(sport, cfg.optic_odds.league, cfg.odds.sportsbook)
        if not markets:
            print("  (none — the league slug above is probably wrong)")
        for m in markets:
            name = m.get("name") if isinstance(m, dict) else str(m)
            mid = m.get("id") if isinstance(m, dict) else ""
            flag = ""
            blob = f"{name} {mid}".lower()
            if "goal" in blob and "goalie" not in blob:
                flag = "   <-- likely goalscorer market"
            print(f"  {str(name):<45} id={mid}{flag}")
    except Exception as exc:
        print(f"  market lookup failed: {exc}")

    print(
        "\nPaste the correct league id and market id into config.yaml "
        "under optic_odds.\n"
    )
    return 0


def cmd_inspect(args, cfg) -> int:
    import json

    client = OpticOddsClient(cfg)
    date_iso = _target_date(cfg, args.date)

    fixtures = client.fixtures_for_date(date_iso)
    if not fixtures:
        print(f"No fixtures for {date_iso}. Check the league slug with `discover`.")
        return 1

    print(f"\n{len(fixtures)} fixture(s) for {date_iso}:")
    for f in fixtures:
        print(f"  {f.id}  {f.away_team} @ {f.home_team}  ({f.start_date})")

    target = fixtures[0]
    print(f"\nRaw odds rows for {target.away_team} @ {target.home_team}:\n")
    rows = client.goalscorer_odds(target)

    if not rows:
        print("  (empty — the market key is probably wrong; run `discover`)")
        return 1

    print(f"  {len(rows)} row(s). First 3 verbatim:\n")
    for row in rows[:3]:
        print(json.dumps(row, indent=2)[:1500])
        print()

    keys: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            keys.update(row.keys())
    print(f"  All keys seen across rows: {sorted(keys)}\n")
    return 0


def _load_price_history(cfg):
    """The stored price list, or None if it could not be read.

    Only loaded when the fallback is on. None is distinct from an empty list:
    the fallback reports "store unreadable" rather than "no history yet".
    """
    if not cfg.fallback.enabled:
        return []
    try:
        from .sheets import SheetsWriter

        return SheetsWriter(cfg).read_price_history()
    except Exception:
        log.exception("could not read the price store — fallback will not run")
        return None


def _alert_failure(args, cfg, stage: str, error: str) -> None:
    """Post a build failure to Slack — except on a dry run, which only logs.

    Dry runs are how the service gets tested, and a test should never land in
    the team channel.
    """
    if getattr(args, "dry_run", False):
        log.info("dry run: not posting %s failure to Slack", stage)
        return
    notify.failure(cfg, stage, error)


def cmd_build(args, cfg) -> int:
    date_iso = _target_date(cfg, args.date)
    tab_name = datetime.strptime(date_iso, "%Y-%m-%d").strftime(
        cfg.sheets.tab_name_format
    )

    log.info("building slate for %s -> tab %r", date_iso, tab_name)

    try:
        client = OpticOddsClient(cfg)
        build = slate_mod.build(
            client, cfg, date_iso, price_history=_load_price_history(cfg)
        )
    except Exception as exc:
        log.exception("odds pull failed")
        _alert_failure(args, cfg, "odds pull", str(exc))
        return 1

    eligible = build.eligible
    log.info(
        "%d player(s) pulled, %d eligible, %d outside the bands",
        len(build.players), len(eligible), len(build.out_of_band),
    )

    if not eligible:
        msg = (
            f"No eligible players for {date_iso}. "
            f"{build.fixtures_total} fixture(s), "
            f"{build.fixtures_with_market} with a market."
        )
        log.error(msg)
        _alert_failure(args, cfg, "slate assembly", msg)
        return 1

    stats = tier_stats(eligible, cfg)
    projections = project_all(stats, cfg)

    if args.dry_run:
        print(f"\nDRY RUN — {date_iso} — nothing written\n")
        print(
            f"  Games: {build.fixtures_with_market}/{build.fixtures_total} priced"
            f"   Players: {len(eligible)} eligible, {len(build.out_of_band)} out of band"
        )
        if build.estimated_fixtures:
            print(
                f"  Estimated: {len(build.estimated)} player(s) across "
                f"{len(build.estimated_fixtures)} unpriced game(s) "
                f"({', '.join(build.estimated_fixtures)})"
            )
        if build.dropped_recent_absence:
            print(
                f"  Left out (scratched last game): "
                f"{len(build.dropped_recent_absence)}"
            )
        for note in build.fallback_notes:
            print(f"  Not estimated: {note}")
        if build.unresolved_team:
            print(f"  No team found (left out): {', '.join(build.unresolved_team)}")
        if build.dropped_fixtures:
            print(f"  Dropped: {', '.join(build.dropped_fixtures)}")
        _print_projection_table(cfg, stats, projections)

        conc = team_concentration(eligible, cfg)
        if conc:
            print("\n  Team concentration (T1+T2):")
            for team, share in list(conc.items())[:6]:
                flag = "  <-- above threshold" if share > cfg.slate.team_concentration_warn else ""
                print(f"    {team:<8}{share:>7.1%}{flag}")
        print()
        return 0

    try:
        from .sheets import SheetsWriter

        writer = SheetsWriter(cfg)
        result = writer.write_slate(
            players=eligible,
            date_iso=date_iso,
            tab_name=tab_name,
            games_count=build.fixtures_with_market,
            overwrite=args.overwrite,
        )
    except Exception as exc:
        log.exception("sheet write failed")
        _alert_failure(args, cfg, "sheet write", str(exc))
        return 1

    # Season roster + paste tab. Failures here must not lose the slate that was
    # already written, so each is guarded separately.
    roster_changes = None
    paste_url = ""
    if cfg.roster.enabled:
        try:
            from . import roster as roster_mod
            from .slate import _team_id_index

            teams_playing = {p.team for p in build.players if p.team}
            stored = writer.read_roster()

            if cfg.roster.seed_from_api:
                stored = roster_mod.seed_teams(
                    client, cfg, stored, _team_id_index(client, cfg), teams_playing
                )

            stored, roster_changes = roster_mod.reconcile(
                cfg, stored, build.players, teams_playing
            )
            writer.write_roster(stored)

            tiers_by_id = {
                p.player_id: p.tier for p in build.players if p.player_id and p.tier
            }
            tiers_by_name = {
                roster_mod.normalise_name(p.name): p.tier
                for p in build.players if p.tier
            }
            rows = roster_mod.build_paste_rows(
                cfg, stored, tiers_by_id, tiers_by_name, teams_playing
            )
            paste_name = datetime.strptime(date_iso, "%Y-%m-%d").strftime(
                cfg.sheets.paste_tab_format
            )
            paste_url = writer.write_paste_tab(rows, paste_name, overwrite=args.overwrite)
        except Exception:
            log.exception("roster/paste step failed (slate was still written)")

    # Price store for the fallback. Recorded every night whether or not the
    # fallback is enabled, so it has history by the time it is switched on.
    # Pruned here, alongside the roster.
    try:
        from . import price_store

        stored = writer.read_price_history()
        merged, added = price_store.merge(stored, price_store.rows_from_build(build, cfg))
        kept, pruned = price_store.prune(merged, cfg)
        writer.write_price_history(kept)
        log.info(
            "price store: +%d row(s), %d pruned, %d total", len(added), pruned, len(kept)
        )
    except Exception:
        log.exception("price store update failed (slate was still written)")

    if build.estimated:
        try:
            writer.append_estimates(build.estimated, date_iso)
        except Exception:
            log.exception("estimate logging failed (slate was still written)")

    try:
        notify.slate_summary(
            cfg, build, stats, projections, result.spreadsheet_url, tab_name,
            roster_changes=roster_changes, paste_url=paste_url,
        )
    except Exception:
        log.exception("slack notification failed (slate was still written)")

    log.info("done: %s (%d rows)", result.tab_name, result.rows_written)
    return 0


def cmd_drift(args, cfg) -> int:
    """Morning run on game day. Collects and measures; changes nothing.

    In order of importance:
      1. Record today's prices in the price store. Games unpriced at 10pm often
         have a market by now, and this is the only capture the fallback will
         ever get of them — so it runs first and does not depend on anything
         else succeeding. In particular it runs when last night's build wrote
         no slate at all, which is exactly the night it matters most.
      2. Score last night's fallback estimates against real prices.
      3. Compare the locked slate against current odds (scratches, big moves).

    Results go to the log. Slack only if drift_check.slack is on.
    """
    from .sheets import SheetsError, SheetsWriter

    date_iso = args.date or datetime.now(ZoneInfo(cfg.slate.timezone)).strftime("%Y-%m-%d")
    tab_name = datetime.strptime(date_iso, "%Y-%m-%d").strftime(cfg.sheets.tab_name_format)

    writer = SheetsWriter(cfg)
    client = OpticOddsClient(cfg)
    # Real prices only. With the fallback on, an estimate would be compared
    # against itself below and always score as a match.
    build = slate_mod.build(client, cfg, date_iso, use_fallback=False)
    current = {p.name: p.american_odds for p in build.players}

    # 1. Price store. Append-only: pruning happens in build.
    try:
        from . import price_store

        stored = writer.read_price_history()
        _merged, added = price_store.merge(stored, price_store.rows_from_build(build, cfg))
        writer.append_price_history(added)
        log.info("price store: +%d row(s) from the morning pull", len(added))
    except Exception:
        log.exception("price store update failed")

    # 2. Fill in what the estimates were actually worth. This is the only
    # feedback the historical fallback ever gets — without it the estimates
    # look plausible and nothing contradicts them.
    accuracy_note = ""
    try:
        pending = [
            e for e in writer.read_estimate_log(date_iso) if not e["actual_odds"]
        ]
    except Exception:
        log.exception("could not read estimate log")
        pending = []

    if pending:
        updates = []
        matched = 0
        for entry in pending:
            actual = current.get(entry["player"])
            if actual is None:
                updates.append((entry["_row"], "not priced", "", ""))
                continue
            actual_tier = cfg.tier_for(actual)
            try:
                est_tier = int(entry["est_tier"])
            except (TypeError, ValueError):
                est_tier = None
            match = "yes" if (actual_tier is not None and actual_tier == est_tier) else "no"
            if match == "yes":
                matched += 1
            updates.append((entry["_row"], actual, actual_tier or "out of band", match))

        try:
            writer.fill_estimate_actuals(updates)
            scored = [u for u in updates if u[3] in ("yes", "no")]
            if scored:
                accuracy_note = (
                    f"{matched}/{len(scored)} estimated players landed in the "
                    f"right tier ({matched / len(scored):.0%})"
                )
                log.info("estimate accuracy: %s", accuracy_note)
        except Exception:
            log.exception("could not write estimate actuals")

    # 3. Movement since lock. No slate tab means last night's build wrote
    # nothing (no eligible players, or it failed) — nothing to compare, but
    # not an error for this run, since steps 1 and 2 have already done their
    # job.
    try:
        locked_rows = writer.read_slate(tab_name)
    except SheetsError:
        log.warning("no slate tab %r — skipping the movement check", tab_name)
        locked_rows = []

    changes: list[str] = []
    log_rows: list[list] = []
    threshold = cfg.drift_check.move_threshold
    marker = cfg.fallback.marker

    for shown_name, team, _opp, old_odds in locked_rows:
        # Estimated players carry the marker in the slate tab ("X (est)").
        # Strip it to look up the real price, and keep them apart from live
        # players: their "move" is estimation error, not market drift, and is
        # scored in the estimate log instead.
        estimated = bool(marker) and shown_name.endswith(marker)
        name = shown_name[: -len(marker)] if estimated else shown_name
        source = "est" if estimated else "live"

        old_tier = cfg.tier_for(old_odds)
        new_odds = current.get(name)
        if new_odds is None:
            flag = "not_priced"
            log_rows.append([date_iso, name, team, source, old_odds, old_tier or "",
                             "", "", "", flag])
            if not estimated:
                changes.append(f"*{name}* — no longer priced (likely scratched)")
            continue

        old_p = 100.0 / (old_odds + 100.0)
        new_p = 100.0 / (new_odds + 100.0)
        move = (new_p - old_p) / old_p if old_p > 0 else 0.0
        new_tier = cfg.tier_for(new_odds)
        flag = "moved" if abs(move) >= threshold else ""
        log_rows.append([date_iso, name, team, source, old_odds, old_tier or "",
                         new_odds, new_tier or "out of band", round(move, 4), flag])

        if flag and not estimated:
            direction = "shorter" if move > 0 else "longer"
            changes.append(
                f"*{name}* — {old_odds:+d} to {new_odds:+d} ({direction}, {move:+.0%})"
            )

    if locked_rows:
        live = sum(1 for r in log_rows if r[3] == "live")
        log.info("drift: %d of %d live locked player(s) moved or unpriced", len(changes), live)
    for line in changes:
        log.info("drift: %s", line.replace("*", ""))

    # Kept for the season: every locked player, not just the flagged ones, so
    # "how often does a locked slate go stale" has a denominator.
    if log_rows:
        try:
            writer.append_drift_log(log_rows, date_iso)
        except Exception:
            log.exception("could not write the drift log")

    if cfg.drift_check.slack and locked_rows:
        try:
            notify.drift_summary(cfg, tab_name, changes, accuracy_note)
        except Exception:
            log.exception("slack notification failed")

    return 0


def cmd_simulate(args, cfg) -> int:
    """Project a slate from a CSV. No network, no writes.

    CSV columns: player,team,opponent,odds

    This is the loop for retuning tier bands — edit config.yaml, re-run, see
    what it does to the projected payout, repeat.
    """
    players: list[Player] = []
    with open(args.csv, newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                players.append(
                    Player(
                        name=row.get("player", "").strip(),
                        team=row.get("team", "").strip(),
                        opponent=row.get("opponent", "").strip(),
                        american_odds=int(float(row["odds"])),
                    )
                )
            except (KeyError, ValueError):
                log.warning("skipping unparseable row: %s", row)

    if not players:
        log.error("no usable rows in %s", args.csv)
        return 1

    enrich(players, cfg)
    eligible = [p for p in players if p.eligible]

    print(f"\n{len(players)} player(s) in file, {len(eligible)} inside the tier bands")
    if len(eligible) < len(players):
        out = [p for p in players if not p.eligible]
        shorter = sum(1 for p in out if p.american_odds < cfg.tiers[0].min_odds)
        longer = len(out) - shorter
        print(
            f"  {shorter} shorter than +{cfg.tiers[0].min_odds}, "
            f"{longer} longer than +{cfg.tiers[-1].max_odds}"
        )

    if not eligible:
        return 1

    stats = tier_stats(eligible, cfg)
    projections = project_all(stats, cfg)
    _print_projection_table(cfg, stats, projections)
    print()
    return 0


# ---------------------------------------------------------------------------

def cmd_accuracy(args, cfg) -> int:
    """Roll up the estimate log.

    Three questions this answers, in order of how much they should change what
    you do:

      1. Does the fallback put players in the right tier? If it is well under
         ~80%, shipping estimated players is worse than dropping their game.
      2. Does it run systematically long or short? A consistent signed bias is
         correctable with an offset; scatter around zero is not.
      3. Is a one-game estimate meaningfully worse than a five-game one? That
         is the direct test of the "use even one game rather than lose a star"
         call.
    """
    from .sheets import SheetsWriter
    from .tiering import american_to_implied

    writer = SheetsWriter(cfg)
    entries = writer.read_estimate_log()

    scored = [e for e in entries if e["tier_match"] in ("yes", "no")]
    unscored = len(entries) - len(scored)

    if not scored:
        print(
            f"\nNo scored estimates yet. {len(entries)} row(s) logged, "
            f"{unscored} awaiting a drift run.\n"
        )
        return 0

    matched = sum(1 for e in scored if e["tier_match"] == "yes")
    print(f"\nEstimate accuracy — {len(scored)} scored, {unscored} pending\n")
    print(f"  Tier match rate: {matched}/{len(scored)} ({matched / len(scored):.0%})")

    # Signed probability error: positive means the estimate was too generous.
    errors = []
    for e in scored:
        try:
            est = american_to_implied(int(e["est_odds"]))
            act = american_to_implied(int(e["actual_odds"]))
        except (TypeError, ValueError):
            continue
        errors.append(est - act)

    if errors:
        mean = sum(errors) / len(errors)
        mean_abs = sum(abs(x) for x in errors) / len(errors)
        direction = "too generous" if mean > 0 else "too harsh"
        print(f"  Mean signed error: {mean:+.1%} ({direction})")
        print(f"  Mean absolute error: {mean_abs:.1%}")
        if abs(mean) > 0.02:
            print(
                f"    -> a consistent bias this size is worth correcting with an "
                f"offset rather than living with"
            )

    # Accuracy by how many prior games fed the estimate.
    by_games: dict[str, list[int]] = {}
    for e in scored:
        key = str(e["games_used"] or "?")
        by_games.setdefault(key, []).append(1 if e["tier_match"] == "yes" else 0)

    if len(by_games) > 1:
        print("\n  By games used:")
        for key in sorted(by_games, key=lambda k: (k == "?", k)):
            hits = by_games[key]
            print(
                f"    {key} game(s): {sum(hits)}/{len(hits)} "
                f"({sum(hits) / len(hits):.0%})"
            )

    # Which direction the misses go.
    misses = [e for e in scored if e["tier_match"] == "no"]
    if misses:
        print(f"\n  {len(misses)} miss(es). Most recent:")
        def _tier_label(v) -> str:
            text = str(v).strip()
            return f"T{text}" if text.isdigit() else (text or "-")

        for e in misses[-5:]:
            print(
                f"    {e['date']}  {e['player']:<22} "
                f"est {e['est_odds']:>6} ({_tier_label(e['est_tier'])})  ->  "
                f"actual {e['actual_odds']:>6} ({_tier_label(e['actual_tier'])})"
            )

    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slate-builder")
    parser.add_argument("-c", "--config", default=None, help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="list league slugs and market keys")
    p.add_argument("--sport", default=None)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("inspect", help="dump one game's raw odds payload")
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default: tomorrow)")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("build", help="build and write a slate")
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default: tomorrow)")
    p.add_argument("--dry-run", action="store_true", help="print, do not write")
    p.add_argument("--overwrite", action="store_true", help="replace an existing tab")
    p.add_argument("--scheduled", action="store_true",
                   help="cron mode: run only during slate.build_hour local time")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("drift", help="compare a locked slate against current odds")
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    p.add_argument("--scheduled", action="store_true",
                   help="cron mode: run only during slate.drift_hour local time")
    p.set_defaults(func=cmd_drift)

    p = sub.add_parser("accuracy", help="roll up the estimate-accuracy log")
    p.set_defaults(func=cmd_accuracy)

    p = sub.add_parser("simulate", help="project a slate from a CSV, offline")
    p.add_argument("csv", help="CSV with player,team,opponent,odds")
    p.set_defaults(func=cmd_simulate)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        print(f"\nconfig error:\n{exc}\n", file=sys.stderr)
        return 2

    if _outside_run_hour(args, cfg):
        return 0

    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
