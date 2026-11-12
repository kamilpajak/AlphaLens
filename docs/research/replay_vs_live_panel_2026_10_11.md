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

**SUPERSEDED IN PART by section 10 (2026-11-03): the DIRECTION holds on 13 of 13, but the range below does not survive at either end. The bars are 1-minute and the replay stamps the bar start, so 9 of the 13 are inside one bar and cannot be sized from this tape; the panel median is -0.84 min. Read section 10.5 before quoting a magnitude.**

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

**§8 (2026-10-12): both landed — #1756 and #1757. The amendment also carries one measurement this section provoked.**

1. **`_replay_exclusions` should treat `never_filled` plus `state_reason: pending` as not comparable**, the way it already treats `open` and `unresolved`. Five of twelve records on this panel carry that shape, and replaying one compares two different horizons. The fix belongs beside the existing `not_final` branch at `trades.py:2973`.
2. **The script's refusal codes and reason vocabularies are published only as prose**, in the bar-fetch design's section 11. The broker contract keeps its table and its registries in step with `tests/brokers/test_broker_failure_codes_published.py`; nothing does that here, so a reason added in code turns no document red.

---

## 8. Amendment, 2026-10-12

Section 7 named two changes and said neither was done. Both are now on `main`, and the review of the first produced a measurement that belongs beside the panel rather than only in a pull request.

### Item 1 landed as #1756, and it is sharper than section 7 asked for

`_replay_exclusions` now adds `not_final` for a `never_filled` record whose `state_reason` is `pending`. The reason is threaded in from `_state`, which already derives it, so the predicate and the producer cannot drift. `EXCLUDE_NOT_FINAL` is reused: the published nine-string `REPLAY_EXCLUSIONS` tuple did not grow, because "it has no final outcome" is one failure mode and the record already carries `state` and `state_reason` for a caller that needs to know which.

The fixture's comparable set is now the seven records section 3 measured, and the five of section 2 carry `not_final` themselves. So the narrowing this memo had to do by hand is now the report's own verdict.

**What the review added, and it is a new measurement.** Asked whether `disarmed` is the same horizon mismatch as `pending` — in both, the ladder's own window never ran out — the answer turned out to need data rather than reasoning. Each disarmed pick leaves an unobserved tail between the operator's intervention and the ladder's expiry:

| pick | disarmed at | entry deadline | unobserved tail | bars at or below the declared limit inside it |
|---|---|---|---:|---:|
| `BE:2026-09-29` | 2026-09-30T15:42:20Z | 2026-10-08T20:00Z | 196.3 h | **0** |
| `DAVE:2026-09-25` | 2026-09-30T15:42:21Z | 2026-10-06T20:00Z | 148.3 h | **0** |
| `QBTS:2026-09-21` | 2026-09-23T14:22:52Z | 2026-09-30T20:00Z | 173.6 h | **0** |

The replay walks those tails while the record is frozen, so the comparison on these three converges **because no bar reached the rung inside them** — data, not construction. That is why they stay comparable while `pending` does not: a disarmed pick cannot come back to life, so its replay is a counterfactual; a pending pick was still live, so a fill in its tail is one the daemon might still have taken. The distinction is now written into `_replay_exclusions`' own docstring with these figures.

Section 3's never-filled paragraph measured zero touches over each pick's whole entry window; this table is the same check narrowed to the part of that window the record could not see. Both readings agree, which is the point: the earlier number already covered the tail.

### Item 2 landed as #1757

The command's 15 codes and 29 reasons now live in one registry, `_REFUSAL_REASONS`, which the refusal renderer reads; an unregistered pair prints a warning before the envelope and still refuses, so the "one JSON object as the last stderr line" contract holds even on a bad pair. An AST walk over the module makes four things CI failures: a site naming an unregistered code, a site stating a reason outside that code's set, a registered code the help text omits, and a code the help text names that the registry does not define.

**The published surface is the command's own `--help`, deliberately NOT section 11 of the bar-fetch design.** That memo is a dated record marked LOCKED, so pinning live code to it would make a historical document a contract. And it could not serve as the gated surface anyway, measured: its section 11 table carries **11 rows against the registry's 15 codes**, describing the four pre-existing refusals in prose instead. A reader sent there for `usage`'s reasons would find no row. `--help` therefore publishes the codes, and names the registry itself as authoritative for the reasons.

