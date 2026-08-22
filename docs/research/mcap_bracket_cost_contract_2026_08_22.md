# Analysis contract — what the market-cap bracket costs

Status: LOCKED
Date: 2026-08-22
Template: `docs/research/experiment_analysis_contract_2026_08_07.md`
Related: #1002 (why so few in-bracket proposals), `docs/research/proposal_funnel_first_read_2026_08_21.md`

This document is committed BEFORE any outcome is computed. Its commit timestamp,
strictly preceding the commit of any result, is the evidence that the rules below
were fixed in advance. Nothing in it may be edited after the first result commit;
a change of mind becomes a numbered amendment appended at the bottom.

## What was known before writing this

Read from disk while designing (sampling frame only, no outcome):

* the funnel covers 2026-08-06 .. 2026-08-21, 16 days;
* 418 `too_big` proposals over 122 distinct tickers; 97 `in_bracket`; 45
  `too_small`; 50 `no_mcap`;
* the 2026-08-18 prompt change roughly quadrupled proposals per day (18 -> 75
  `too_big` on that date), so day sizes are strongly unequal;
* `~/.alphalens/population_ladders/` holds 901 rows over 95 brief days, 262
  tickers, of which 536 are terminal — and EVERY row is in-bracket, because the
  bracket filter runs before a card exists;
* `TIME_STOP_DAYS = 42` trading days, `DEFAULT_ORDER_TTL_DAYS = 7` trading days;
* `build_trade_setup(ticker, asof, loader)` is deterministic from OHLCV — no LLM
  is involved in the ladder geometry;
* `replay_population_ladders(briefs_dir, store_dir=...)` accepts an injected
  briefs directory and store, so both arms can run through identical code.

No ladder outcome for any `too_big` row has been computed or looked at. No
`realized_r` has been read for any arm broken down by market cap.

## 1. QUESTION

The pipeline discards roughly three quarters of what the mapper proposes because
the company is above $10B. We have never measured what that discard costs. Do
the discarded `too_big` proposals produce worse trade outcomes than the
`in_bracket` proposals we keep, when both are run through the same ladder?

This is the premise underneath #1002. If mega-caps do as well, then "the mapper
proposes too few small caps" is not a defect to fix but a filter to reconsider.

## 2. UNIT

One `(asof, ticker)` proposal row from the funnel. Not a ticker (a ticker may be
proposed on several days), not a theme, not a day.

## 3. SAMPLING

Every row in `~/.alphalens/thematic_candidates/proposal_funnel/*.parquet` with
`bracket_verdict` in `{too_big, in_bracket}` and a non-null ticker. All 16 days.
No day, theme, or ticker is dropped for any reason discovered later.

`too_small` and `no_mcap` rows are OUT of both arms. `too_small` is a different
question (the floor, not the ceiling) and `no_mcap` has no cap to classify on.
Both are counted and reported, never silently omitted.

## 4. ARMS

* **A — discarded**: `bracket_verdict == "too_big"`.
* **B — kept**: `bracket_verdict == "in_bracket"`.

Both arms are taken at the FUNNEL level, before the gates, verification, and
brief selection that only arm B's survivors passed in production. This is
deliberate: comparing all of arm A against SHIPPED cards would confound the
bracket with every downstream filter. At the funnel level the bracket is the only
difference between the arms.

The 901-row production store is NOT the control arm. It may be quoted for context
and must be labelled as differently filtered wherever it appears.

## 5. TREATMENT OF THE LADDER

Both arms are replayed by `replay_population_ladders` against a synthetic briefs
directory and a store directory OUTSIDE `~/.alphalens/population_ladders/`.
The production store is never written to.

Rows are marked `verified = True` in the synthetic brief for BOTH arms. This
bypasses the downstream gates by construction and equally for both arms. It is
not a claim that the rows would have passed.

Trade setups are built by `build_trade_setup` from OHLCV as of the proposal date.
A row whose setup comes back `NO_STRUCTURE` is not plannable, is reported in the
attrition table, and is excluded from the outcome arms — for both arms by the
same rule.

## 6. PRIMARY

**Median `realized_r`, arm A minus arm B, over TERMINAL rows only.**

`NO_FILL` rows are INCLUDED, carrying the value the engine assigns them. This
matches how the shipped population is measured; excluding them would score a
different quantity than the one the project already tracks. The version excluding
`NO_FILL` is a secondary, reported beside it, never substituted for it.

Interval: a **cluster bootstrap resampling DAYS** (not rows), 10 000 draws,
percentile 95% interval on the median difference. Days are the cluster because
proposals within a day share a slate, a market session, and a prompt version.

## 7. SECONDARY

Each reported with its own N, none of them able to overturn the primary:

