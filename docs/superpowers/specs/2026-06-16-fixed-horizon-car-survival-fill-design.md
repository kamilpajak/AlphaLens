# Fixed-horizon CAR + survival-fill diagnostic — design spec

**Status:** DRAFT (awaiting user review)
**Date:** 2026-06-16
**Owner:** Kamil
**Origin:** Follow-on to the NO_FILL root-cause diagnostic (PR #588) and the denominator fix (#590). A Perplexity Sonar Deep Research pass (2026-06-15) on evaluating event-driven strategies with partially-matured records settled the methodology: (1) aggregating the stored running `market_excess` over open positions is invalid (mark-to-market + horizon-heterogeneity bias) — `terminal` gating is a methodological necessity for *realized* returns; (2) the academically standard way to measure **selection** quality independent of fill/exit timing is a **fixed-horizon event-study abnormal return** over a fixed k-session window, complete-window-only (Brown & Warner; Corrado); (3) `n=30` is folklore — fat-tailed, clustered event returns need bootstrap CIs and often N≫100, not a fixed threshold; (4) entry/fill is a **survival** problem — unfilled orders are right-censored, the metric is abnormal-return-per-order-placed.

This spec adds two read-only, descriptive, telemetry-only diagnostics that implement (2) and (4). It does NOT change the production path, the ladder, or the stored `market_excess`.

---

## 1. Problem

The stored `market_excess_return = (last_close − reference_close)/ref − SPY_same` uses `last_close` that **advances every session** for open positions (`ladder_replay._forward_return`), so it is a running, not-final value over heterogeneous windows. It is therefore unsafe to aggregate across the ~211 non-terminal plannable rows, and gating on `terminal` (21 now) both shrinks the sample and conflates selection quality with fill/exit timing. We need:

- a **selection** metric that is fixed-window, complete-only, and decoupled from fill/exit — to answer "do the surfaced names beat the benchmark?";
- an **entry** metric that treats unfilled orders as censored — to answer "how often / how fast does the dip-buy ladder fill, and does it miss the winners?".

Both must be sound on a small, partially-matured sample (bootstrap CIs, descriptive only).

## 2. Goals / non-goals

**Goals**
- Per-event **fixed-horizon CAR** (market-adjusted BHAR) over a configurable set of k-session windows, complete-window-only, with percentile-bootstrap CIs, on the plannable population — reported overall and split by fill outcome.
- **Kaplan-Meier** time-to-fill survival curve over the entry-TTL window with right-censoring at TTL, plus fill-rate with bootstrap CI.
- Reuse the NO_FILL diagnostic's store readers (DRY) via a small extracted module.

**Non-goals**
- No market-model (α/β) abnormal returns — market-adjusted (β=1) only.
- No production-path / ladder / `market_excess` change.
- No ML, no automated re-weight, no intraday/minute precision (daily closes/lows only).
- No findings memo in this spec — the memo is produced when the diagnostic is run on data (like the NO_FILL memo).

## 3. Data sources (verified; same as NO_FILL diagnostic)

All read-only from the VPS parquet stores (`~/.alphalens/`), research-side (may import `alphalens_pipeline`).

| Source | Provides | Reader |
|---|---|---|
| `population_ladders/<date>.parquet` | `plannable`, `ladder_classification`, `reference_close`, `terminal`, `theme`, `ticker` | `pandas.read_parquet` (cols = `LadderOutcome` field names) |
| `thematic_briefs/<date>.parquet` | `brief_trade_setup` (JSON) → E1 (`entry_tiers[0]["limit"]`) | `alphalens_pipeline.paper.brief_loader._coerce_trade_setup` |
| `grouped_daily_history/<date>.parquet` | per-session daily OHLC for the ticker (close → CAR; low → time-to-touch) AND for the benchmark `SPY` (whole-market US grouped-daily includes ETFs) | `alphalens_pipeline.data.rs_history.read_grouped_day(root, date)` → `{TICKER: {o,h,l,c,v}}` |
| `alphalens_pipeline.paper.calendar` | sessions: `session_on_or_after`, `advance_trading_sessions`, `previous_trading_day` | direct import |

**Assumption to verify at run time:** `SPY` is present in the grouped-daily snapshots. If absent for a session, that event's CAR is dropped for that k (counted as benchmark-missing), not silently zeroed. No network fallback (keeps the diagnostic read-only).

## 4. Fixed-horizon CAR (selection quality)

**Anchor + window (daily, self-consistent):**
- `arrival = session_on_or_after(brief_date)`.
- `anchor_session = previous_trading_day(arrival)` — the pre-event daily close.
- For each k in `K_WINDOWS` (default `(5, 10, 20)`): `horizon_session = advance_trading_sessions(arrival, k - 1)` (k sessions inclusive of arrival).
- **Complete-window gate:** include the event at k only if `horizon_session <= newest_stored_session` (the newest grouped-daily date on disk). Otherwise that (event, k) cell is omitted (window not elapsed).

**Per-event market-adjusted BHAR:**
```
stock_bhar = close_stock[horizon] / close_stock[anchor] − 1
spy_bhar   = close_SPY[horizon]   / close_SPY[anchor]   − 1
car_k      = stock_bhar − spy_bhar
```
`None` when any of the four closes is missing/non-positive (counted as data-incomplete for that cell).

**Aggregation (per k):** mean CAR + percentile-bootstrap 90% CI, computed three ways:
- **all** plannable events with a complete window (= abnormal return per order placed — the headline selection number);
- **filled** — `ladder_classification in {OPEN, PARTIAL_TP_OPEN, TP_FULL, SL_HIT}` (an entry actually executed);
- **unfilled** — `ladder_classification == "NO_FILL"`.
Blank / `nan` classifications (entry state not yet resolved by the monitor) are included in **all** (the order was placed) but excluded from the filled/unfilled split. The filled-vs-unfilled gap quantifies whether the dip-buy ladder systematically misses winners (with CIs).

**Anchor note:** this daily anchor (pre-arrival close) differs from the stored intraday `reference_close` (arrival VWAP) — deliberate, so the metric is a clean daily fixed-window BHAR. It is a *new* selection metric, not a recompute of the stored column.

## 5. Survival-fill (entry quality)

For each plannable event with a parseable E1:
- Walk the entry window `[arrival, advance(arrival, TTL))` (`TTL = DEFAULT_ORDER_TTL_DAYS = 7`).
- `duration` = 1-based index of the first session whose daily `low <= E1 * (1 + TOUCH_EPS)` (`TOUCH_EPS = 0.0025`, mirrors the monitor / NO_FILL diagnostic); `event = 1` (touched/filled).
- If no session touches within the window: `duration = TTL`, `event = 0` (right-censored).
- Events with a missing session inside the window before the first touch are dropped (data-incomplete), not censored.

**Outputs:**
- `kaplan_meier(durations, events) -> [(t, S_t)]` — S(t) = P(not yet filled by session t), product-limit estimator with censoring.
- `fill_rate_ci(n_touched, n_total) -> (lo, rate, hi)` — fraction touched within TTL + percentile-bootstrap CI.

**Caveat (documented in output):** touch is detected on the daily low (not minute bars), so it can disagree with the monitor's minute-level fill (the AMBIGUOUS case). This is a daily proxy; minute escalation is out of scope.

## 6. Inference (small-sample discipline)

- **Percentile bootstrap** (`n_resamples` default 10_000) for every interval; a `seed: int` parameter makes resampling deterministic for tests (use `random.Random(seed)`; no global RNG).
- Report **CIs, never a t-test p-value**. All output is descriptive / telemetry-only (project doctrine: no self-driving re-weight).
- Report **all k** as a curve — never select the "best" k (avoids multiplicity abuse).
- Print a small-N caveat line whenever any reported cell has fewer than `LOW_N_WARN = 30` events — purely a *low-precision warning string*, NOT a gate (the threshold is folklore; it only flags that the CI is wide and the estimate anecdotal).

## 7. Deliverables (research side)

- `apps/alphalens-research/alphalens_research/diagnostics/edge_stores.py` — extracted shared store loaders (currently inline in `scripts/diagnose_nofill.py`): `load_store(dir) -> DataFrame`, `setup_index(dir) -> {(date,ticker): setup}`, `GroupedDailyCache` (memoized `read_grouped_day`), `newest_session(root) -> date | None`. `diagnose_nofill.py` is refactored to import these (no behaviour change).
- `apps/alphalens-research/alphalens_research/diagnostics/fixed_horizon.py` — pure: `car_for_event(...)`, `bootstrap_ci(values, *, n_resamples, ci, seed)`, `summarize_car(...)`.
- `apps/alphalens-research/alphalens_research/diagnostics/fill_survival.py` — pure: `kaplan_meier(durations, events)`, `fill_rate_ci(n_touched, n_total, *, n_resamples, ci, seed)`.
- `apps/alphalens-research/scripts/diagnose_selection.py` — thin driver: load stores → build per-event windows → call pure fns → write a tidy per-event table (`~/.alphalens/diagnostics/selection.parquet`) + print per-k CAR (all/filled/unfilled) with CIs, fill-rate with CI, and the KM curve points.
- Tests: `apps/alphalens-research/tests/test_fixed_horizon.py`, `apps/alphalens-research/tests/test_fill_survival.py`, and a parity test that `diagnose_nofill.py` still works after the loader extraction (run its `--help` / a tiny synthetic smoke if feasible, else keep the existing `test_nofill_diagnostics.py` green).

## 8. Testing (TDD)

- `car_for_event`: hand-computed BHAR (e.g. stock 100→110 = +10%, SPY 100→104 = +4% → CAR +6%); `None` on any missing/zero close.
- `bootstrap_ci`: deterministic with a fixed seed on a known small array (assert reproducible lo/mean/hi, and mean within the array's range).
- `kaplan_meier`: hand-worked example with censoring, e.g. durations `[2, 3, 3, 7, 7]`, events `[1, 1, 1, 0, 0]` → assert the product-limit steps (S drops at t=2 and t=3, flat through the censored 7s) match the hand calculation.
- `fill_rate_ci`: `3/5` → rate 0.6, CI brackets it, deterministic with seed.
- complete-window gate: an event whose `horizon_session` exceeds the newest stored session is omitted at that k but present at smaller k.

## 9. Risks / open points

- **SPY in grouped-daily** — assumed present; per-event drop (not zero) if missing for a session (§3).
- **Daily-touch proxy** for fill (§5 caveat) — diverges from minute-level fills (AMBIGUOUS); acceptable for a descriptive entry diagnostic.
- **β=1 market-adjusted** — no market-model; fine at this N, noted.
- **Small N + event clustering** — effective N below nominal (serial correlation from clustered catalysts); bootstrap CIs partly mitigate but report counts and the caveat honestly; do not claim significance.
- **Loader extraction touches the merged `diagnose_nofill.py`** — keep it a pure move (same functions), guarded by the existing NO_FILL tests staying green.
- **Store freshness** — complete-window gate keys off the newest stored grouped-daily session; an un-topped-up store shrinks the eligible set (same hygiene as the NO_FILL re-run: rsync with `--delete` into a clean dir).

## 10. Out of scope (explicit)

Market-model abnormal returns, any production/ladder/`market_excess` change, minute precision, ML, automated re-weight, a findings memo (produced at run time), and intraday CAR. Each is its own future work if ever needed.
