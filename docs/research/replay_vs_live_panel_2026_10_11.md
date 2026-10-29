# Measurement: the replay against the LIVE record, on the whole comparable panel

Status: **MEASURED**, 2026-10-11. The first run of `intent-replay` over every comparable LIVE pick with a tape the tool fetched itself, after #1750 made the bar window and the session grid the script's own job. Every number below carries the command that produced it, and nothing here is quoted from an earlier run.

## 1. What was run, and on what

The report is built from the committed journals rather than from a live broker read, so the panel is reproducible:

```python
# apps/alphalens-research, with the repo venv
from tempfile import TemporaryDirectory; from unittest import mock; import pathlib
from alphalens_pipeline.brokers.automanager.trades import TradesFilters, build_trades
from tests.brokers.automanager.trades_fixture import NOW, FakeFillHistory, install_journals
with TemporaryDirectory() as tmp:
    home = pathlib.Path(tmp)
    with mock.patch.object(pathlib.Path, "home", staticmethod(lambda: home)):
        install_journals(home)
        body = build_trades("live", broker=FakeFillHistory(),
                            filters=TradesFilters(pick=None, limit=None), now=NOW).body()
```

`NOW` is `2026-10-03T15:30:00Z` and the fixture is `tests/brokers/automanager/fixtures/trades/live_2026_10_03/`. The report holds **35 records**, of which **12** carry an empty `replay_exclusions`. The other 23 are excluded for `legacy_plan_shape` (21), `entry_mode_unsupported` (3), `not_final` (3) and `manual_close` (2); a record can carry more than one.

Each of the 12 was then joined and walked:

```bash
python -m scripts.replay_from_pick <report.json> <PICK> --out <dir> --bar-cache <cache> \
    --entry-trails tests/brokers/automanager/fixtures/trades/live_2026_10_03/entry_trails.jsonl \
    --format json
python -m intent_replay run <dir>/<stem>.document.json \
    --config <dir>/<stem>.config.json --bars <dir>/<stem>.bars.json
```

The bars were fetched from Polygon at `now = 2026-10-11T17:2x Z`, raw (`adjusted=false`), on each venue's half-open session grid, with a provenance sidecar per pick. Nine of the twelve ran; three refused, for a reason section 5 states.

## 2. The panel is smaller than the report's own count

**Five of the twelve records were not terminal when the report was taken.** Their `state` is `never_filled` and their `state_reason` is `pending`: the entry ladder was still live at `NOW`. `_replay_exclusions` (`trades.py:2962-2992`) adds `not_final` only for `state in (open, unresolved)`, so `never_filled` plus `pending` passes as comparable.

| pick | MIC | record at the report's `now` | replay | why it is not comparable |
|---|---|---|---|---|
| `BE:2026-09-30` | XNYS | `never_filled` / `pending` | refused `deadline_not_yet_servable` | the entry ladder was still live when the report was taken |
| `OSCR:2026-09-30` | XNYS | `never_filled` / `pending` | refused `deadline_not_yet_servable` | the entry ladder was still live when the report was taken |
| `PLTR:2026-09-29` | XNAS | `never_filled` / `pending` | `no_fill` | the entry ladder was still live when the report was taken |
| `SYM:2026-09-30` | XNAS | `never_filled` / `pending` | refused `deadline_not_yet_servable` | the entry ladder was still live when the report was taken |
| `VCYT:2026-09-29` | XNAS | `never_filled` / `pending` | `open` | the entry ladder was still live when the report was taken |

Replaying such a record compares a snapshot taken on 2026-10-03 against a walk that runs to the ladder's deadline. On `VCYT:2026-09-29` that is exactly what happened and it moved the answer — see section 4.

**So the comparable panel is 7 records, not 12:** four closed and three `never_filled/disarmed`. All seven ran; the three the window gate refused were all in the non-terminal five.

## 3. The seven terminal records

