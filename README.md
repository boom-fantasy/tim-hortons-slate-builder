# Hockey Challenge — Slate Builder

Nightly job that pulls tomorrow's NHL anytime-goalscorer odds from OpticOdds,
assigns players to tiers, writes a dated tab into the master calibration
workbook, and posts a summary to Slack.

Every tunable lives in `config.yaml`. Changing a tier band or a payout does not
require a code change or a logic redeploy — edit, commit, done. Commit each
change, because git history is what tells you what the bands were on a given
night when you reconcile projected against actual later in the season.

---

## Before the first run

Two values in `config.yaml` are guesses and **will silently produce an empty
slate if wrong**, because OpticOdds returns an empty list rather than an error
for an unknown league slug or market key:

```
optic_odds.league    # usa_-_nhl        <- VERIFY
optic_odds.market    # player_goals     <- VERIFY
```

Verify both:

```bash
python -m src.main discover --sport hockey
```

That prints every league slug for hockey and every market DraftKings publishes
for the configured league, flagging anything that looks like the goalscorer
market. Paste the real values into `config.yaml`.

Then confirm the response schema matches what the parser expects. This does NOT
need to wait for the season — point it at any completed game and you get a real
payload:

```bash
python -m src.main inspect --date 2026-06-10   # last season's playoffs
```

Confirm in particular that `player_id` is present. The season roster is keyed on
it, and without it nobody ever gets added.

This dumps one game's raw odds rows verbatim plus every key seen across rows.
The parser in `optic_odds.py` and `slate.py` tries a list of candidate field
names and raises a loud error naming what it looked for and what was actually
there — so if a field name is different, `inspect` shows you the fix in one
round trip.

---

## Setup

### 1. Google service account

A service account, not a personal Google account: it survives password changes
and staff turnover, it does not route through anyone's 2FA, and the credentials
drop cleanly into Railway variables.

1. In the GCP project that already holds the OpticOdds secret, create a service
   account and generate a JSON key.
2. Enable the Google Sheets API on that project.
3. Open the master workbook and share it with the service account's email
   address (`...@....iam.gserviceaccount.com`) as an **Editor**.
4. Put the whole JSON blob in the `GOOGLE_SERVICE_ACCOUNT_JSON` variable.

Then set `sheets.spreadsheet_id` in `config.yaml` — it is the long id in the
workbook URL between `/d/` and `/edit`.

### 2. Slack

Create an app with the `chat:write` scope, install it, invite the bot to the
channel, and set `SLACK_BOT_TOKEN`. Set the channel in `config.yaml`.

### 3. Environment

See `.env.example`. Three variables: `OPTICODDS_API_KEY`,
`GOOGLE_SERVICE_ACCOUNT_JSON`, `SLACK_BOT_TOKEN`.

### 4. Railway

Two cron services off the same image:

| Service | Cron (UTC) | ET | Command |
|---|---|---|---|
| `slate-build` | `0 2,3 * * *` | 10:00 pm | `python -m src.main build --scheduled` |
| `slate-drift` | `30 13,14 * * *` | 9:30 am | `python -m src.main drift --scheduled` |

Railway cron is UTC-only, so each service fires at both UTC hours its Eastern
time can fall on. `--scheduled` makes the job exit immediately unless the local
hour matches `slate.build_hour` / `slate.drift_hour`, so exactly one of the two
triggers does the work each day, and the times hold across daylight saving
with nothing to change in March or November. The skipped trigger shows up in
Railway as a run that exits in a second or two. Manual runs (no `--scheduled`)
are never blocked.

---

## Commands

```bash
python -m src.main discover              # league slugs + market keys
python -m src.main inspect               # dump raw odds payload
python -m src.main build --dry-run       # full pull + projection, writes nothing
python -m src.main build                 # the nightly job
python -m src.main build --date 2026-10-14 --overwrite
python -m src.main drift                 # post-lock movement + score estimates
python -m src.main accuracy              # roll up the estimate-accuracy log
python -m src.main simulate slate.csv    # offline projection from a CSV
```

