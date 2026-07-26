# /edge ladder-chart staleness — root-cause fix + capacity root

**Status:** DRAFT
**Date:** 2026-07-25
**Author:** Kamil Pająk
**Scope:** `apps/alphalens-pipeline/alphalens_pipeline/feedback/` (chart enrich pass), pipeline-only
**Review:** zen `deepseek/deepseek-v4-pro` thinking=high mandatory (shared feedback surface)

---

## 1. Problem

On `/edge`, ladder-replay charts for some CLOSED positions are stale: the stored `chart_payload_json` freezes days before the position matured, so the terminal exit marker (the TP1 green sell arrow) is missing. Concrete case: **FTRE brief 2026-06-12** — `TP_FULL`, `captured_tp_count=1`, `realized_r=0.52`, sequence `E1->TP1`, `matured_at 2026-07-23` — has a stored payload ending 2026-07-14 with `markers=[E1]` only; a fresh rebuild against the current minute cache correctly yields bars 06-12→07-23 with `markers=[E1, TP1@2026-07-23 price 18.67]`. A store scan finds **79 of 214 terminal charts stale, every one with payload last bar = 2026-07-14** — a systematic-freeze fingerprint since ~07-14 (RUN 05-27, SEDG 05-28, DFIN, KFY, SPIR, CPRI, CRAI…).

## 2. Root cause

The exit marker is folded for free from the cached minute bars, but the code couples it to a Polygon-budget-starved pass that never reaches long-held positions:

1. The nightly monitor re-prices but **carries the chart forward verbatim** — `population_ladder_monitor.py:903` (`row[_CHART_PAYLOAD_COLUMN] = prior_chart_payload`). Chart rebuild is a separate enrich pass.
2. `enrich_store_with_chart_payloads` walks parquets **newest-brief-date first** (`ladder_chart.py:760`, `sorted(..., reverse=True)`) and on the deadline **hard-breaks the whole file walk** (`ladder_chart.py:780-785`), on the assumption "old parquet ⇒ frozen rows" (docstring :741-748).
3. Inside a frame, the per-row loop treats the whole payload as one budgeted unit and **breaks on `deadline.should_stop()`** (`ladder_chart.py:693-695`) — skipping the row entirely, marker included.
4. The budget is effectively the ~15 min reserve floor: `chart_deadline = _RunDeadline(total_s)` is constructed at job start (`feedback.py:110`) but `_RunDeadline` latches `deadline = monotonic()+budget` at construction (`population_ladder_monitor.py:142`), and it is consumed only after the long monitor replay + three other enrich passes. At Polygon free tier (5 req/min) that caps the pass at ~75-87 rows — the logged "enriched 87 rows" ceiling on both 07-23 and 07-24.
5. `_is_frozen_terminal_ok` (`ladder_chart.py:621-661`) already makes a stale terminal *eligible* (last bar < `matured_at` ⇒ not frozen, :654-661) — but eligibility ≠ reachability. A **long-held position from an OLD brief that matures WEEKS later** sits at the back of the newest-first queue; the break trips before the walk reaches it, so it is deferred every night. FTRE (brief 06-12, matured 07-23) is exactly this class.

**Load-bearing fact (proven by repro):** markers + in-trade candles come from the disk minute cache folded to daily (`_default_bar_fetch`→`_read_cached_bars`, `ladder_chart.py:570-580`) and need **zero Polygon**. Only the cosmetic lead-in/trailing daily context band uses Polygon (`_context_bars`/`_default_daily_bar_fetch`, :583-596). The expensive gated part is cosmetic; the exit marker the user cares about is free.

## 3. Recommended design

**Winner: Proposal 2 (cheap-markers-split) as the spine** — decouple the Polygon-free marker-core from the budgeted context band and rebuild the marker-core **unconditionally** for every eligible row. This is the only mechanism that *guarantees* the exit marker under any budget, queue position, or maturation timing (correctness + robustness judges both selected it; small blast radius).