* median `market_excess_return` by arm;
* `NO_FILL` rate by arm;
* terminal classification mix by arm (`TP_FULL` / `SL_HIT` / `PARTIAL_TP_THEN_SL`
  / `TIME_STOP` / `NO_FILL`);
* median `realized_r` excluding `NO_FILL`;
* the same primary split at $50B, to see whether any effect is "mega" or merely
  "above ten billion".

## 8. POWER, AND THE HONEST STATE OF IT

The funnel begins 2026-08-06. `TIME_STOP_DAYS = 42` trading days, so the OLDEST
proposal cannot time-stop before roughly 2026-10-06. Early terminality can only
come from `SL_HIT`, `TP_FULL`, or `NO_FILL` after the 7-session entry TTL.

**Therefore the primary is expected to be under-powered at first read, and this
contract pre-commits to saying so rather than reporting a thin number as an
answer.**

Floor: the primary is reported as a VERDICT only when **both arms have at least
30 terminal rows**. Below that floor the verdict is INCONCLUSIVE — the numbers
are still published, with N, labelled as an interim read that cannot decide
anything.

Arm B is the binding constraint: 97 rows total before attrition. If arm B never
reaches 30 terminal rows from the funnel alone, the answer waits for the funnel
to accumulate. It accumulates daily at no cost.

## 9. FAILURE MODES ADMITTED IN ADVANCE

* **Observational, not randomised.** Nothing assigned companies to arms. Mega-caps
  differ from small caps in volatility, liquidity, and spread. `realized_r` is
  ATR-scaled, which normalises some of this, and none of it is a randomisation.
  No causal language is permitted in the report.
* **Arm sizes differ by more than 4x** (418 vs 97). The bootstrap handles the
  interval; it does not make the arms comparable in composition.
* **The 08-18 prompt change sits inside the window.** Day sizes before and after
  differ several-fold. Reported as a stratum, not adjusted away.
* **Theme is confounded with cap.** Three themes are structurally outside the
  bracket (`pentagon`, `consumer_sentiment`, `mrna_technology`, per the #1002
  read). A theme-stratified version of the primary is a secondary; it is not the
  primary, because the number of themes is small and the strata would be thin.
* **Survivorship in the price join.** A ticker delisted mid-window has no forward
  bars. Counted in attrition, never dropped silently.

## 10. CONTROLS

* **Positive control**: arm B replayed here must broadly agree with the same rows
  in the production store where they overlap. A large disagreement means the
  synthetic-brief path differs from production and invalidates the run.
* **Attrition table**: every row from the 515 in scope is accounted for in exactly
  one of: replayed-terminal, replayed-ongoing, no-structure, no-bars, excluded.
  The five counts must sum to 515.

## 11. STOPPING

One read now, then re-read no more than weekly. The rule for stopping is the
maturity floor in §8, not the appearance of a satisfying number. Re-reads do not
consume a new hypothesis slot; they are the SAME pre-registered test on a growing
sample, and each re-read reports the N it ran on.

## 12. VERDICTS, FIXED NOW

* **BRACKET EARNS ITS KEEP** — arm B median exceeds arm A median and the 95%
  interval on the difference excludes zero.
* **BRACKET NOT JUSTIFIED BY THIS DATA** — the interval includes zero, or arm A
  is higher. This does not mean the bracket is wrong; it means we have no
  evidence for it, which is a different and weaker statement that the report must
  make in those words.
* **INCONCLUSIVE** — either arm below 30 terminal rows.

## 13. FORBIDDEN

The report may not use "defect", "bug", or "broken" about the bracket or the
mapper. It may not claim the bracket causes anything. It may not present a
secondary as the answer when the primary is INCONCLUSIVE. It may not quote the
901-row production store as arm B.

## 14. ARCHIVE

Result memo: `docs/research/mcap_bracket_cost_read_<DATE>.md`, one per read,
each naming the N it ran on. Script:
`apps/alphalens-research/scripts/replay_bracket_arms.py`. Store:
`~/.alphalens/bracket_cost_ladders/` — outside the production ladder store.

---

## Amendment 1 — §10's row count contradicts §2's unit

Written 2026-08-22, BEFORE any outcome was computed or read. The replay was
running; no `realized_r` for either arm had been looked at.

**The inconsistency.** §2 fixes the unit as `(asof, ticker)`. §3 admits every
funnel row whose verdict is in the two arms. Those are different counts, because
the same ticker is frequently proposed under several themes on the same day:
515 funnel rows collapse to **413** distinct `(asof, ticker)` units. §10 then
asserts the attrition table "must sum to 515", which is the pre-collapse number.

**Resolution: §2 governs.** The unit clause is the one that defines what is being
counted; §10's 515 was an arithmetic slip made while writing §10, not a second
decision about the unit. The attrition table must sum to **413**.