### One residual this amendment exists to keep findable

No issue was filed for it, so it is recorded here. `_action_reason` returns `split` for any corporate-action detail that does not begin with `special dividend`. It is total today — the lookup writes exactly two detail shapes, which its own docstring states — but a third shape would be **published as `split` rather than refused**. And measured rather than reasoned, the fallthrough is wider than a third shape: the expression is `detail and detail.startswith("special dividend")`, so a `found` answer whose `detail` is `None` also publishes `split`, and `CorporateActionsAnswer.detail` is typed `str | None`. The new gate cannot see this: it reads the literals the function returns, not whether its branches are exhaustive. Fixing it means refusing an unknown detail, or adding a sentinel reason, or making the lookup's detail an enum that is total by construction — each a behaviour change needing its own tests and its own check of whether a third shape exists in the wild.

### What this amendment does not change

Every figure in sections 1 to 6 was measured on 2026-10-11 against the report as it behaved before #1756, and stands as measured. Re-running today yields the same seven terminal records; what differs is that the report now marks the other five itself instead of leaving the narrowing to the reader.

---

## 9. Correction, 2026-10-28: VCYT closed, and the outcome word no longer agrees

Status: **MEASURED**, 2026-10-28. It builds on section 8 (2026-10-12), which reports the two section 7 items landing and changes nothing measured here. The `Status:` word is the one this file's own header uses, not one of the four in [`CLAUDE.md`](CLAUDE.md) — those four describe a design decision, and this file is a measurement record. Sections 3, 4 and 6 are SUPERSEDED IN PART by this section. Sections 1, 2, 5, 7 and 8 do not move. Section 10 (2026-11-03) later measured a 13-pick panel and reconciled 9.2's bound with it; 9.2 stands over its own 8 picks.

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

## 10. Refresh, 2026-11-03: the panel on thirteen comparable closed picks

Status: **MEASURED**, 2026-11-03; section 10.6 and 10.9 amended by section 11 (2026-11-12).
Section 9 measured one pick against the LIVE record; this section
measures thirteen, every closed pick the report calls comparable. It SUPERSEDES IN PART section 3
(the exit-timing range and the non-flipping bound) and extends section 9.2 with the panel it was
missing. Sections 1, 2, 4, 5, 6, 7, 8, 9.1 and 9.3 to 9.8 do not move, and 10.9 says which claims
of theirs this section deliberately did not re-derive.

The occasion was a different question: two LIVE positions closed on 2026-11-02 and neither can be
replayed at all. `NVDA:2026-10-30` and `AVGO:2026-10-30` both carry `entry_mode: "immediate"`, and
the replay refuses that shape with `entry_mode_unsupported` by the §8.1 decision in the spec. Three
of the four picks armed on 2026-10-30 are `immediate`; only `MSFT:2026-10-30` is not, and it is
still open. So the panel below is the newest measurement available, and it will not grow from the
2026-10-30 cohort.

### 10.1 What was run, and why its counts are not section 1's

```bash
# on the VPS, read-only
alphalens broker trades --env live --format json --all     # 2026-11-03T13:37:38.075Z
# locally, per pick, SERIALLY (the vendor allows 5 requests a minute for the whole project key)
python -m scripts.replay_from_pick <report.json> <PICK> --out <dir> \
    --bar-cache ~/.alphalens/replay_bars --entry-trails <live entry_trails.jsonl> --format json
python -m intent_replay run <dir>/<stem>.document.json \
    --config <dir>/<stem>.config.json --bars <dir>/<stem>.bars.json
```

The report holds **53 records**: 27 closed, 2 open, 24 never_filled. **23** carry an empty
`replay_exclusions`, and the **13 of those that are closed** are this panel. Exclusion counts over
all 53, a record able to carry more than one: `legacy_plan_shape` 21, `entry_mode_unsupported` 6,
`placed_late` 4, `not_final` 3, `manual_close` 3.