Grafted, per the judge panel:
- **Anti-downgrade guard (Proposal 4)** — a fresh non-OK build never overwrites an existing OK payload. Cheap, portable, folded into the shared build path.
- **Oldest-matured-first ordering of the *residual context* budget (Proposal 1)** — so long-held terminals' cosmetic band is not perpetually starved even after markers are guaranteed.
- **Context-completeness freeze gate (Proposal 5 / 2)** — freeze a terminal only when markers reach `matured_at` AND context is complete; a *missing* `context` key defaults to complete so the legacy already-final tail does not trigger a re-fetch storm.
- **Persistent grouped-daily-first context cache (Proposal 5)** — deferred to **PR 2** (see §9). It is the true capacity root (turns the immutable-band re-fetch into an O(1)-per-row-lifetime cost) but carries the most new surface; it is not needed to kill the user-visible defect and must not gate the correctness fix.

### Control-flow changes (PR 1)

**a) `build_chart_payload` (`ladder_chart.py:469-567`)** — add a context-state stamp and an optional reuse path.
- New param `reused_context_bars: Sequence[Mapping[str, Any]] | None = None`.
- Stamp `"context": "OK" | "reused" | "in_trade_only"` on both OK returns (:538-546, :559-567):
  - `daily_bar_fetch is not None` and it did not raise → `"OK"` (even when it returns `[]` — a delisted/empty ticker resolves to OK-context rather than retrying forever).
  - `daily_bar_fetch is None` and `reused_context_bars` given → split them by `time < arrival_iso` (lead-in) / `time > horizon_iso` (trailing) and feed into the existing `_merge_bars` calls (:535, :557). The in-trade minute-fold still wins on overlap (`_merge_bars`, :464 — verified), so any reused bar now inside an extended hold is correctly overwritten → `"reused"`.
  - neither → `"in_trade_only"`.
- The free tier (markers + in-trade candles from `fetch`) is built in every branch.

**b) `_payload_for_row` (`ladder_chart.py:797-864`)** — add `context_allowed: bool` and `prior_payload_json: str | None`.
- `context_allowed` → call with `daily_bar_fetch=daily_fetch` (today's behavior; `context="OK"`).
- not allowed → `daily_bar_fetch=None` + `reused_context_bars` parsed (guarded, never raises) from `prior_payload_json`. Either way the free tier is built from the disk minute cache.

**c) `_chart_frame_payloads` (`ladder_chart.py:664-708`)** — remove the whole-row break; make the free tier unconditional; add the anti-downgrade guard.
- Delete the `if deadline is not None and deadline.should_stop(): stopped_early=True; break` at :693-695.
- Per row: `context_allowed = deadline is None or not deadline.should_stop()`; pass it + the row's existing `CHART_PAYLOAD_COLUMN` into `_payload_for_row`.
- **Anti-downgrade:** if the freshly built payload `status != "OK"` and the prior stored payload parses to `status == "OK"`, keep the prior string (a transient empty minute cache never blanks a chart that at least shows E1).
- Drop `stopped_early` from the return tuple; keep `recomputed_any`.

**d) `enrich_store_with_chart_payloads` (`ladder_chart.py:711-794`)** — visit every parquet; order the paid context work.
- Delete the `if stopped_early: break` short-circuit at :780-785 — every parquet is visited each run. Frozen-terminal-OK rows are still preserved verbatim (zero fetch, :687-692) and all-frozen files still skip the rewrite via `recomputed_any` (:787-792), so the added cost is bounded to free-tier CPU (ms-scale minute-fold per non-frozen row) + I/O only on files that contain a rebuilt row.
- Replace the newest-first `sorted(..., reverse=True)` (:760) driver so the **residual context budget** is spent oldest-matured-first: do a cheap JSON-only census over all frames, collect non-frozen rows into one flat list, and sort context-eligible terminals by maturation recency (long-held maturers no longer sit behind the newest-first queue for their cosmetic band). The **free tier is already unconditional**, so ordering now only decides who spends the scarce context budget — it can no longer hide an exit marker.

**e) `_is_frozen_terminal_ok` (`ladder_chart.py:621-661`)** — add a context-completeness gate to the final return (:661): freeze only when `last_bar >= matured` **AND** `payload.get("context", "OK") == "OK"`. Defaulting a MISSING key to `"OK"` keeps the legacy already-final tail frozen (no mass unfreeze / 429 storm); any payload this code writes as `"reused"`/`"in_trade_only"` stays eligible so a later budgeted, oldest-matured-first night upgrades its band to `"OK"`, then it freezes.

**No change** to `population_ladder_monitor.py:903` (carry-forward stays — the enrich pass remains the sole upgrader) and **no change** to `feedback.py:100-136` (the reserve split stays; the pass simply uses it for context only). **No Django change** — `edge/api/chart.py` only READS `status`/`bars`/`markers`/`price_lines`; the new `context` field is additive.

