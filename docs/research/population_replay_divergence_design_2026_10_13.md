# Design: does charging costs change WHICH candidates look good, or only how good they look?

Status: **REJECTED**, 2026-10-13 — see section 9. The money question it was built to answer is settled there by arithmetic, as a null, and the design is refused on five measured blockers. Section 9 is the only part a later reader needs. Adversarial review comes before any compute, per this repository's
rule for a design of this class. Code facts come from `50f286e7`; store facts carry their host and
their read timestamp. Nothing in the repository was modified while this was written.

The question in one paragraph. `/edge`'s realized population measure is produced by
`feedback/ladder_replay.py`, whose fill model executes a level AT that level the first time price
touches it and which has no money dimension at all: a case-insensitive grep for `commission`,
`_bps`, `currency`, `notional` and `whole_share` over its 1910 lines returns **0** matching lines.
`apps/intent-replay` charges commission, FX, the whole-share lattice and a cost gate, and applies a
half-open session grid and the SNU intra-bar rule. The owner's decision of 2026-10-13 deprecates the
**what-if** layer in favour of that engine (epic #1526). This memo asks the separate question that
decision does not settle: whether the REALIZED measure should follow. The answer turns on one
observable — whether the costed basis reorders the candidates or merely shifts the level — and this
memo is the design that produces it.

---

## 1. Why this is a question and not a migration

Three facts argue AGAINST re-basing the realized signal, and they are the reason this is an
experiment rather than a task.

- **`/edge`'s headline does not come from the ladder engine.** `apps/alphalens-django/edge/api/summary.py:7-9`
  pins the doctrine: "headline central tendency is the BENCHMARK-EXCESS return
  (`market_excess_return`), NOT raw R. Raw `realized_r` is reported only as a de-emphasised
  'gross / risk-normalised' mean." `market_excess_return` is `forward_return - benchmark_window_return`
  (`feedback/benchmark_excess.py:50`), which needs no ladder replay.
- **Gross / pre-cost is a stated decision.** Same docstring, §3.4: "every R / excess number is
  gross / pre-cost."
- **Costs are account facts, and `/edge` is not about the account.** `RunConfig.from_jsonable`
  takes `account_currency` as a REQUIRED keyword (`intent_replay/config.py:261`) because "a block
  able to state it would be able to disagree with the document it replays" (`:267-270`), and the
  interpreter reads `spec.size.notional_acct` and `spec.size.currency` by path. A re-based `/edge`
  would move when the owner's budget moves. CLAUDE.md draws this line itself: `/edge` means "does
  the pipeline select well?", never "how is the owner's money doing".

Against that stands one fact that could overturn all three: if charging costs REORDERS the
candidates, then the cheap measure is not merely coarse, it is misleading, and no amount of doctrine
rescues it. That is the observable this design is built to produce.

## 2. What a naive run would have produced, and why it is refused

Four integration facts, each measured, each a place where a careless harness yields a number that
reads as a divergence of the engines and is a divergence of the harness.

**2.1 There is no batch input, and it is not a small addition.** The command takes one document, one
`--config` and one `--bars`. Open issue #1611 records why none of the three generalises: the
configuration is per document (`walk_start` resolves `meta.source` plus `meta.trade_date`,
`entry_deadline` resolves `spec.order_ttl_days` against the venue calendar), the bars are per
instrument, and a refusal mid-stream cannot satisfy the contract's "stdout is empty on a refusal".
So the harness drives the engine **in process**, once per episode. That is feasible and has
precedent: `apps/alphalens-research/scripts/replay_from_pick.py:56-58` already imports
`intent_replay.units`, `intent_replay.bars._BAR_KEYS` and `intent_replay.fx.Fx`.

**2.2 The two bar shapes share exactly one key.** The engine models
`_BAR_KEYS = ("t", "open", "high", "low", "close")` (`intent_replay/bars.py:72-74`) and REFUSES an
unknown key — `bars_invalid`, `reason="unknown_key"`, "bar {index} carries {...}, which a bar does
not model" (`bars.py:215-220`). The population store writes
`_BAR_COLUMNS = ("t", "o", "h", "l", "c", "v")` (`feedback/population_ladder_monitor.py:230`). So a
handoff of store bars refuses on bar 1: the price fields need expanding from single letters and `v`
must be dropped. This is a translation, not a load.

**2.3 The population bars are UNADJUSTED, and the monitor knows it.** `population_ladder_monitor.py:781-784`
carries a detector for exactly this — "True when the replay's forward return is a likely
unadjusted-split artifact (`adjusted=false`). When True the caller skips + carries the prior row".
A replay against raw prices is the right basis for "what would have happened", because those are
the prices that traded; but a corporate action INSIDE the window makes the brief's levels and the
bars disagree, and the harness must refuse such an episode rather than price it. `replay_from_pick.py`
already has the machinery (a cached corporate-actions lookup, FOUND kept forever, NONE-FOUND for 14
days) and the action-window padding constants.

**2.4 The session grid is not in the engine.** A grep for `session_open|session_close|calendar` over
`apps/intent-replay/intent_replay/*.py` hits only two prose lines. The half-open grid, the
close-stamped-bar drop and the off-grid refusal live in `apps/alphalens-research/scripts/replay_from_pick.py`
— the bar-preparation step. So the store read must enter through that same drop-with-count path, or
the comparison silently inherits a basis difference that belongs to neither engine.

## 3. The pool

Measured on the Mac mirror, host `Mac.localdomain`, read 2026-10-13. Construction: pandas concat
over `~/.alphalens/population_ladders/*.parquet` (111 files), filtered on the `terminal` column —
the column is `terminal`, not `is_terminal`, and a predicate on the wrong name silently counts every
row.

| Quantity | Value |
|---|---|
| rows | 1044 |
| terminal rows | 629 |
| terminal with finite `realized_r` (the `/edge` pool) | **482** |
| distinct tickers in that pool | 176 |
| `TIME_STOP` / `TP_FULL` / `SL_HIT` / `PARTIAL_TP_THEN_SL` | 193 / 188 / 85 / 16 |
| terminal rows OUTSIDE the pool (`NO_FILL`, null `realized_r`) | 147 |
| bar parquets on disk / size | 881 / 153 MB |

The VPS store is authoritative and larger; every number here is re-derived there before the run, and
the run itself happens there.

**The 147 excluded episodes are a design problem, not a rounding detail.** `/edge`'s own pool
definition drops them (`summary.py:128-130`: "NO_FILL terminals carry a NULL `realized_r` and drop
out"). But a fill-model difference shows up precisely as a changed fill: an episode the lens scored
`NO_FILL` may fill under the costed engine, and an episode the lens filled may fail to. A ranking
comparison restricted to the lens pool would silently discard every such case, which is the subset
most likely to carry the divergence. So the primary comparison runs on the 482, and a SECOND,
mandatory table reports the fill-status transitions across all 629 — including the direction
`NO_FILL -> filled`, which the ranking cannot see.

## 4. Three arms, because one number cannot be acted on

A single divergence figure would mix four mechanisms: cost charging, the fill model, the session
grid and the SNU intra-bar tie rule. This repository has already paid for that confusion once — two
mechanisms producing the same total, separable only by a third quantity. So the design is three arms
over the same episodes, and the costs are declared run values, which makes the middle arm possible:
`commission_rate`, `min_commission`, `min_commission_applies`, `exit_edge_min_bps`,
`round_trip_cost_rate`, `sizing_buffer_pct` and `mid_rate` are all config keys
(`intent_replay/config.py:199-236`).

| Arm | What it is | What the comparison isolates |
|---|---|---|
| **0 — stored** | the existing `realized_r` from the lens engine, as stored | the baseline `/edge` uses today |
| **1 — mechanics only** | intent-replay with every money term neutral: zero commission, no minimum, `exit_edge_min_bps` 0, no round-trip rate, no sizing buffer, size declared in USD so FX is a no-op | **0 vs 1** = fill model + half-open grid + SNU, with money removed |
| **2 — as it would run** | intent-replay with the production fee card for the venue and the real cost gate | **1 vs 2** = the money terms alone |

Two implementation facts to settle rather than assume, both marked here so the review can attack
them: whether the config REFUSES a zero for any of those keys (it refuses a `0` for
`entry_trail_bps`, so the pattern exists), and whether a USD-declared size genuinely makes FX inert
(`config.py:61` says the fx key set is one entry when the instrument code equals `account_currency`
and four when they differ). If either fails, arm 1 is built by computing the money term analytically
per episode and subtracting it, and the memo must be amended to say so before the run.

## 5. The document each episode gets

Synthesised per episode from the brief payload, with every value either read from the store or
declared once in the config. Nothing is written as a literal that the document or the store can
supply.

- `instrument`: `ticker` from the store row; `mic` is the declared constant **XNYS**. Justification,
  measured rather than assumed: the three US venues share one fee card
  (`brokers/automanager/costs.py:121-124`), and over 2022-01-01 to 2026-10-01 XNYS, XNAS and XASE
  hold 1191 sessions each with identical session dates, opens and closes. So the MIC choice cannot
  move a number for a US panel. A non-US ticker in the pool is an abort condition, not a default.
- `spec.entry_tiers[]`: `limit` -> `limit_price`, `alloc_pct` carried across, `entry_mode` omitted so
  the schema default `"pullback"` applies. The engine REFUSES `immediate`
  (`ENTRY_MODE_UNSUPPORTED_CODE`, `interpreter.py:92`), and the brief emits no `entry_mode`, so this
  is consistent rather than lucky.
- `spec.disaster_stop`: carried across.
- `spec.tp_tranches[]`: `target` -> `price`, `tranche_pct` carried. **`r_multiple` is NOT supplied** —
  both doors refuse a supplied derived field.
- `spec.size`: the ONE free parameter, declared once and identical for every episode, in USD. Its
  value is a nuisance parameter for a ranking question and a real one for the cost arm, because the
  whole-share lattice and the commission minimum are size-dependent. So arm 2 is run at **three**
  sizes and the memo reports whether the ranking verdict is stable across them. A verdict that holds
  at one size and not another is itself the finding.
- `meta.source`: `"manual"`, with the day-1 anchor that implies. This is a KNOWN divergence from the
  population measure, which anchors on `ladder_arrival_session`, and it is why §7 lists the anchor
  as the first thing the review should attack.
- `meta.trade_date`: the store row's brief date, advanced by the same rule the population monitor
  uses, so the two measures start on the same session.
- `spec.order_ttl_days`: from the brief payload, with the `0`-means-default sentinel preserved.

## 6. The statistic, and the decision rule stated in advance

Fixed before the run so the answer cannot be chosen after seeing it.

- **Primary:** Spearman rank correlation between arm 0 and arm 2 over the 482. Reported with a
  cluster bootstrap by ticker, because 482 episodes sit in 176 tickers and the episodes of one
  ticker are not independent.
- **Secondary, and the one that decides the action:** the top-decile set overlap. `/edge` is used to
  judge whether the pipeline selects well, so what matters is whether the same candidates land at
  the top, not whether the whole series correlates.
- **Mandatory third:** the fill-status transition table over all 629, with counts in both
  directions.
- **Decomposition:** the same three statistics for 0 vs 1 and 1 vs 2, so a reordering is attributed
  to mechanics or to money.

Decision rule:

| Observation | Action |
|---|---|
| rank correlation high AND top-decile overlap high | the cheap instrument is vindicated; `/edge` keeps `ladder_replay`, and the memo's conclusion is recorded in `feedback/__init__.py` so this is not re-litigated |
| top-decile overlap breaks, attributed to MONEY (1 vs 2) | `/edge`'s gross/pre-cost doctrine is the defect; re-base, and the old `edge_ladderoutcome` rows are cut at a date rather than recomputed |
| top-decile overlap breaks, attributed to MECHANICS (0 vs 1) | the lens engine's fill model is wrong independently of costs, which is a bug report against it, not an argument for charging costs |
| the verdict flips across the three sizes of arm 2 | the question is mis-posed: a size-dependent ranking means `/edge` cannot be both account-free and cost-charging, and that goes back to the owner |

## 7. What this is NOT, and the abort conditions

- **Not an alpha claim.** No strategy is being tested, no hypothesis about returns is being
  registered, and this consumes no multiplicity budget. It compares two instruments on one fixed
  population. Anyone later quoting a number from it as evidence about edge is misusing it.
- **Not the retirement of the what-if layer.** That is decided and lives in #1526. This experiment
  reuses the brief-to-intent adapter that epic needs anyway, which is why the work is not throwaway
  even if the answer is "keep the cheap instrument".
- **Abort before publishing any number** if: a non-US ticker appears in the pool; a corporate action
  falls inside an episode's window and the episode cannot be refused cleanly; the bar translation
  drops or gains a bar without an accounted reason; or arm 1 cannot be built as §4 specifies.

## 8. Questions for the review, in the order they would most change the design

1. **The anchor.** The population measure anchors on `ladder_arrival_session`; a `"manual"` document
   anchors day 1 on `meta.trade_date`. Neither `meta.source` value reproduces the lens anchor on
   every date. Does that make the comparison unsound, or is it a bounded offset to be measured and
   reported?
2. **Is the top-decile set the right secondary?** It is chosen because `/edge` is a selection
   instrument, but a decile over 482 episodes is 48 names, and the bootstrap may not separate it
   from chance.
3. **Is three sizes enough**, and are the three the right ones, given the whole-share lattice bites
   hardest at small notionals?
4. **Does arm 1 answer a real question**, or is a cost-free intent-replay an instrument nobody would
   ever run, making 0 vs 1 a comparison of two hypotheticals?
5. **What would make this experiment worth NOT running?** If the answer "keep the cheap instrument"
   changes nothing, and the answer "re-base" would be refused on doctrine anyway, then the measurement
   is decoration. The reviewer should say so plainly if that is the case.


---

## 9. Review verdict, 2026-10-13 — do not run; the money question is settled by arithmetic

Two adversarial passes ran on this memo before any compute: one reading it against the code, one on
the statistical design. Both refuse it, and the first settled the underlying question in ten minutes
without an engine. Every number below was re-derived by hand before being written here.

**The premise of section 1 is wrong: `intent-replay` charges nothing to the result.** Its R is gross
— `pnl_cash = _proceeds(result.events) - spend` (`intent_replay/measures.py:121-122`), with no fee
subtracted anywhere; `commission` appears **0** times in `walk.py`; the whole-share lattice is absent
and the engine says so (`cost_gate.py:27-28`, "**No whole-share lattice.** The daemon prices the
WHOLE-SHARE notional; this prices the stated budget"), with fractional units at `walk.py:217`; and FX
cancels everywhere but the gate. Fees enter through exactly one mechanism: the take-profit cost gate,
which DEFERS a tranche whose exit would not clear cost. So "charging costs" cannot lower an R number;
it can only move when a tranche fires.

**And it cannot do that either, on this population.** The gate threshold is
`entry * (1 + (fee_bps + exit_edge_min_bps)/1e4)` (`cost_gate.py:118`) with `COMMISSION_RATE = 0.0008`,
`MIN_COMMISSION_USD = 1.0` and `EXIT_EDGE_MIN_BPS = 50.0` (`brokers/automanager/costs.py:53,57,203`),
so the floor is 66 bps above the fee knee and about 80 bps at the owner's real 8 000 PLN position
(three tranches, ~667 USD each, under the 1 250 USD knee). Measured over every `status == "OK"` setup
in `~/.alphalens/thematic_briefs/*.parquet` (host `Mac.localdomain`, read 2026-10-13; construction:
alloc-weighted blend from `entry_tiers`, gap to each `tp_tranches[].target` in bps):

| Quantity | Value |
|---|---|
| take-profit tranches measured | 2553 |
| smallest gap above the blend | **91.2 bps** |
| 1st percentile / median | 542.9 / 3122 bps |
| tranches below 66 bps | **0** |
| tranches below 80 bps | **0** |
| tranches below 110 bps | 1 (0.04%) |

The nearest take-profit in the whole store sits 91 bps above the blend against a threshold of at most
80. The gate has nothing to bind on, so arm 2 — 482 episodes times three sizes of engine runs — would
have measured a null it took one query to establish. `/edge`'s `gross_of_cost: True` is therefore
correct on the evidence and not merely by doctrine, and the realized measure keeps `ladder_replay`.

**Five measured blockers would have corrupted the run had it gone ahead**, each in the harness rather
than in either engine. `time_stop_t` is hardcoded to `None` on the path section 2.4 names
(`scripts/replay_from_pick.py:296`), so the 193 `TIME_STOP` episodes — 40% of the pool — would never
close. The entry deadline differs by one full session: the population blocks a fill at the session
OPEN of expiry (`feedback/population_ladder_monitor.py:495`) where `pick_window` gives its CLOSE
(`brokers/automanager/pick_window.py:68-69`), measured at 6.5 hours on 200 of 200 sampled episodes,
landing exactly on the `NO_FILL -> filled` cell section 3 calls the most interesting. The bar path
section 2.4 names REFUSES an off-grid bar on a supplied series rather than dropping it
(`replay_from_pick.py:1603-1608`), and 7.63% of cached bars are off-grid, so every file would be
refused. The brief-store join is unspecified, and 83 of 629 terminal rows (13%) have no recoverable
payload. And the independence unit contradicts this repository's own rule for this panel: rule 5
collapse gives 247 episodes in 51 arrival-session clusters, not 482 in 176 tickers, with no replicate
count stated where the house figure is B = 10 000.

**What the reviews settled in this memo's favour, and what survives as a separate question.** The
field mapping of section 5 is sound: 546 of 546 synthesized documents pass the replay's own door with
zero refusals, and `planned_blended_entry` equals the stored `full_ladder_blended_entry` on 546 of 546
with a maximum relative difference of 0. Arm 1 is buildable — zero is accepted for every cost
magnitude it needs — so section 4's worry and section 7's matching abort were dead branches. Question
1 was wrong in the other direction: the anchors agree on 84 of 84 brief dates. What remains is a
different experiment under a different title, and this memo is not it: **is `ladder_replay`'s fill
model wrong independently of costs?** It would be dominated by a signed asymmetry neither engine's
documentation advertises — on a gap-down bar `intent-replay` buys at `min(bar.open, limit_price)`
(`walk.py:357-360`) where the lens buys at the limit, and 290 of 629 terminal rows filled exactly one
tier, so the day-1 gap is the modal case, not the tail.