`simulate` takes a CSV with `player,team,opponent,odds` and needs no network
and no credentials. It is the loop for retuning bands: edit `config.yaml`,
re-run, see what it does to the projected payout, repeat.

---

## What the model does

1. **Odds → probability.** Implied probability from American odds, then strip
   `odds.devig` (default 6%) to remove the book's overround.
2. **Probability → tier.** First band in `config.yaml` whose range contains the
   price. Anything outside every band is excluded.
3. **Pool → effective top.** Only `max_players_per_tier` players make the
   contest. When the pool is larger, the strongest player is not guaranteed to
   appear, so the top-of-tier probability a sharp user actually sees is blended
   toward the pool average:

   ```
   effective_top = (cap/N) * top + (1 - cap/N) * avg
   ```

   This is deliberately conservative — when the top player is dropped the
   replacement is the second-best, not the average — so the projection
   understates payout slightly, which is the safe direction for a budget.
4. **Effective top → payout.** Outcome distribution for three independent
   picks, weighted by the payout schedule.

Three user models are projected: **sharp** (picks the pool top), **sharp
(adj)** (accounts for the cap — the honest number, and what the verdict uses),
and **average** (picks a tier-average player; unaffected by dilution, since the
expected average of a random sample equals the population average).

Tiers are computed twice on purpose — in Python for the Slack summary, and
independently by the sheet's own formulas from the same odds. If they ever
disagree, one has drifted and you want to know.

---

## The season roster and the paste tab

The contest sheet expects **every** player on a team's list every night, with
non-participants marked tier 4 rather than left out. A slate built only from who
got priced would change length nightly and silently drop anyone scratched — so
the job keeps a season-long roster and projects each night's tiers onto it.

**Identity is the player_id, never the name.** A formatting change between
"JJ Peterka" and "J.J. Peterka" would otherwise fork one player into two
entries, one of which never matches a price again and sits at tier 4 for the
rest of the season.

**Seeding.** On a team's first appearance the roster is filled from
`/api/v3/players` so early slates already carry their tier 4s instead of
accreting them over weeks. Goalies are excluded — they never carry a
goalscorer price.

**Pruning.** An api-sourced player with no price for `prune_after_days` (30)
drops off the list, and is re-added automatically the first night they are
priced again. Only teams that played that night are pruned, so a team on a long
break does not age out. Pruning never affects whether someone is *correctly*
tier 4 — anyone unpriced is 4 regardless — it only controls list length.

**Manual additions.** The `_roster` tab is safe to hand-edit: add a row with the
team and player name, leave `player_id` blank, set `source` to `manual`. Those
rows are never pruned and never overwritten. They match on a normalised name
until the job sees a priced player with that name, then the id is backfilled and
they behave like any other entry.

The catch is that a typo'd manual row looks exactly like a player who is simply
never priced — both sit at tier 4 forever. So entries that have never matched
anything after `manual_unmatched_warn_days` (14) are reported in Slack.

**Trades** are detected when a player_id turns up on a different team, and are
reported rather than applied silently — a genuine trade and a data glitch look
identical from here.

### The paste tab

Written fresh each night as `Paste YYYY-MM-DD`, laid out to match the
contest-creation sheet: two columns per team (Player Name, Tier), a spacer
column between, teams left-to-right alphabetically by location. Within a team,
tiers 1-3 ascending, a blank row, then the tier 4s.

```
     A                  B      C   D                  E      F   G
 1   Anaheim Ducks                 Boston Bruins             
 2   Player Name        Tier       Player Name        Tier   
 3   Chris Kreider      1          Casey Mittelstadt  1      
 4   Mason McTavish     1          Elias Lindholm     1      
 5   Troy Terry         1          Charlie McAvoy     2      
 6   Jackson LaCombe    2          Hampus Lindholm    2      
 7   John Carlson       2          Mark Kastelic      3      
 8   Jacob Trouba       3                                    
 9                                                           
10   Beckett Sennecke   4          David Pastrnak     4      
11   Radko Gudas        4          Pavel Zacha        4      
12   Ryan Poehling      4                                    
```