| pick | MIC | real | replay | bars walked | R real | R replay | ΔR | P&L real | P&L replay | ΔP&L pp | exit Δ min |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `ASTS:2026-09-23` | XNAS | closed/stop_moved_kind_unknown | `closed_stop` | 1566 | 0.3580 | 0.4036 | +0.0456 | 5.22% | 6.01% | +0.80 | -1.6 |
| `BE:2026-09-29` | XNYS | never_filled/disarmed | `no_fill` | 3510 | — | — | — | —% | —% | — | — |
| `DAVE:2026-09-25` | XNAS | never_filled/disarmed | `no_fill` | 1670 | — | — | — | —% | —% | — | — |
| `EWTX:2026-09-25` | XNAS | closed/stop_moved_kind_unknown | `closed_stop` | 1148 | 0.3404 | 0.4174 | +0.0770 | 4.11% | 4.89% | +0.78 | -106.5 |
| `QBTS:2026-09-21` | XNAS | never_filled/disarmed | `no_fill` | 3900 | — | — | — | —% | —% | — | — |
| `SMMT:2026-09-23` | XNAS | closed/take_profit | `closed_tp` | 1557 | 1.0267 | 0.9349 | -0.0918 | 11.87% | 10.76% | -1.11 | -1.0 |
| `VST:2026-09-21` | XNYS | closed/trailed_stop | `closed_stop` | 3136 | 0.3834 | 0.3099 | -0.0734 | 2.63% | 2.37% | -0.25 | -13.4 |

`R` is `summary.r_multiple` against the record's `outcome.r_multiple`; `P&L` is `pnl_pct_of_spent` on both sides. `exit Δ` is the replay's `position_closed` event against the record's `exits[-1].venue_time`, in minutes, negative meaning the replay closed earlier.

### The outcome word agrees on 7 of 7

**SUPERSEDED IN PART by section 9 (2026-10-28): `VCYT:2026-09-29` has since closed and its outcome word does NOT agree. This heading, and the `|ΔR| <= 0.092` / `|ΔP&L| <= 1.11 pp` block below, are true of the seven records measured here and of no panel that contains a closed VCYT. Read section 9 before quoting either.**

Four closed picks agree on the exit LEG, and three never-filled picks agree that nothing filled. The leg and not the instruction kind, for a reason the record states itself: `VST:2026-09-21` carries `reason: trailed_stop` and `SMMT:2026-09-23` carries `take_profit`, but `ASTS:2026-09-23` and `EWTX:2026-09-25` carry `reason: stop_moved_kind_unknown`. Their `reason_evidence` shows the level being moved before the fill — once on ASTS, four times on EWTX — so a stop closed them and it was being trailed, while the record declines to assert which instruction was resting. Saxo's audit endpoint rewrites `TrailingStopIfTraded` to `StopIfTraded`, so its rows cannot adjudicate that, and the replay's `closed_stop` is the same claim at the same resolution.

**The never-filled agreement is earned, not vacuous.** Measured against each pick's own tape, inside its own entry window: `BE:2026-09-29`, `DAVE:2026-09-25` and `QBTS:2026-09-21` have **zero** minute bars whose low reaches the declared `entry_tiers[0].limit_price` before `config.entry_deadline`, over 3 510, 1 670 and 3 900 walked bars. The price never got there, and the replay publishes `entry_expired`. A window too short to reach the ladder's expiry would publish the same word with an empty trace, which is why section 6.6 of the bar-fetch design makes the right edge a refusal rather than a clamp.

### The exit fires EARLY in four of four, by 1.0 to 106.5 minutes

| pick | replay closed | venue filled | Δ |
|---|---|---|---:|
| `SMMT:2026-09-23` | 2026-09-29 13:30Z | 2026-09-29 13:30Z | −1.0 min |
| `ASTS:2026-09-23` | 2026-09-29 13:35Z | 2026-09-29 13:36Z | −1.6 min |
| `VST:2026-09-21` | 2026-10-01 13:46Z | 2026-10-01 13:59Z | −13.4 min |
| `EWTX:2026-09-25` | 2026-09-30 15:40Z | 2026-09-30 17:26Z | −106.5 min |

One direction, every time. That is the sign the exit-leg inventory predicts: the replay's trail ratchets from each bar's HIGH, where the daemon ratchets from a ~45-second poll that can miss it, so the modelled stop sits at or above the real one and fires at or before it.

### But the money is NOT systematically biased

```
ΔR    +0.0456   +0.0770   −0.0918   −0.0734      two up, two down, |ΔR| <= 0.092
ΔP&L  +0.80 pp  +0.78 pp  −1.11 pp  −0.25 pp     two up, two down, |ΔP&L| <= 1.11 pp
```

**This is the result worth stating plainly, because the naive reading of the timing channel is wrong.** "The replay exits early" invites "so it leaves money on the table and is systematically pessimistic". On this panel it is not: the sign of the money gap is split two–two, while the sign of the timing gap is four–zero. An earlier trail exit books more on the bar it fires and gives up whatever the path would have added afterwards, and which term wins is a property of the path, not of the model.