**Do not compare these counts with section 1's.** Section 1 built its report from the frozen fixture
`tests/brokers/automanager/fixtures/trades/live_2026_10_03/` at `NOW = 2026-10-03T15:30:00Z`; this
one is a live read five weeks later. Different corpus, different construction, and the arithmetic
between them means nothing.

The trail distance each run needs comes from the pick's own `watch_open` line, and
`replay_from_pick.py:2566` reads the journal FILE rather than its history — so a distance boot
compaction removed would refuse the pick. Checked before the run rather than assumed: the live
journal holds 193 lines, its full history through `journal_snapshots.iter_journal_history` holds 224
records, both fold to 63 tiers, and the two folds agree on all 63 shared crids except
`AUR-2026-10-06-g2-entry-t0`, a never_filled pick outside this panel. So compaction removed nothing
this panel needs, and either source gives the same distance.

Bars are 1-minute and raw (`adjusted=false`) on each venue's half-open session grid. Nine windows
came from the cache; four were fetched (ASTS, SMMT, TNGX, VST). The corporate-action verdict is
`none` on all thirteen, from `polygon /v3/reference/{splits,dividends}`.

### 10.2 The thirteen rows

`ΔR` and `Δ price` are REPLAY MINUS LIVE, so a positive number means the replay said more. `ΔT` is
in minutes. The LIVE side is `outcome.*` and `exits[].venue_time` from the report (source
`venue.audit`); the replay side is its own `summary`.

| pick | LIVE exit | replay exit | LIVE R | replay R | ΔR | ΔT exit | ΔT entry | Δ entry px | Δ exit px |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| `ALAB:2026-10-05` | disaster_stop | stop | −1.0116 | −1.0000 | +0.0116 | −0.8 | −0.5 | −0.4880 | +0.5100 |
| `ASTS:2026-09-23` | stop (kind unknown) | stop | +0.3580 | +0.4036 | +0.0456 | −1.6 | −0.5 | +0.2285 | +0.7144 |
| `BE:2026-09-30` | disaster_stop | stop | −1.0363 | −1.0684 | −0.0322 | −0.4 | −2.8 | +1.6997 | −1.4762 |
| `CRDO:2026-10-05` | disaster_stop | stop | −1.2188 | −1.2241 | −0.0053 | −0.2 | −1.2 | +0.1019 | −0.1800 |
| `EWTX:2026-09-25` | stop (kind unknown) | stop | +0.3404 | +0.4174 | +0.0770 | −106.5 | −0.1 | −0.1558 | +0.1427 |
| `FLY:2026-10-06` | disaster_stop | stop | −1.0181 | −1.0000 | +0.0181 | −0.6 | +1.9 | +0.0151 | +0.0600 |
| `HUT:2026-10-06` | disaster_stop | stop | −1.0056 | −1.0000 | +0.0056 | −0.1 | −1.0 | −0.0680 | +0.0767 |
| `NESR:2026-09-30` | disaster_stop | stop | −0.9987 | −1.0000 | −0.0013 | −0.3 | −84.0 | +0.1627 | −0.0041 |
| `RLAY:2026-10-05` | disaster_stop | stop | −1.0000 | −1.0000 | −0.0000 | −0.2 | −0.3 | +0.0017 | 0.0000 |
| `SMMT:2026-09-23` | take_profit | tp_complete | +1.0267 | +0.9349 | −0.0918 | −1.0 | −0.7 | −0.0095 | −0.1900 |
| `TNGX:2026-09-23` | take_profit | tp_complete | +0.7557 | +0.9297 | **+0.1740** | −0.9 | −14.3 | −0.3428 | −0.0103 |
| `VCYT:2026-09-29` | take_profit | **stop** | +0.9039 | +0.3391 | **−0.5648** | −7219.2 | +1101.8 | −0.6300 | −4.0180 |
| `VST:2026-09-21` | trailed_stop | stop | +0.3834 | +0.3099 | −0.0734 | −13.4 | −1.6 | +1.1781 | +0.8632 |

### 10.3 The R denominator differs on 13 of 13, so ΔR is not a measure of exit quality

Both sides compute R on `avg_entry_price - placed_stop`, and because the entry fill differs, so does
the denominator — on **every** pick. The relative difference has a median of **0.09%** and a range
of **−11.21%** (VCYT) to **+12.72%** (VST). Four picks exceed 5%: NESR +5.08, TNGX −9.16, VCYT
−11.21, VST +12.72.