`sheets.align_tier_break` pads each team's selectable block so the blank row
lands at the same height everywhere. With it off, columns close up tight and one
team's tier 4 ends up alongside another's gap, which is hard to scan.

The `(est)` marker is deliberately **not** applied here — it is on the
calibration tab for review, but appending it to a name your team pastes into a
lookup would break the match.

---

## Unpriced games: the historical fallback

**Currently disabled** (`fallback.enabled: false`) for the start of the season.
Its history comes from our own price store, which only starts filling on
opening night, so no team has enough stored games until mid-October. Turn it on
then. Until then an unpriced game drops entirely, which is more honest than a
bad tier.

Goalscorer props often are not posted at 10pm for next-day games. Rather than
losing those games entirely, the job estimates their players from recent prices.

**It runs at the game level only.** The two kinds of absence mean opposite
things:

| Situation | Meaning | Action |
|---|---|---|
| Game priced, player missing | The book judged every skater it expects to dress. Player is scratched or hurt. | Drop |
| Game not priced at all | No information about any individual player. | Estimate |

For each unpriced game, the job reads both teams' last `fallback.lookback_games`
games from the price store, averages each player's price across them, and
converts back to a tier. A player priced in only one of those games still gets
estimated. One noisy price beats losing a star.

### The price store

The API key has no access to `/fixtures/odds/historical`, so the service keeps
its own record in a hidden `_price_history` tab (`sheets.price_history_tab`):
`date, fixture_id, player_id, player, team, american_odds`.

* **Both runs write to it**, whether or not the fallback is enabled. `build`
  (10pm) records tomorrow's priced games. `drift` (9:30am) records today's games
  again. That morning capture is what matters: a game unpriced at 10pm is
  exactly the one the fallback will later have no other record of.
* **One row per `(fixture_id, player_id)`, earliest capture wins.** The 10pm
  price is kept over the 9:30am one for the same player. A later capture only
  adds players who were missing the first time. Because we store whatever price
  existed when we ran, there is no opening/closing choice.
* **Only live prices go in.** Estimated players are never written back.
* **Pruned each `build`, alongside the roster**, to `lookback_games` games per
  team. Rows before `earliest_game_date` are dropped too.
* **Thin-store guard.** A team is estimated only once the store holds
  `fallback.min_stored_games` of its games. Below that the game is left out and
  Slack says why (`Bruins (2 of 5 games stored)`). This stops a one-game average
  going out while the config says five.

Two implementation details worth knowing:

**Averaging happens in probability space, never odds space.** A player at +200
and +600 averages to 23.8% (+320), not 20.0% (+400) — nearly four points of
probability, easily a different tier.

**A player absent from their most recent game is dropped, not estimated.** Every
stored game was priced, so that absence is a real scratch. Including a star
who does not dress hands the user a pick with zero chance, which is worse than
leaving out a star who would have played. Games we never *captured* (unpriced at
both runs, or a failed run) tell us nothing about anyone. They are simply
absent from the store, so the check falls to the next game back.

**Preseason is excluded from the lookback.** Exhibition lineups are full of
prospects who will not be on the roster and regulars play limited minutes, so
those prices are a poor basis for regular-season tiering — a confident-looking
estimate built on them is worse than dropping the game.
`fallback.earliest_game_date` sets a floor at the regular-season opener, which
works regardless of whether the feed labels season type;
`fallback.exclude_season_types` is a second line of defence: prices from
fixtures the feed labels `Preseason` are never written to the store.

This self-heals. On opening night no team has eligible history, so unpriced
games drop; by mid-October everyone has a full window and the fallback is
fully active. Nothing to switch back on.

Estimated players are marked in the sheet (`fallback.marker`, default
`" (est)"`) and counted in the Slack summary. When more than 35% of the pool is
estimated, the summary flags that the projection is correspondingly softer.

### Is the fallback any good?

Nothing about an estimate contradicts itself — it produces a plausible number,
the sheet tiers it, and without a check you would never learn it was wrong. So
`build` logs every estimate to a hidden `_estimate_log` tab, and the 9:30am
`drift` run fills in the actual price once the market posts. The comparison is
against a morning price, which is the closest like-for-like benchmark we get: a
price closer to puck drop has absorbed confirmed goalies and lineup news that
the estimate never had access to, so a gap against it would mix estimation error
with market movement you cannot separate out.