Four picks cannot establish a distribution. What they do establish is that the direction is not fixed, so no single-signed correction may be applied to an R figure on the strength of the timing channel.

## 4. The one disagreement, and it is the harness rather than the model

**SUPERSEDED IN PART by section 9 (2026-10-28): the horizon mismatch this section diagnoses was real, and the tape prediction below held, but it was concealing a genuine model divergence — the daemon detects a touch on the BID and the replay on the bar low, i.e. the lowest trade. Read section 9 before concluding "harness rather than model".**

`VCYT:2026-09-29` is the only pick where the outcome word differs: the record says `never_filled`, the replay says `open` with `filled_fraction` 1.0 at an average entry of 43.80.

Both sides agree on the level. The document declares one `pullback` tier at `limit_price` **44.01**, and the daemon's own `planned_limit` is **44.01** too. The tape then settles it: **698 minute bars at or below 44.01 before the entry deadline, the first at 2026-10-07T13:30:00Z.**

That instant is four days AFTER the report's `NOW` of 2026-10-03T15:30Z. So the record's `never_filled` is true of the span the record covers, the replay's fill is true of the span the replay walks, and the two spans differ. The record's `entries[0].touched_at` is null with `null_reason: not_journaled`, which is what a watch that has not fired yet looks like.

**A ~45-second poll cannot miss a 698-minute condition**, so this is not the entry-trail divergence and not a missed fill. It is a comparison of two different horizons, and it is the reason section 2 narrows the panel.

## 5. The window gate refused exactly what it was designed to refuse

Three picks refused with `bars_window_unservable` / `deadline_not_yet_servable`, exit 7. Their refusal names its own arithmetic — for `BE:2026-09-30`:

> the entry ladder was live through session 2026-10-09 (entry_deadline 2026-10-09T20:00:00.000Z). The session grid is half-open at the close, so the ladder's expiry is only observable on the NEXT session, 2026-10-12 (close 2026-10-12T20:00:00.000Z), and the price vendor serves only past calendar days. The last session it can serve today is 2026-10-09.

`details.servable_from_date` is **2026-10-13**, which is what the bar-fetch design's own measurement predicted for this panel ("3 remain at 2026-10-12 and none at 2026-10-13"). The whole cost of the hard refusal, on this panel, is a wait of two calendar days on a pick whose ladder expired the day before yesterday. It is open question Q3 of that design, and this run is the first measurement of what it actually blocks rather than of how often it fires.

## 6. What this does NOT settle

**SUPERSEDED IN PART by section 9 (2026-10-28): the last bullet below ("The three refused picks") is wrong about what those three became. `BE:2026-09-30` filled and closed; only two of the three stayed `never_filled`. Read section 9.7 before quoting it.**

* **The retracted 1.05%.** The published claim that "the replay is pessimistic on take-profit exits by 1.05%" came from a run whose tape carried a bar ninety minutes after the session close. On a correctly fetched tape the same pick measures **−1.11 pp of P&L (−9.4% of the realized figure)** — the same direction and a similar magnitude. Whether that is agreement or coincidence **cannot be determined**, because the retracted run's session filter and close-bar treatment were never recorded. That irreproducibility is why every run now writes a provenance sidecar.
* **Any distribution.** Seven terminal records, four of them closed. Every magnitude here is a hint with its construction attached, not a constant.
* **The non-US path.** All twelve records are XNAS or XNYS, so the venue-span half of the grid is unexercised by this panel.
* **The three refused picks.** They become replayable on 2026-10-13 and would take the closed panel from four to four and the terminal panel from seven to ten, since all three are `never_filled`.

## 7. Two things worth changing, neither done here

1. **`_replay_exclusions` should treat `never_filled` plus `state_reason: pending` as not comparable**, the way it already treats `open` and `unresolved`. Five of twelve records on this panel carry that shape, and replaying one compares two different horizons. The fix belongs beside the existing `not_final` branch at `trades.py:2973`.
2. **The script's refusal codes and reason vocabularies are published only as prose**, in the bar-fetch design's section 11. The broker contract keeps its table and its registries in step with `tests/brokers/test_broker_failure_codes_published.py`; nothing does that here, so a reason added in code turns no document red.

---

## 9. Correction, 2026-10-28: VCYT closed, and the outcome word no longer agrees