## 4. Why not the alternatives

- **Proposal 1 (priority-reorder), standalone** — reorders the contest but leaves markers coupled to the Polygon budget inside `_payload_for_row`; a stale terminal past the ceiling still gets neither marker nor band. Its ordering idea is grafted for the *residual context* budget only.
- **Proposal 3 (monitor-inline-rebuild)** — best marker/classification agreement, but fixes **zero** of the 79 existing rows (the monitor freezes terminals and never re-resolves them), edits the load-bearing nightly monitor, and leaves enrich's starvation for the cosmetic band unaddressed. Its same-outcome idea is a possible later refinement, not the fix.
- **Proposal 4 (decoupled-sweep), standalone** — a second near-duplicate chart writer plus two processes doing read-modify-write on the same parquet, safe only while schedules never overlap (a later monitor-timeout bump silently reintroduces a data-loss race). Only its anti-downgrade guard is grafted.
- **Proposal 5 (capacity-root), as first PR** — it is the real capacity root and is adopted as PR 2, but shipping the persistent cache + 3-state predicate + grouped-cache coupling + reserve retune in one PR is the highest review burden and is not needed to kill the user complaint.

## 5. Implementation plan (TDD, red→green→refactor)

All research tests are `unittest.TestCase` (pytest-style silently skips in CI). File: `apps/alphalens-research/tests/test_ladder_chart_payload.py`. Production files: `apps/alphalens-pipeline/alphalens_pipeline/feedback/ladder_chart.py`.

Ordered steps, each test written red first:

1. **`TestBuildChartContextStamp.test_context_ok_when_daily_fetch_supplied`** — stub `daily_fetch` returns one lead-in bar → payload `context=="OK"` and a pre-arrival bar present.
2. **`TestBuildChartContextStamp.test_context_in_trade_only_when_no_fetch_no_reuse`** — `daily_bar_fetch=None`, no reuse → `context=="in_trade_only"`, markers still present.
3. **`TestBuildChartContextStamp.test_context_reused_when_prior_bars_spliced`** — prior payload lead-in bars passed as `reused_context_bars`, `daily_bar_fetch=None` → `context=="reused"`, lead-in kept, fresh in-trade + exit marker present.
4. **`TestReusedContextMergeInTradeWins.test_reused_trailing_bar_inside_extended_hold_overwritten`** — a reused trailing bar whose date now falls inside the extended hold is overwritten by the minute fold (pins the :464 invariant).
5. **`TestFreeTierAlwaysRebuiltPastDeadline.test_stale_terminal_gets_exit_marker_when_deadline_exhausted`** — `deadline.should_stop()→True`, `daily_bar_fetch` RAISES if called → markers reach `matured_at` incl. TP1, `status=="OK"`, daily fetch never called. **The FTRE regression test.**
6. **`TestFreeTierAlwaysRebuiltPastDeadline.test_context_skipped_reuses_prior_lead_in`** — deadline exhausted, prior payload has lead-in → new payload keeps lead-in + fresh in-trade + exit marker, `context=="reused"`, no fetch.
7. **`TestFreeTierAlwaysRebuiltPastDeadline.test_every_parquet_visited_no_early_break`** — two dated files, deadline exhausted → the OLDER file's non-frozen rows still get free-tier payloads (no file-walk break).
8. **`TestAntiDowngradeGuard.test_empty_minute_cache_keeps_prior_ok_payload`** — `fetch` returns `[]` (NO_DATA) but prior parses OK → prior string preserved byte-identical.
9. **`TestFrozenGateRequiresFullContext.test_in_trade_only_terminal_not_frozen_and_reattempts_context`** — `context=="in_trade_only"`, `last_bar>=matured` → `_is_frozen_terminal_ok` False; with budget this run stamps `context=="OK"` then it freezes.
10. **`TestFrozenGateRequiresFullContext.test_full_context_terminal_freezes`** — `context=="OK"`, `last_bar>=matured` → True.
11. **`TestFrozenGateRequiresFullContext.test_legacy_payload_missing_context_key_stays_frozen`** — OK payload, `last_bar>=matured`, no `context` key → True (no re-fetch storm on pre-fix DONE charts).
12. **`TestContextBudgetOrdering.test_long_held_maturer_gets_context_before_recent_ongoing`** — tight context deadline, an old-arrival newly-matured row + recent rows → the old maturer's band is filled first (oldest-matured-first).
13. **`TestFrozenSkipPreserved.test_frozen_rows_never_fetched_and_preserved_verbatim`** — frozen terminal-OK payload byte-identical after pass; `fetch`/`daily_fetch` never called for it.
14. **REPLACE** existing `TestEnrichSkipsFrozenTerminalRows.test_processes_newest_store_file_first` (`test_ladder_chart_payload.py:894`) — it pins the now-removed newest-first heuristic; rewrite to assert the free tier is unconditional and every parquet is visited. **Must land in the same commit or the suite is red.**

