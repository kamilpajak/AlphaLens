# What the market-cap bracket costs — read 1 of N

Date: 2026-08-22
Contract: [`mcap_bracket_cost_contract_2026_08_22.md`](mcap_bracket_cost_contract_2026_08_22.md)
(committed `44d852cd`, 18:32:33 +02:00, before any outcome existed)
Amendments in force at this read: 1, 2

## Verdict

**INCONCLUSIVE.**

The maturity floor in §8 requires 30 rows per arm carrying a realised R. The
discarded arm has 12; the kept arm has 3. The contract pre-committed this
outcome as the expected result of a first read and this read is it.

Everything below is published because §8 requires the numbers to be shown with
their N. None of it decides anything, and §13 forbids substituting a secondary
for an inconclusive primary.

## Sample

| | |
|---|---:|
| funnel days | 16 (2026-08-06 .. 2026-08-21) |
| proposals in scope, unit `(asof, ticker)` | **413** |
| — discarded arm (`too_big`) | 320 |
| — kept arm (`in_bracket`) | 93 |
| distinct days reaching a terminal row | 9 |

Excluded by §3, counted here because the clause forbids omitting them:
`too_small` 45, `no_mcap` 50. Neither is in either arm; `too_small` asks about
the floor rather than the ceiling, and `no_mcap` has no cap to classify on.

Attrition (§10, sums to 413 per Amendment 1):

| bucket | n |
|---|---:|
| replayed, terminal | 44 |
| replayed, ongoing | 369 |
| no structure | 0 |
| no bars | 0 |
| unmatched arm | 0 |

Every one of the 413 proposals produced a plannable ladder from daily OHLCV, and
every replayed row matched back to its arm.

## Positive control (§10)

| | |
|---|---:|
| kept-arm rows overlapping the production store | 77 |
| comparable (classified in both) | 77 |
| **classification agreement** | **88.3%** |

The nine disagreements are concentrated on the `NO_FILL` / `OPEN` boundary —
an entry limit touched in one store and missed in the other by a small margin.
That is the expected signature of the one deliberate difference between the two
paths: this replay builds every setup from the whole-market daily store so both
arms share a single OHLCV source, where production builds from a per-ticker
cache that exists only for names that reached the score stage.

The control is satisfied. The synthetic-brief path does not diverge from
production in a way that would invalidate the run.

## Numbers, with their N

| | discarded | kept |
|---|---:|---:|
| proposals | 320 | 93 |
| terminal rows | 34 | 10 |
| **rows carrying a realised R** | **12** | **3** |
| median realised R | 0.220 | 0.135 |
| `NO_FILL` rate | 29.0% | 31.5% |

Terminal classification mix:

| | discarded | kept |
|---|---:|---:|
| OPEN | 151 | 43 |
| NO_FILL | 90 | 29 |
| PARTIAL_TP_OPEN | 57 | 17 |
| TP_FULL | 11 | 3 |
| SL_HIT | 1 | 0 |

Above $50B (§7 split): n = 5, median realised R 0.109.

**Read none of this as a result.** Twelve and three observations, nine days, no
interval computed because the floor was not met. The two medians differ in the
direction that would matter, and at these counts that is indistinguishable from
noise; the contract exists precisely so that this sentence cannot be replaced by
a more interesting one.

## Prompt-change stratum (§9)

Split at 2026-08-18, never pooled:

| | discarded n | median R | kept n | median R |
|---|---:|---:|---:|---:|
| before | 10 | 0.220 | 2 | 0.209 |
| on or after | 2 | −0.173 | 1 | 0.135 |

Almost every observation so far predates the prompt change, which is what a
42-session hold implies: the newer proposals have had less time to terminate.
The post-change cells hold two and one observation.

## Theme-stratified secondary (§9)

Ten themes have at least one realised R. Nine of the twenty cells hold exactly
one observation and only two themes have rows in both arms:

| theme | discarded n / median | kept n / median |
|---|---|---|
| ammunition_shortage | — | 1 / 0.057 |
| currency_intervention | 1 / 0.055 | — |
| flu_vaccine | 3 / 0.231 | — |
| melanoma | 1 / −1.000 | 1 / 0.135 |
| minerals | 2 / 0.109 | — |
| mrna_technology | 1 / 0.252 | — |
| mrna_vaccine | 1 / 0.221 | 1 / 0.361 |
| obesity | 1 / 0.250 | — |
| risk_appetite | 1 / 0.653 | — |
| tech_rally | 1 / 0.121 | — |

This is the clearest picture of why the primary is inconclusive. It also shows
the confound §9 named: most themes contribute to one arm only, so a pooled
difference between arms is partly a difference between themes.

## A secondary that could not be computed

`market_excess_return` is null on every replayed row in both arms, so the §7
secondary that uses it is absent from this read rather than reported as zero.
The column is populated in the production store, so this is a property of the
replay path, not of the data. Named here as an open item for read 2.

## What this read does establish

Three things, none of them the question:

1. **The measurement exists.** Before today the project had no ladder outcome
   for any above-bracket proposal, because the filter runs before a card is
   created — all 901 rows in the production store are in-bracket. There are now
   320 discarded-arm rows under replay, resolving nightly.
2. **The two arms are comparable.** Both were built from one OHLCV source, run
   through the production monitor, and both cleared setup construction with zero
   attrition. The bracket is the only difference between them.
3. **Fill behaviour is close between arms.** 29.0% vs 31.5% `NO_FILL` is the one
   figure here whose denominators are not tiny (124 and 92 classified rows). It
   suggests the ladder geometry is not systematically unreachable for the larger
   names — worth watching, not concluding.

## When this can be answered

The kept arm is the binding constraint, as §8 said it would be. Of its 93 rows,
29 are `NO_FILL` and cannot contribute a realised R; 60 are still open. Reaching
30 needs roughly half the currently-open kept positions to terminate.

`TIME_STOP_DAYS` is 42 trading days, so the oldest proposals force-terminate
around **2026-10-06**. Earlier terminality only arrives via stop or full target.
The funnel also grows daily at no cost, which raises both arms.

Re-read weekly per §11, each read stating its own N. The next read is not
expected to clear the floor.

## Review trail

Three errors were made building this, all the same shape — **code that guarded
my assumption about the data instead of the data** — and all three passed their
tests before being caught:

1. The plannability guard read `structure` / `entries`; the real `TradeSetup`
   emits `status` / `entry_tiers`. Every row was rejected and the first run
   reported 413 of 413 without structure. The test had used a hand-written dict
   shaped the way the guard expected. Tests now assert against payloads the real
   builder produced.
2. The bootstrap's seed-sensitivity test used a fixture where every day carried
   the same median, so resampling days could not move the estimate and an
   implementation ignoring its seed would have passed.
3. The positive control counted rows the replay had not yet classified as
   disagreements, reading 61% while the rows it had resolved agreed 86% of the
   time. The same function already contained an explicit guard against exactly
   this confusion one level up, for the empty-overlap case.

Two contract defects were also found and amended, both recorded with the reason
the resolution cannot favour a preferred answer: §10's row count contradicted
§2's unit (Amendment 1), and §6 assumed the engine assigns `NO_FILL` a numeric
value when it assigns null (Amendment 2).

A fourth error, and the one this project has paid for before: the first draft of
the read script **did not implement three clauses of its own contract** — §3's
requirement to count the excluded verdicts, and §9's prompt-change stratum and
theme-stratified secondary. All three were absent, and the memo was written
without noticing. They were found by walking the contract clause by clause and
naming the line that implements each, which is the check that should have run
before the first read rather than after it.

The read script was run once against the partially-drained store as a smoke test
before the final read, so interim numbers were seen. The verdict logic was
already committed and tested at that point, so the verdict could not move; this
is recorded because the prose could have been influenced and the reader should
be able to weigh that.