Status: **MEASURED**, 2026-10-28. **This section assumes section 8 (2026-10-12) is already in the file.** If the two land in the other order, read section 8 first; it reports the two section 7 items landing and it changes nothing measured here. The `Status:` word is the one this file's own header uses, not one of the four in [`CLAUDE.md`](CLAUDE.md) — those four describe a design decision, and this file is a measurement record. Sections 3, 4 and 6 are SUPERSEDED IN PART by this section. Sections 1, 2, 5, 7 and 8 do not move.

### 9.1 Section 3's "The outcome word agrees on 7 of 7" now has a counterexample

`VCYT:2026-09-29` is terminal. Re-joined and re-walked on 2026-10-28:

| | entry | exit | outcome | R | P&L of spent |
|---|---|---|---|---:|---:|
| LIVE | 2026-10-06T19:09:14.357Z @ 44.43 | 2026-10-27T14:46:12.275Z @ 49.51 | `take_profit`, `tp_label` TP1 | 0.9039 | 11.4337% |
| replay | 2026-10-07T13:31:00Z @ 43.80 | 2026-10-22T14:27:00Z @ 45.492 | `closed_stop` | 0.3391 | 3.8630% |

The gap is ΔR **−0.5648** and ΔP&L **−7.57 pp**. Decomposed against a counterfactual that runs the
same trail rule from the LIVE entry price, the **entry channel is ΔR −0.5115** and the
**exit-fill channel ΔR −0.0534**, so nearly all of it is the entry.

The claim this corrects was true of its own panel. It is no longer true of any panel that contains
a closed `VCYT:2026-09-29`.

### 9.2 Section 3's `|ΔR| <= 0.092` and `|ΔP&L| <= 1.11 pp` block is six times exceeded

`0.5648 / 0.092 = 6.1` and `7.5707 / 1.11 = 6.8`. Both bounds were stated over four closed picks,
and VCYT is a fifth.

The population measured on 2026-10-28 is a DIFFERENT one from section 3's, so it replaces no
bound: 8 broker-backed, exclusion-free, touched picks rather than section 3's 7 terminal records.
On it, **1 outcome word of 8 flips, and it is VCYT**. Among the 7 that did not flip,
`max |ΔR| = 0.0322` and `max |ΔP&L| = 1.07 pp`, both on `BE:2026-09-30`.

What section 3 says ABOUT the money does survive: the sign is still split across the panel, so no
single-signed correction may be applied to an R figure.

### 9.3 Section 8's "stands as measured" holds for every figure, and says nothing about the panel

Section 8 closes by saying every figure in sections 1 to 6 "stands as measured". That is correct
and this section withdraws none of them. **They were measured correctly; the population they
described was incomplete.** Section 2 of this file already recorded why: at the report's
`NOW` of 2026-10-03T15:30Z, `VCYT:2026-09-29` was `never_filled` / `pending`, its entry ladder
still live, and section 2 excluded it by hand for exactly that reason. It became terminal on
2026-10-27. So "stands as measured" is a statement about each figure and must not be read as "the
panel is complete".

### 9.4 Section 4's prediction held — and the horizon mismatch was hiding a model divergence

Section 4 is titled "The one disagreement, and it is the harness rather than the model" and
concludes that VCYT's disagreement was a comparison of two different horizons. It made a checkable
prediction about the tape:

> **698 minute bars at or below 44.01 before the entry deadline, the first at 2026-10-07T13:30:00Z.**

**That prediction held.** The replay's arming bar today is still `2026-10-07T13:30:00Z` (open 44.04,
high 44.21, low 43.51, close 43.795), and the native entry trail fires one bar later, at
13:31 @ 43.80.

What section 4 could not see is that the daemon had already filled a session earlier, on the BID.
The journaled bid touch is **44.00 at 2026-10-06T19:05:54.861Z** against a tier limit of 44.01, and
the fire followed at 2026-10-06T19:09:14.357Z @ 44.43. So the horizon mismatch was real AND it was
concealing a second, genuine divergence: **the two sides detect an entry touch on two different
price series.** The daemon reads the bid; the replay reads the bar LOW of Polygon minute
aggregates, which is the lowest TRADE.

**The wedge between the two series, with its construction.** Polygon's unadjusted minute tape for
2026-10-06 holds **357 bars** on the session grid the replay walks. The session's lowest TRADE is
**44.0872 at 2026-10-06T17:48:00Z**, and NO bar that session reaches the 44.01 limit. Two readings,
both against the 44.00 bid:

| measured against | arithmetic | wedge |
|---|---|---:|
| the session's lowest trade | `(44.0872 − 44.00) / 44.00` | **19.82 bps** |
| the touch-minute bar's own low (19:05Z bar: O 44.21, H 44.25, L 44.17, C 44.25) | `(44.17 − 44.00) / 44.00` | **38.64 bps** |

**Why 63 cents of entry flipped the outcome word.** The document declares `disaster_stop` 38.81 and
one take-profit tranche at 49.21.

| | entry | risk/share | 0.5R trail-arm trigger |
|---|---:|---:|---:|
| LIVE | 44.43 | `44.43 − 38.81 = 5.62` | `44.43 + 0.5 × 5.62 = 47.240` |
| replay | 43.80 | `43.80 − 38.81 = 4.99` | `43.80 + 0.5 × 4.99 = 46.295` |

The tape's highest high inside the replay's holding window (2026-10-07T13:31Z to
2026-10-22T14:27Z, 3 542 bars) is **46.6299 at 2026-10-22T13:54:00Z** — BETWEEN the two triggers.
So the replay armed its trail, was stopped on the 2026-10-22 pullback at 45.492, and never saw the
rally to the take-profit. The first high at or above 47.240 is **2026-10-23T15:55:00Z (47.26)**,
after the replay had already exited.

**The size of the entry gap is not what drives a flip.** `BE:2026-09-30` is the instructive row of
the same panel: its replay entry was **1.6997 HIGHER** than LIVE, far more than VCYT's 0.63, and its
ΔR was only −0.0322. What decides the word is whether the path's peak falls between the two arm
triggers.

### 9.5 The decision: change NEITHER side

The replay keeps reading the bar low and the daemon keeps reading the bid. The replay's stated
purpose settles it. Section 2 of
[`docs/superpowers/specs/2026-09-23-intent-replay-design.md`](../superpowers/specs/2026-09-23-intent-replay-design.md)
sets the goal as "Answer 'what would this document have done' against real bars", and section 2.1
says:

> The rule does NOT make the replay agree with the daemon. [...] A knob cannot supply a missing
> fact; it can only hide that the fact is missing.

So the replay answers to the MARKET, and the daemon is the comparison baseline rather than the
thing to copy.

**What that leaves exposed, measured over all 11 journaled LIVE touches.** On **11 of 11** the trade
tape reached the tier limit somewhere inside the entry window, so the replay never publishes "no
fill" where LIVE filled. On **10 of 11** it reached the limit in the touch session itself; VCYT is
the only exception, and it is late by one session rather than absent.

### 9.6 Two alternatives rejected, each by a measurement

**A quote entitlement, so the replay could see a bid: $199/month.** That is the only stocks plan
listing Quotes, against a project run cost of about $58/month, bought to address a measured
**1 outcome-word flip in 8**. It would also be half a fix: the touch test reads `bar.low` while the
FIRE test reads `bar.high`, so a bid-faithful entry needs BOTH sides of the book to repair one half
of a model whose other half is measured at n = 1.

**A spread tolerance on the touch test: NON-MONOTONE on this one pick.** Measured by replacing the
walk's reachability predicate with `bar.low <= rung.limit_price * (1 + tol / 10_000)` and re-running
the same VCYT document, configuration and bars:

| tolerance | outcome | replay entry | replay exit | R |
|---:|---|---|---|---:|
| 0 bps | `closed_stop` | 2026-10-07 13:31 @ 43.80 | 2026-10-22 14:27 @ 45.492 | 0.3391 |
| 17.54 bps | `closed_stop` | 2026-10-07 13:31 @ 43.80 | 2026-10-22 14:27 @ 45.492 | 0.3391 |
| 17.55 bps | `closed_tp` | 2026-10-06 17:48 @ 44.23 | 2026-10-27 14:44 @ 49.21 | 0.9188 |
| 20 bps | `closed_tp` | 2026-10-06 17:48 @ 44.23 | 2026-10-27 14:44 @ 49.21 | 0.9188 |
| 38.00 bps | `closed_tp` | 2026-10-06 17:34 @ 44.23 | 2026-10-27 14:44 @ 49.21 | 0.9188 |
| 38.70 bps | `closed_stop` | 2026-10-01 19:59 @ 44.23 | 2026-10-06 13:55 @ 46.996 | 0.5103 |
| 50 bps | `closed_stop` | 2026-10-01 19:58 @ 44.23 | 2026-10-06 13:55 @ 46.996 | 0.5103 |
| 200 bps | `closed_stop` | 2026-10-01 18:05 @ 44.23 | 2026-10-06 13:55 @ 46.996 | 0.5103 |