That is not a footnote. Three of the four largest `|ΔR|` in the table — VCYT, TNGX, VST — are in
that same set of four, so on those rows a large ΔR is substantially a different entry anchor rather
than a different exit. NESR is the counter-case that keeps this from being a rule: a 5.08%
denominator gap with `ΔR = −0.0013`.

The two columns that ARE like-for-like are the price deltas, and they are stated in the table for
that reason.

### 10.4 The exit MECHANISM agrees on 12 of 13, and the disagreement is the pick section 9 named

The `state` word in the LIVE record is `closed` for all thirteen, so comparing it against the
replay's `closed_stop` / `closed_tp` is trivially true and measures nothing. The comparison worth
making is the mechanism, and it is in the table's first two columns:

| LIVE mechanism | n | replay | agrees |
|---|---:|---|---|
| `disaster_stop` | 7 | stop | yes |
| stop, kind unknown (ASTS, EWTX) | 2 | stop | yes |
| `trailed_stop` (VST) | 1 | stop | yes |
| `take_profit` (SMMT, TNGX) | 2 | tp_complete | yes |
| `take_profit` (VCYT) | 1 | **stop** | **no** |

**12 of 13.** The single disagreement is VCYT, which is the counterexample section 9.1 already
published; this panel adds no second one. Section 3's heading said "the outcome word agrees on 7 of
7" while its body scored the exit LEG — the leg is what is scored here too, and on a panel almost
twice the size.

### 10.5 The exit fires early on 13 of 13, and within one bar on 9

Every row is negative: the replay exits no later than the LIVE record on all thirteen, which
extends section 3's "four of four" to a panel three times the size and makes the direction the
firmest thing in this file.

The MAGNITUDE is a different claim, and section 3's "by 1.0 to 106.5 minutes" does not survive at
either end. The bars are 1-minute and the replay stamps the bar START, so anything inside 1 minute
is below the instrument's resolution. **Nine of thirteen are within one bar** (|ΔT| ≤ 1.0), with a
panel median of **−0.84 min**. Only four exceed it: VCYT −7219.2, EWTX −106.5, VST −13.4, ASTS
−1.6.

So the early-exit gap is real in direction and, on 9 of 13 picks, too small for this tape to size.

### 10.6 The money gap is still not systematically signed

`ΔR` is **above zero on 6** and below on **7**. In the like-for-like units: the entry price delta is
positive on 7 and negative on 6, median **+0.0017 USD**; the exit price delta is positive on 6,
negative on 6 and exactly zero on 1 (RLAY), median **0.0000 USD**.

Section 3's money claim therefore stands on a panel of 13: **no single-signed correction may be
applied to an R figure.** The largest `|ΔR|` is 0.5648 (VCYT) and, excluding it, **0.1740** (TNGX). Section 11 measured a 14th pick at **0.7901**; both figures stand over their own panels.

Section 9.2 reported `max |ΔR| = 0.0322` on BE among the picks that did not flip. TNGX exceeds that
5.4×, and it is NOT a contradiction: section 9.2's panel was "8 broker-backed, exclusion-free,
**touched** picks", and exactly 8 of these 13 carry a journaled `touched` line — FLY, HUT, ALAB,
CRDO, RLAY, BE, NESR and VCYT. The other five (EWTX, ASTS, SMMT, TNGX, VST) are the oldest, armed
2026-09-21 to 2026-09-25, and their touch lines were compacted away before the first snapshot
existed on 2026-10-01. So section 9.2's bound holds over its own panel, and TNGX could not have
been in it. The membership was recovered by counting, not assumed.

### 10.7 VCYT reproduces section 9 to every digit, and the reproduction tests the walker, not the tape

`ΔR = −0.5648` and `ΔP&L = −7.5707 pp`, the figures section 9.1 and 9.2 published, from a LIVE
report read five days later and a replay re-run.

What that does and does not establish was checked rather than asserted. Both bar caches are still on
disk, so the two tapes can be differenced directly:

```
VCYT_1790688600000-1793131200000_1m_raw.json   fetched_at 2026-10-28T13:28:28.183Z   7065 bars
VCYT_1790688600000-1793304000000_1m_raw.json   fetched_at 2026-11-02T21:27:30.265Z   7804 bars
overlap 7065   differ in ANY field 208   differ in OHLC 3   {'vw': 206, 'o': 2, 'h': 2, 'l': 3, 'c': 3}
```

All three OHLC differences are six-decimal to four-decimal rounding (48.142208 → 48.1422), and all
three fall on 2026-10-26 and 2026-10-27, AFTER the replay's exit at 2026-10-22T14:27:00Z. Inside the
replay's holding window (entry 2026-10-07T13:31:00Z) the count of OHLC-differing bars is **0**, and
206 of the 208 differences are in `vw`, which the walker never reads.

So the two runs saw the same prices wherever the result turns on them. The reproduction is evidence
that the walker is deterministic; it is NOT evidence that the vendor's tape is stable.

### 10.8 One refusal, published

The first `ASTS:2026-09-23` join exited **7** with code `corporate_actions_unanswered`, reason
`lookup_failed`, vendor message "splits lookup failed for ASTS: POLYGON_API_KEY environment variable
not set". The cause is the environment, not the pick: a worktree has no `.env`, and the retry
succeeded after the key was exported from the main checkout. It consumed no vendor request.

It is recorded here because the command block in 10.1 does not reproduce in a fresh worktree without
that export, and because a refusal inside a run that reports thirteen successes is exactly the kind
of thing that otherwise disappears.

### 10.9 What this does NOT settle

- **The two newest closed picks are outside every panel.** `NVDA:2026-10-30` and `AVGO:2026-10-30`
  are `entry_mode: immediate` and the replay refuses them. Nothing here measures the backtester
  against an immediate entry, and nothing can until that shape has a model — which per the spec
  needs a drain-time price the bars may not carry.

  How urgent that is was counted rather than inferred from the cohort. Over all 53 records in the
  same report: **38 picks are pure `pullback`, 3 are pure `immediate`, 1 is mixed**, and 11 carry no
  `entry_tiers` at all (the legacy plan shape). By declared tier it is **46 `pullback` against 4
  `immediate`**. Every `immediate` pick sits on ONE trade date, 2026-10-30, against ten trade dates
  carrying a `pullback` pick. The owner confirms on 2026-11-03 that `immediate` is NOT the default
  entry mode, so the shape is an exception on one day rather than a drift in how picks are written,
  and the panel will keep growing from `pullback` picks — `MSFT:2026-10-30` is the next one, still
  open. Three of four picks on one day is a single-day number and was read as a trend here before it
  was counted; the counts above are what the question should rest on.
- **13 of 24 closed picks, not 24.** The other eleven are excluded for `legacy_plan_shape`,
  `entry_mode_unsupported`, `manual_close` or `placed_late`. This panel describes the subset the
  tool accepts, and that subset is not a random sample of the owner's trading.
- **Section 9.5's "11 of 11 touches" was not re-counted and cannot be from this corpus.** Five of
  the thirteen have no `touched` line left at all, as 10.6 states.
- **Section 9.1's channel decomposition** (entry channel ΔR −0.5115, exit-fill channel −0.0534) and
  **section 9.4's tape figures** were not re-derived here. 10.7 re-derives the two totals and
  nothing finer.
- **No PLN figure is published.** The record carries `pnl_cash_acct` and `notional_spent_acct`, so an
  account-currency return is computable for all thirteen, but the replay produces a USD price return
  and differencing the two would subtract quantities in different units. The ΔP&L column is
  deliberately absent from 10.2 for the same reason the R columns carry 10.3's warning.
- **Fees are in neither side's percentage.** `fees` is null on all thirteen LIVE records and
  `fees_not_included` names financing, dividends and withholding tax.
- **Nothing here measures the arming-time channel**, which #1848 documented and measured at zero
  effect on the picks then available. VCYT's `ΔT entry` of +1101.8 minutes is that channel showing
  up on the one pick where it bites.

  **SUPERSEDED by section 11 (2026-11-12): `MSFT:2026-10-30` measures it. The replay filled 248.6
  minutes before the plan was armed, and on that pick the channel costs 0.79 R and changes which
  exit leg fires. Read section 11 before repeating that the channel is unmeasured.**