`python -m src.main accuracy` rolls it up:

* **Tier match rate.** The headline. Well under ~80% means shipping estimated
  players is worse than dropping their game.
* **Mean signed error.** A consistent lean (estimates running long or short) is
  correctable with an offset. Scatter around zero is not.
* **Accuracy by games used.** The direct test of "use even one game rather than
  lose a star" — if one-game estimates are far worse than five-game ones, that
  rule is costing you.

None of this costs extra API calls. `drift` already pulls the day's odds; this
writes the comparison down instead of discarding it.

---

## Pool size is the dominant lever in the regular season

This is the thing that changes versus the playoffs, and it is worth
understanding before the first live slate.

In the playoffs the pools were small — under 15 per tier on a one- or two-game
night — so the cap never bound and dilution never applied. The main lever was
excluding the shortest-priced stars.

On a ten-game regular-season night the eligible pool is 40–90 players per tier
against a cap of 15. Dilution bites hard, and it does something counterintuitive:
**widening a band to add more players lowers the projected payout**, because
the added players drag the tier average down, which drags the effective top
down with it.

Measured on a simulated ten-game slate, holding the bands fixed and changing
only how many players get submitted per tier:

| Submitted per tier | T1 eff | T2 eff | T3 eff | E[pts] |
|---|---|---|---|---|
| all eligible (41/65/94) | 34.0% | 21.9% | 11.7% | 7.93 |
| top 30 | 35.5% | 24.6% | 14.4% | 9.00 |
| top 20 | 36.8% | 25.3% | 15.1% | 9.42 |
| top 15 | 37.5% | 25.6% | 15.5% | 9.64 |

Same bands, same available players — a 1.7-point swing purely from pool size.

The practical consequence: **do not submit the full eligible pool on a big
night.** Trim to roughly the cap. Submitting 94 players to a tier that will
only use 15 of them is throwing away payout for no user-visible benefit.

The trim is not yet automated, because the right number depends on targets that
are not final. Once they are, this becomes a `slate.max_submitted_per_tier`
setting and the job handles it.

---

## Open items

- **`tiers` are provisional.** They were solved against the confirmed 8.85-point
  budget, but on a *simulated* slate — no real odds have been pulled. Re-derive
  with `simulate` against a real pull before the first live contest. Note they
  are far looser than last season's playoff bands, so the old habit of excluding
  anyone shorter than +250 no longer applies: those stars belong in tier 1 now.
- **The fallback is off** until teams have ~5 regular-season games behind them
  (mid-October). Flip `fallback.enabled` then.
- **10 pm coverage is unverified.** Goalscorer props often do not post until the
  morning of the game. The Slack summary reports coverage (`6 of 10 games
  priced`) every night, so the first week of runs answers this empirically.
  Partial coverage is not a random subset — early-priced games skew toward
  marquee matchups with higher totals — so the config drops unpriced games
  entirely rather than writing a partial pool, and warns below 80% coverage.
- **Historical odds are not on our API key.** `/api/v3/fixtures/odds/historical`
  returns `Insufficient permissions`, which is why the fallback reads from the
  self-built price store instead.
- **Estimate accuracy needs a few weeks of data** before the rollup means
  anything. Check `accuracy` after the first couple of weeks and decide then
  whether the fallback is earning its place.
- **Drift is measurement only.** The slate locks at generation, so the morning
  run cannot fix anything. It exists to quantify how often a locked slate had a
  player scratched or repriced, which is the error term for reconciling
  projected against actual. It runs quietly: it records the morning prices,
  scores the estimates and logs the movement check, but posts nothing to Slack
  unless `drift_check.slack` is turned on. Every locked player's result is
  kept all season in the hidden `_drift_log` tab (never pruned), one row per
  player per day, with a `flag` of `moved` or `not_priced`, so stale-slate
  rates can be measured over the season. It also runs when the previous
  night's build wrote no slate, because that is the morning whose prices the
  fallback needs most.
