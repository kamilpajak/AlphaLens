# benchmark-excess reuse-first (drain the old-tail starvation) — design

**Status:** LOCKED (approved 2026-07-28)
**Date:** 2026-07-28
**Related:** PR #920 (completion-stamp — this is the deferred §3 old-tail follow-up), PR #904/#912 (the reuse-first pattern this mirrors), PR #847 (pending-vs-na)

---

## 1. Problem

`enrich_store_with_benchmark_excess` (`apps/alphalens-pipeline/alphalens_pipeline/feedback/benchmark_excess.py`) computes, per closed position, the two columns `benchmark_window_return` + `market_excess_return` (how the stock did vs SPY over its arrival→exit window). Each value needs a Polygon SPY fetch, and Polygon is throttled (≈5 req/min), so the nightly pass runs under a wall-clock deadline.

Two defects (the deferred residuals from PR #920 §3/§7):

1. **Old-tail starvation.** The pass sweeps parquets **newest-first** under a shared deadline (`enrich_store_with_benchmark_excess`, the `sorted(..., reverse=True)` loop) AND recomputes **every** row every run — `_enrich_frame_rows` calls `_row_excess_cached` for every row with a `forward_return`, even rows that already carry a good, consistent `(benchmark, excess)` pair. So each night the budget is spent re-fetching windows for already-settled rows; when a backlog exists, the sweep never reaches the deep history. Result: ~110 old closed rows carry a permanent `-` in the excess column (they matured, but their benchmark was never computed and never will be under the current budget).

2. **Cheap NO_FILL maturation can carry a stale excess.** When the monitor matures a `NO_FILL` row cheaply (`population_ladder_monitor._cheap_update_row`, `row = dict(prior)`), it copies the prior `(benchmark, excess)` and recomputes `forward_return`. Normally the changed `forward_return` makes the copied pair inconsistent, so `_carry_forward_prev_pair` forces a recompute on the next benchmark pass — but only if that pass reaches the row (defeated by defect 1), and in the edge case where `forward_return` is unchanged at maturation the pair stays *consistent* and would be trusted even though the benchmark window (now fixed at the terminal exit) changed. So a closed `NO_FILL` can show a stale, wrong excess rather than an honest blank.

### 1.1 Current live state
~110/761 rows un-benchmarked (`market_excess_return IS NULL`, older brief dates); the `/edge` completeness banner (PR #920) reads below 100% because of them. Not an incident — the completion-stamp already made the mirror honest; this drains the residual.

---

## 2. Goal
Drain the old-tail so every matured row gets its benchmark within a night or two and then stays put, and guarantee no closed row ever shows a stale/wrong excess (either the real value or an honest blank → #847 pending). Budget is spent only on rows that actually need it.

---

## 3. Non-goals / out of scope
- **Do NOT flip the file ordering.** Newest-first is a deliberate, test-enforced choice (`test_processes_newest_parquet_first`, `test_newest_first_under_deadline_heals_recent_and_leaves_old`) so that under budget pressure the recent, dashboard-visible dates heal first. Reuse-first makes the whole sweep cheap enough to reach the tail without changing ordering.
- The benchmark math and the completion-stamp are unchanged.
- **`sector_excess.py` is out of scope (deferred).** It has the identical settled-row-refetch waste (and sweeps oldest-first, no carry-forward guard), BUT the `/edge` completeness banner counts MARKET-excess only (`n_matured` = terminal rows with finite `market_excess_return`), not sector-excess. A follow-up applies the identical reuse-first pattern to `sector_excess.py` if the sector-excess column's old-tail `-` needs draining too.
- Raising the Polygon budget / `TimeoutStartSec` — not needed once settled rows stop consuming it.

**Note on `_carry_forward_prev_pair` (revised after adversarial review):** reuse-first `continue`s before the fetch for exactly the rows that guard protected (terminal + consistent), so the guard becomes unreachable. It is **REMOVED**, not shared — keeping it as a two-call-site helper would ship dead code and orphan its test (Sonar diff-coverage). Its predicate lives in one place (`_has_consistent_stored_pair`), used only by reuse-first.

---

## 4. Design — two changes

### Change A — reuse-first skip in `_enrich_frame_rows` (the core fix)
Before fetching for a row, check whether it is **already settled**: it is TERMINAL (`matured_at` set) AND carries a **consistent** stored pair (`benchmark_window_return`, `market_excess_return` both real AND `abs(excess - (forward - benchmark)) < 1e-9`). If so, **reuse** the stored values verbatim — no Polygon fetch. (Refinement M1: a reused row DOES seed the in-run window cache for its `(arrival, exit)` window via the same key derivation `_row_excess_cached` uses, so a GAP sibling sharing that window is served free instead of paying its own fetch; the `if key not in window_cache` guard never clobbers a real fetch, and the benchmark return is a pure function of the window so serving it to a same-window ticker is exact.) Only rows that are (a) a gap (null/NaN benchmark), (b) inconsistent (freshly matured), or (c) ONGOING (`matured_at` is None — window still grows, must recompute every run) actually fetch.

- This is the SAME consistency predicate `_carry_forward_prev_pair` already uses — factor it into a shared helper (`_has_consistent_stored_pair(row)`) so the two sites cannot drift.
- Ordering stays newest-first. The deadline check stays at the TOP of the per-row loop (unchanged), so a tripped deadline still breaks exactly as the existing tests assert; a reused row simply costs no fetch.
- ONGOING rows are never reused (mirrors the `_carry_forward_prev_pair` `matured_at` guard + `test_transient_none_nulls_an_ongoing_rows_stale_benchmark`).
- Counting: keep `n_enriched` = rows with a non-null excess after the pass (so reused rows still count as covered — the log/return stays a coverage number, not a fetch count). Optionally log `reused=<n>` for observability.

**Why this drains the tail without an ordering change:** a settled row now costs ~microseconds (a consistency check, no throttled fetch), so a full newest-first sweep of the whole store fits far inside the 90-min budget. The one-time catch-up of ~110 gap rows ≈ 22 min at 5 req/min; steady-state is near-zero benchmark fetches (only newly-matured rows + ongoing rows).

### Change B — null benchmark/excess on cheap TERMINAL maturation (`_cheap_update_row`)
When `_cheap_update_row` matures a row to terminal (it sets `terminal=True` + `matured_at`), explicitly set `benchmark_window_return = None` and `market_excess_return = None` on the carried row. Rationale: the benchmark window is only fixed once the position is terminal; a value carried from the ongoing (growing-window) state is stale. Nulling makes it an honest gap that Change A will recompute with the final terminal window — and it closes the edge case where an unchanged `forward_return` would leave a *consistent-but-stale* pair that reuse-first would wrongly skip. The monitor already carries these columns via `dict(prior)`, so this is a two-line honesty fix, not new coupling.

---

## 5. Test strategy (`apps/alphalens-research/tests/test_benchmark_excess.py`, unittest.TestCase)
- **A1 reuse-first skips a settled row:** a TERMINAL row with a consistent stored pair → enrich does NOT call the bar-fetch for it and leaves its values unchanged. (Assert via a fetch spy that records calls.)
- **A2 gap row still fetches:** a TERMINAL row with a NULL benchmark → enrich fetches and fills it.
- **A3 ongoing row still recomputes:** a row with `matured_at=None` and a stored pair → still fetched/recomputed (not reused).
- **A4 inconsistent terminal pair recomputes:** a TERMINAL row whose stored excess ≠ forward − benchmark → recomputed, not reused.
- **A5 budget goes to gaps:** with a deadline that allows only 1 fetch and a store of [settled, settled, gap], the gap gets filled (settled rows didn't consume the budget). Pins the starvation fix.
- **B1 cheap terminal maturation nulls the pair:** `_cheap_update_row` maturing a NO_FILL to terminal returns a row with `benchmark_window_return`/`market_excess_return` = None (test in the monitor's test module).
- **Regression:** the existing newest-first tests, the transient-none guards, and the property tests all stay green (they operate on gap/ongoing rows, which are unaffected).

---

## 6. Behaviour notes
- **One-time catch-up:** the first post-deploy nightly benchmark pass fills the ~110-row tail (~22 min); the `/edge` banner rises toward 100% and stays. Confirm via the new `reused=M fetched=F` log.
- **Shared latched budget:** the benchmark pass shares ONE latched `_RunDeadline` with `replay` (runs first), `sector_excess`, and `size`. A replay-heavy night can latch the budget before benchmark runs, so reuse-first REDUCES but does not fully eliminate multi-night drain — the catch-up may span a couple of nights under heavy replay.
- **Reduced mtime churn:** the pass now skips the parquet rewrite for a file where every row was reused (nothing changed), so it stops bumping mtime needlessly — which also lets the Django edge-mirror mtime gate skip unchanged dates, complementing PR #920's settled-watermark.
- **Deploy:** writer is host-venv (`feedback backfill-shadow-returns` at 06:30 UTC) → `git pull` on the VPS host checkout. No Django/image change (Django only reads the columns). No SPA change.
- Zen pre-merge (deepseek-v4-pro, high) mandatory (pipeline change).

## 7. File-touch map
| File | Change |
|---|---|
| `apps/alphalens-pipeline/alphalens_pipeline/feedback/benchmark_excess.py` | reuse-first skip in `_enrich_frame_rows`; shared `_has_consistent_stored_pair` helper; optional `reused` log |
| `apps/alphalens-pipeline/alphalens_pipeline/feedback/population_ladder_monitor.py` | null benchmark/excess cols on cheap terminal maturation in `_cheap_update_row` |
| `apps/alphalens-research/tests/test_benchmark_excess.py` | A1-A5 |
| `apps/alphalens-research/tests/test_population_ladder_monitor.py` (or the monitor's test module) | B1 |