**Why this is not a convenient reinterpretation.** The change cannot move the
primary in a known direction: collapsing duplicates removes rows from BOTH arms,
and which arm loses more is a property of the data that was not consulted. It
also shrinks the discarded arm — the larger one — so if anything it makes the
maturity floor harder to clear, not easier.

**What would have made this improper.** Noticing it after reading the primary,
or resolving it the other way (keeping 515) so that the floor cleared sooner.
Neither happened; the collapse is what the code did on its first run, and it is
pinned by `test_duplicate_asof_ticker_collapses_to_one_row`.

The 102 collapsed rows are themselves reportable: about a fifth of proposals are
the same company reached through a different theme on the same day.

---

## Amendment 2 — §6 describes a mechanism the engine does not have

Written 2026-08-22 at the first read, after the primary had been computed. The
verdict was already INCONCLUSIVE under §8 and stays INCONCLUSIVE under every
reading below, so nothing here can have been chosen to reach an answer.

**The defect.** §6 says `NO_FILL` rows are "INCLUDED, carrying the value the
engine assigns them". The engine assigns them **NULL**: of 44 terminal rows,
29 are `NO_FILL` and every one has `realized_r = NaN`. A null cannot enter a
median, so §6 is unimplementable as literally written.

**Resolution, decided by §6's own criterion.** §6 gives the tie-breaker in its
next sentence — the primary must match "how the shipped population is measured".
Production does this, in `apps/alphalens-django/edge/api/summary.py`
`_accumulate_terminal`: it counts a `NO_FILL` terminal in `n_terminal` and in the
`NO_FILL` rate, then takes `rv = _finite(row.get("realized_r"))` and appends
only finite values to the R aggregates. Never-filled rows are therefore excluded
from `/edge`'s R statistics and are NOT mapped to zero.

The primary is accordingly **median `realized_r` over terminal rows that opened
a position**, which is what the script already did. The prose in §6 was wrong
about the mechanism; the criterion in §6 was right and decides it.

**Consequence for §7.** The secondary "median `realized_r` excluding `NO_FILL`"
is now degenerate — identical to the primary by construction. It is dropped as a
separate figure. The `NO_FILL` RATE, which §7 lists separately, carries the
information that secondary was meant to carry and is unaffected.

**Consequence for §8.** The floor counts rows that contribute to the primary,
i.e. rows with a finite `realized_r` — not all terminal rows. This is the
STRICTER reading: it puts the discarded arm at 12 rather than 34, so it delays a
verdict rather than enabling one. The script already implemented it this way
before the ambiguity was noticed.

**Why zero was rejected.** Mapping `NO_FILL` to 0.0R is defensible in the
abstract (no position, no P&L) and was the first thing considered. It is rejected
because it would make this analysis measure a different quantity than every
number the project already reports, which is exactly what §6 forbids.

---

## Amendment 3 — the row floor cannot see the cluster count

Written 2026-08-22 after read 1, prompted by an adversarial review (zen,
deepseek-v4-pro) and then measured rather than accepted.

**The gap.** §6 fixes a cluster bootstrap over DAYS and §8 fixes a floor of 30
rows per arm. Those are different quantities. The bootstrap's precision is
bounded by how many days can inform a DIFFERENCE — days carrying a realised R in
BOTH arms — and a row-count floor is blind to it. A percentile cluster bootstrap
under-covers badly below roughly 20 clusters, so a verdict could be issued with
an interval far narrower than the design earns.

**Measured, not assumed.** On the read-1 store, 9 days carried a realised R and
the kept arm appeared on only **3** of them. The effective cluster count for the
difference was 3, not 9.

**Amendment.** A verdict additionally requires **at least 20 paired days** —
days with a realised R in each arm (`MIN_PAIRED_DAYS`). Below that the verdict is
INCONCLUSIVE regardless of row counts.

**Why this cannot favour an answer.** It only ever converts a verdict into
INCONCLUSIVE. It cannot produce EARNS or NOT JUSTIFIED where the original rules
produced INCONCLUSIVE, and read 1 was already INCONCLUSIVE on the row floor
alone, so it changes no published verdict.

**Second item: the skipped draws.** §6 did not say what to do with a resample
that leaves an arm empty. The implementation drops it, which conditions the
bootstrap on both arms being present. The fraction dropped is now REPORTED on
every read (`skipped_draw_fraction`) rather than left silent; it measured 2.5%
on the read-1 store, where the shift is negligible, and a future read where it is
large will say so on its face.

**One reviewer claim was checked and rejected.** The review reported that the
theme column is never loaded, so §9's theme-stratified secondary is always
empty. Run against the store, `theme` is present and non-null on all 413 rows:
it arrives on the ladder rows, which the monitor stamps, not through the brief
columns the review inspected. The secondary reports 10 themes.