The word goes `closed_stop`, then `closed_tp`, then `closed_stop` again. The reason is visible in
the entry column: a wider tolerance moves the entry EARLIER, eventually into a different session,
and from a different session the path is a different path. So no tolerance "agrees with the
daemon" — agreement is a WINDOW, and both of its edges are properties of this one pick. The 19.82
bps wedge of section 9.4 happens to fall inside that window, which is what makes the knob tempting
and is also a fact about one pick and not a result.

### 9.7 Section 6's last bullet is wrong about the three refused picks

Section 6 closes with: "**The three refused picks.** They become replayable on 2026-10-13 and would
take the closed panel from four to four and the terminal panel from seven to ten, since all three
are `never_filled`."

The three are `BE:2026-09-30`, `OSCR:2026-09-30` and `SYM:2026-09-30` — the rows section 2 marks
`refused deadline_not_yet_servable`. Read from the LIVE trades report taken
2026-10-28T13:26:17.661Z (`alphalens broker trades --env live --format json`, offline mode, 48
records) — the same read section 9.8 counts its 66 entry rows from:

| pick | `state` | `state_reason` | exits |
|---|---|---|---:|
| `BE:2026-09-30` | `closed` | — | 1 |
| `OSCR:2026-09-30` | `never_filled` | `expired` | 0 |
| `SYM:2026-09-30` | `never_filled` | `expired` | 0 |

So **two** of the three are `never_filled`, not three, and the closed panel would go from four to
**five**, not four to four. `BE:2026-09-30` filled at 269.58 on 2026-10-08 and is the row section
9.2 already quotes as the largest non-flipping gap on the 8-pick panel. The refutation was already
in this file, one section away from the claim it refutes, which is why a correction has to sweep
the whole document and not only the sentences it was pointed at.

The "seven to ten" half survives: all three did become terminal, so the terminal panel does reach
ten. The bullet was already false when it was published on 2026-10-11, because `BE:2026-09-30`
filled three days earlier; nothing about VCYT caused it.

### 9.8 What IS being done, in two further pull requests

Neither changes how a touch is detected. Both attack the SILENCE around the gap, which is the part
that was actually wrong.

* **`fix/entry-trail-compaction-keeps-touched`** — the LIVE entry-trail journal loses its `touched`
  lines. Counted with `collections.Counter` over the `kind` field of the 186-line journal copied
  from the VPS on 2026-10-28: `watch_open` 61, `trough` 31, `trail_armed` 29, `fired` 28,
  `cancelled` 26, `expired` 7, `fill_probe` 3, `fill_probe_protection_gap` 1 — and **zero**
  `touched`. The 11 touch prices survive only in the pre-compaction snapshot files under
  `~/.alphalens/broker_orders/live/compaction_snapshots/` on the VPS, so **55 of 66 entry tiers can
  never be asked** whether their bid touch had a counterpart on the tape. The two numbers come
  from DIFFERENT reads and neither is the other's denominator, so both constructions are stated
  here: **66** is the entry rows of the LIVE trades report
  (`alphalens broker trades --env live --format json` taken 2026-10-28T13:26:17.661Z, offline
  mode, 48 records, `sum(len(t["entries"]) for t in trades)` = 66), and **11** is the touches that
  still carry a price. The same journal file holds only **61** distinct `crid` values, every one of
  them from a `watch_open`, because a tier whose watch never opened leaves no journal line at all.
  Do not recount 66 from the journal; it is not in there.
* **`feat/replay-touch-audit`** — nothing asks that question routinely. Every figure in this
  section was produced by hand, from scratch files, which is why a 1-in-8 defect went five weeks
  without being seen.

This section is the third change of the three, and it is the pull request on
`docs/replay-entry-reference-errata`. The same branch corrects one unrelated published claim in
`apps/intent-replay/README.md` — "Every live fire on record happened in the session of its touch",
falsified by `AUR:2026-10-06-g2`, touched 2026-10-13T20:01:17.519Z (77 seconds after the 20:00Z
XNAS close) and fired 2026-10-14T13:30:05.242Z, the next session — and replaces an uncheckable
"memo trap #8" citation in `entry_watch.py` and `entry_trail_watcher.py` with the fact it stood
for.