Then green: implement §3(a-e); refactor for clarity.

## 6. Immediate one-shot remediation (79 stale rows)

The 79 rows will **not** self-heal from the nightly job alone before the PR lands (they are deferred every night). Ship PR 1, deploy to the VPS host venv, then run one unbounded pass so every parquet is walked and every eligible stale terminal is rebuilt WITH markers and context in a single shot. On the VPS (`POLYGON_API_KEY` + `SEC_EDGAR_USER_AGENT` in `/etc/alphalens/env`):

```bash
cd /home/jacoren/AlphaLens
source /etc/alphalens/env   # POLYGON_API_KEY, SEC_EDGAR_USER_AGENT
.venv/bin/python -c "from pathlib import Path; \
from alphalens_pipeline.feedback.ladder_chart import enrich_store_with_chart_payloads as e; \
print('enriched', e(Path.home()/'.alphalens'/'population_ladders', \
Path.home()/'.alphalens'/'thematic_briefs', deadline=None))"
```

`deadline=None` means no break — all 79 stale terminals are reached; markers fold from the cached minute bars (correct even if Polygon throttles the cosmetic band — `PolygonClient` self-throttles at 5 req/min, so the run just takes ~15-20 min, not a failure). Idempotent + atomic parquet rewrite. Then refresh the edge Postgres cache so `/edge` serves the new payloads now instead of waiting for the hourly `alphalens-edge-mirror`:

```bash
docker compose -f deploy/docker/django-prod/docker-compose.yaml \
  --profile maintenance run --rm rebuild-ladder-outcomes
```

**Fast Polygon-free path** (markers now, context self-heals) — use an already-expired deadline, NOT an empty fetch:

```python
from alphalens_pipeline.feedback.population_ladder_monitor import _RunDeadline
e(Path.home()/'.alphalens'/'population_ladders', Path.home()/'.alphalens'/'thematic_briefs',
  deadline=_RunDeadline(0.0))
```

An expired deadline makes `context_allowed` False for every row, so `_payload_for_row` rebuilds the marker core from the minute cache and passes `daily_bar_fetch=None` + the prior payload's bars as `reused_context_bars` → `context=="reused"` (or `"in_trade_only"` when there is no prior). Those rows are NOT frozen (context != "OK"), so the next budgeted nightly enrich upgrades their cosmetic band oldest-matured-first, then freezes them. Zero Polygon, seconds, no permanent precision cut.

> ⚠️ Do NOT use `daily_bar_fetch=lambda *a, **k: []` for this. An empty return that does not RAISE is treated as a *successful* fetch, so `_resolve_context_bars` stamps `context=="OK"` on an empty band — the freeze gate then freezes the row **permanently with no context**. The empty-fetch degradation only applies when the real fetch RAISES (`context=="in_trade_only"`); the expired-deadline path above is the correct Polygon-free rebuild.

**As executed (2026-07-25):** the `deadline=None` full run hit the Polygon free-tier 429 wall (ran 1h+, and the pass only writes parquets at the very end, so a mid-run kill loses everything). Switched to the expired-deadline path above: 634 rows rebuilt in ~18 s, zero Polygon; FTRE 2026-06-12 went to bars `2026-04-30 → 2026-07-23` with the `TP1` marker, and the 07-14 stale cohort dropped 79 → 23 (the residual 23 are freshly-matured positions whose minute cache is one session behind `matured_at`, self-healing on the next nightly monitor run — not the starvation bug). Also note `/etc/alphalens/env` cannot be shell-`source`d (the `SEC_EDGAR_USER_AGENT` value has unquoted spaces); parse `KEY=VALUE` in Python instead.