## 11. Measurement, 2026-11-12: the arming-time channel is not a bias, it reroutes the trade

Status: **MEASURED**, 2026-11-12. `MSFT:2026-10-30` closed on 2026-11-04 and became replayable today,
the first day the vendor serves the session its entry ladder needed. It is the 14th comparable closed
pick and it produces the **largest divergence on record**, larger than VCYT's.

This section SUPERSEDES IN PART section 10.9's bullet "Nothing here measures the arming-time
channel", and section 10.6's `max |ΔR|`. Sections 1 to 9 do not move, and 10.1 to 10.5, 10.7 and 10.8
do not move: 10.2's thirteen rows were measured on a panel this pick was not in.

### 11.1 What was run

```bash
# VPS, read-only; report generated 2026-11-12T08:38:00.679Z, 53 records
alphalens broker trades --env live --format json --all
scp jacoren@vault:'~/.alphalens/broker_orders/live/entry_trails.jsonl' .   # 194 lines
# apps/alphalens-research, repo venv, POLYGON_API_KEY exported from the main checkout's .env
python -m scripts.replay_from_pick <report> MSFT:2026-10-30 --out <dir> \
    --bar-cache ~/.alphalens/replay_bars --entry-trails entry_trails.jsonl --format json
python -m intent_replay run <dir>/MSFT_2026-10-30.document.json \
    --config <dir>/MSFT_2026-10-30.config.json --bars <dir>/MSFT_2026-10-30.bars.json
```

One vendor request, no cache hit: 6950 bars fetched, **3510 kept** on the XNAS half-open grid over 9
sessions, 9 dropped at a close and 3431 off grid. `entry_trail_bps` **50**, read from the tier's own
`watch_open` line. The document declares `disaster_stop` 496.1, TP1 540.74 at 100%, and a
`trailing_stop` reaction with `arm_trigger_r` 0.4 and `trail_frac` 0.6.

The replay reports three divergences by name: `daemon_trail_guards`,
`native_entry_trail_is_a_broker_model`, `take_profit_observation_time`. One bar needed the intrabar
tie rule (`snu_bars` 1, `entries_then_stop_then_ladder`).

### 11.2 The two sides

| | LIVE | replay | replay − LIVE |
|---|---:|---:|---:|
| entry instant | 2026-10-30T19:11:38.029Z | 2026-10-30T13:39:00Z | **−332.6 min** |
| entry price | 520.92 | 516.9421 | **−3.9779** |
| exit instant | 2026-11-04T14:30:31.104Z | 2026-11-02T14:33:00Z | **−4317.5 min** |
| exit price | 546.63 | 522.06484 | **−24.5652** |
| exit mechanism | `take_profit` (`u261-tp1-sell`) | **`closed_stop`** (trailed) | **disagrees** |
| R | +1.0359 | +0.2458 | **−0.7901** |
| R denominator | 24.8200 | 20.8421 | **−16.03%** |
| P&L of spent | 4.9355% | 0.9910% | **−3.9445 pp** |

`|ΔR| = 0.7901` exceeds VCYT's 0.5648 by 1.4×, so section 10.6's "largest is 0.5648 (VCYT)" holds
over its own thirteen picks and no longer over the comparable closed set.

### 11.3 The entry gap is the arming head start, and almost nothing else

The plan was armed at 17:47:35Z. The live watch cannot begin before that instant; the replay's
`walk_start` is the day-1 session open, 13:30Z. The replay filled at **13:39:00Z — 248.6 minutes
before the plan existed**.

Session 1 holds 390 bars. Split at the arming instant:

| window | bars | lowest low |
|---|---:|---:|
| 13:30:00Z to 17:47:35Z (before arming) | 258 | **514.3500** |
| 17:47:35Z to 20:00Z (after arming) | 132 | **518.3300** |

