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

`VCYT:2026-09-29` is the only pick where the outcome word differs: the record says `never_filled`, the replay says `open` with `filled_fraction` 1.0 at an average entry of 43.80.

Both sides agree on the level. The document declares one `pullback` tier at `limit_price` **44.01**, and the daemon's own `planned_limit` is **44.01** too. The tape then settles it: **698 minute bars at or below 44.01 before the entry deadline, the first at 2026-10-07T13:30:00Z.**

That instant is four days AFTER the report's `NOW` of 2026-10-03T15:30Z. So the record's `never_filled` is true of the span the record covers, the replay's fill is true of the span the replay walks, and the two spans differ. The record's `entries[0].touched_at` is null with `null_reason: not_journaled`, which is what a watch that has not fired yet looks like.

**A ~45-second poll cannot miss a 698-minute condition**, so this is not the entry-trail divergence and not a missed fill. It is a comparison of two different horizons, and it is the reason section 2 narrows the panel.

## 5. The window gate refused exactly what it was designed to refuse

Three picks refused with `bars_window_unservable` / `deadline_not_yet_servable`, exit 7. Their refusal names its own arithmetic — for `BE:2026-09-30`:

> the entry ladder was live through session 2026-10-09 (entry_deadline 2026-10-09T20:00:00.000Z). The session grid is half-open at the close, so the ladder's expiry is only observable on the NEXT session, 2026-10-12 (close 2026-10-12T20:00:00.000Z), and the price vendor serves only past calendar days. The last session it can serve today is 2026-10-09.

`details.servable_from_date` is **2026-10-13**, which is what the bar-fetch design's own measurement predicted for this panel ("3 remain at 2026-10-12 and none at 2026-10-13"). The whole cost of the hard refusal, on this panel, is a wait of two calendar days on a pick whose ladder expired the day before yesterday. It is open question Q3 of that design, and this run is the first measurement of what it actually blocks rather than of how often it fires.

## 6. What this does NOT settle

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