## 7. Verification

**FTRE end-to-end:**
1. Unit: `TestFreeTierAlwaysRebuiltPastDeadline.test_stale_terminal_gets_exit_marker_when_deadline_exhausted` reproduces the FTRE geometry and asserts the TP1 marker at `matured_at` with an already-tripped deadline (red before §3(c), green after).
2. Post-one-shot, read `~/.alphalens/population_ladders/2026-06-12.parquet` for FTRE and assert `json.loads(chart_payload_json)` has `bars[-1].time == "2026-07-23"`, a `TP`/`TP1` marker at 2026-07-23 ≈ 18.67, and `markers` == `{E1, TP1}`.
3. `/edge`: open the FTRE 2026-06-12 card, confirm the chart ends 2026-07-23 with the green sell arrow (after `rebuild-ladder-outcomes`).

**Backlog cannot re-accumulate:**
- The free marker tier is unconditional and Polygon-free, so no maturation timing / queue position can defer an exit marker — proven by tests 5, 7 (deadline exhausted, older file still built).
- Store-scan check: after the nightly job on two consecutive days, assert **zero** terminal rows have `last_bar < matured_at` (the 07-14 fingerprint is gone). Add this as a lightweight operational check.
- The context band still defers under budget, but oldest-matured-first ordering + the "not frozen until `context==OK`" gate guarantee each row is upgraded and then frozen at most once — it cannot silently re-freeze marker-incomplete (tests 9-12). PR 2 removes the residual context deferral entirely.

## 8. Risks / rollback

- **Freeze-gate mis-spec re-freezing a marker-incomplete row** — mitigated by tests 9-11 (missing key defaults to OK → no legacy thrash; `in_trade_only`/`reused` stay eligible).
- **Legacy `context`-missing tail triggering a re-fetch storm** — the `payload.get("context","OK")` default keeps it frozen; pinned by test 11.
- **Reused trailing bar slightly stale beyond the old horizon** — still an honest daily candle, cheap, and overwritten by the minute fold once inside an extended hold (test 4).
- **Every parquet read nightly** (early break removed) — bounded: 66 small frames, ms-scale parquet reads, negligible vs the multi-minute Polygon passes; frozen files still skip the rewrite.
- **Rollback** — revert the single PR; the carry-forward at `:903` and legacy freeze semantics are untouched, so a revert returns exactly to today's behavior. The one-shot rewrites are atomic and idempotent; a stale payload is never worse than what shipped.
- **Zen pre-merge codereview** (deepseek-v4-pro, thinking=high) is mandatory — shared feedback pipeline surface.

## 9. Effort + PR breakdown

- **PR 1 — the fix (M, ~1 day).** §3(a-e) + §5 tests. Files: `apps/alphalens-pipeline/alphalens_pipeline/feedback/ladder_chart.py`, `apps/alphalens-research/tests/test_ladder_chart_payload.py`. This alone kills the user complaint (budget-independent markers) and self-heals the 79 rows via §6. Ship first.
- **PR 2 — capacity root (M, follow-up).** Proposal 5's persistent per-`(ticker,arrival)` context cache sourced grouped-daily-first (`population_ladder_monitor._read_grouped_cache`, adjusted=false, matching the minute candles), falling back to a single per-ticker daily fetch only for deep lead-in beyond the ~50-session grouped window. Adds `context=="UNAVAILABLE"` for provably-empty frozen horizons. This turns the immutable-band re-fetch into an O(1)-per-row-lifetime cost, collapses steady-state Polygon draw to near-zero, and lets `_CHART_RESERVE_S_DEFAULT` shrink (returning budget to the monitor). Staged separately for review-burden and blast-radius reasons; PR 1 does not depend on it.

**Staged, not one PR** — the correctness fix must not wait on the heavier cache subsystem, and the two are independently reviewable.

**Worktree note:** this session is the primary (main checkout); it must NOT create branches/commits here. Author PR 1 in its own worktree off fresh `origin/main`:

```bash
git worktree add -b bugfix/edge-ladder-chart-stale-markers \
  .claude/worktrees/edge-chart-stale origin/main
```

`cd` into that worktree for all edit/test/git work, and run `uv sync` there if the worktree edits pipeline package code (a worktree needs its own local venv, per the recorded gotcha). Keep CLAUDE.md edits out of the feature branch.