The daemon journaled its bid trough at **518.32**. The replay's trough, recovered from its fill
(`516.9421 / 1.005`, the native trail's trigger arithmetic at `d_bps` 50), is **514.3702** — the
pre-arming minimum.

So the two series agree to **one cent** on the window the live watch could actually see, and the
whole 3.98 USD entry gap comes from 258 bars the replay was allowed to watch and the daemon was not.

### 11.4 The bid-versus-tape reference gap, measured at last, is about one bar

This is the first pick that carries both a journaled bid touch and a comparable replay, because
#1816 landed on 2026-10-29 and this pick was armed the next day — every earlier pick lost its
`touched` line to boot compaction (10.6 counts them).

Measured on the window the live watch could see: the first bar after arming whose LOW reaches the
declared limit 518.42 is stamped **18:53:00Z** with a low of 518.33. The bid touched 518.32 at
**18:54:23.891Z**. The tape therefore reaches the rung **1.4 minutes** before the bid, which is one
1-minute bar plus 23 seconds, with the bar stamped at its start.

**One observation is not a bias.** The direction here is the one a trade-through makes likely, and
`apps/intent-replay/CLAUDE.md` already states why it is not structural: a trade can print below the
displayed bid. What this adds is a magnitude on one pick — a penny of price and about a bar of time —
against which the 3.98 USD entry gap of 11.3 is two orders of magnitude larger. **On this pick the
reference gap is not what moved the number.**

### 11.5 Why a better entry produced a worse trade

The replay entered 3.98 lower, which shrank its R denominator from 24.82 to 20.84, which lowered its
trail arm threshold:

| | entry | denominator | trail arms at `entry + 0.4R` | first bar with a high that reaches it |
|---|---:|---:|---:|---|
| replay | 516.9421 | 20.8421 | **525.2789** | 2026-10-30T13:30:00Z |
| LIVE | 520.9200 | 24.8200 | **530.8480** | **2026-11-04T14:30:00Z** |

The LIVE arm threshold was first reached in the same minute as the take-profit fill. **The real
position's trail never armed.** The replay's did, and `trail_frac` 0.6 then parked its stop at
`516.9421 + 0.6 × (525.50 − 516.9421)` = **522.077**, against the 522.06484 its trace records, a
1.2-cent agreement. A retracement on 2026-11-02 took it out.

So the four hours the replay should not have had did not make it luckier. They moved its entry down,
which moved its R scale down, which armed a trail the real trade never armed, which ended the trade
**2 days 23 h 57 min early** at 24.57 USD below the real exit. The arming channel is **not a
signed bias on an entry price**; on this pick it changed which exit leg fired.

That answers the question #1848 left open — whether `walk_start` should be the later of the day-1
session open and the arming instant — with a measurement rather than a preference: the choice is not
cosmetic. It does not decide it. Changing `walk_start` makes the replay agree with the daemon's
HORIZON, and the spec's §2.1 rule still holds that a knob cannot supply a missing fact. What is now
measured is the price of leaving it alone, on one pick: 0.79 R and a different exit leg.

### 11.6 What this does NOT settle

- **One pick.** Every figure here is one observation with its construction attached. The arming
  channel's effect was measured at exactly zero on the 2026-11-03 panel because no pick there
  exhibited the gap; it is measured large here. Two measurements, two populations, no distribution.
- **The R comparison carries 10.3's warning.** The denominators differ by 16.03%, and that difference
  is not noise here — it is the mechanism. Quoting `ΔR = −0.7901` without the denominators is
  quoting the effect while hiding its cause.
- **Nothing is re-measured on the other thirteen.** Section 10's panel stands as published. This pick
  takes the comparable closed set to 14; the table in 10.2 was not re-run.
- **The exit-timing direction now has a counterexample in magnitude, not in sign.** The replay still
  exited earlier, as on 13 of 13 before it, but by 4317.5 minutes and for a different reason: not an
  early fire of the same leg, a different leg.
- **`mfe_lower_bound` is null on the LIVE side** for this pick. The exit was TP1 and no `trailed`
  line was ever written, so the LIVE record holds no high-water mark to compare against the replay's
  `mfe` of 0.4106 R.
- **The tape-versus-bid magnitude rests on 1-minute bars.** A bar stamped at its start cannot resolve
  a 23-second difference, so "1.4 minutes" is the resolution floor, not a measurement of the lead.
