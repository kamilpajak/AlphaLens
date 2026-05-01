# PIT Leakage Audit — Findings (2026-04-30)

**Pre-audit plan (locked scope):** [`pit_audit_2026_04_30_plan.md`](pit_audit_2026_04_30_plan.md). Scope unchanged from plan.

**Methodology:** read-only audit per source. Five parallel investigations covering Form 4 cluster parquet, OHLCV + survivorship, FRED macro, EDGAR candidates queue, FF/Carhart factors. Each question from the plan answered with PASS / WARN / FAIL + code citation. One agent finding (FF C4) was overruled after manual verification — flagged below.

## Per-source verdict table

| # | Source | Question | Verdict | Severity / Notes |
|---|--------|----------|---------|------------------|
| 1 | Form 4 | F1 filing vs transaction date | **PASS** | `_iter_form4_filings` filters `fd ≤ asof` (`scorer.py:204`); cluster window check uses `transaction_date` against window after gating |
| 1 | Form 4 | F2 aggregation window direction | **PASS** | `cluster.py:43` strict lookback `asof - window_days` |
| 1 | Form 4 | F3 `features_as_of` semantics | **PASS** | filing-date gate at `scorer.py:204` prevents post-asof leakage |
| 1 | Form 4 | F4 delisting fire-sale exclusion | **FAIL** | No survivorship filter anywhere in pipeline. Pre-bankruptcy insider sales (panic unloading) treated as informed cluster activity. **Estimated 100-300 bps inflation** for any insider-cluster strategy. |
| 1 | Form 4 | F5 cache miss vs `has_features=False` | **WARN** | API returns `None` for both; downstream consumers can't distinguish "ticker didn't exist" from "no cluster". Latent survivorship risk if a future scorer uses None to exclude tickers. |
| 2 | OHLCV | O1 parquet truncation at delisting | **PASS** | yfinance stops at delisting; cache static |
| 2 | OHLCV | O2 universe includes delisted tickers | **FAIL (process)** | Engine builds static ticker list at backtest start (`engine.py:185-206`). Survivorship maintained by accident, not enforcement. **No CI guard** — silent regression risk on any future filter change. |
| 2 | OHLCV | O3 adjusted close PIT | **WARN** | Raw prices cached, adjustments external; if external split/div factors are fetched as-of "today", forward-leak ~5-20 bps Sharpe. Low magnitude. |
| 2 | OHLCV | O4 timezone alignment | **PASS** | `tz_localize(None)` on cache; all comparisons naive — no ±1d shift possible |
| 3 | FRED | M1 ALFRED vintage | **FAIL (latent)** | Plain FRED endpoint (`fred_client.py:22`); monthly/quarterly series get post-revision values. **NOT load-bearing today** — only daily series (DGS10, DGS2, VIXCLS) used in current code, and they're real-time/unrevised. **Becomes load-bearing the moment any monthly series is added.** |
| 3 | FRED | M2 publication-lag handling | **FAIL (latent)** | No `publication_date` / `release_date` metadata. Same load-bearing caveat as M1. |
| 3 | FRED | M3 frequency mismatch | **PASS** | Daily-only saves us currently; fragile if monthly series added with naive `ffill()`. |
| 3 | FRED | M4 daily off-by-one | **PASS** | Signals snapped at close, applied to next-day execution |
| 4 | EDGAR | E1 submission timestamp granularity | **FAIL (latent)** | Queue schema stores `enqueued_at` (insertion wall-clock), not minute-precision `accepted_at`. Atom feed has minute precision but it's lost at queue insertion. Latent: not load-bearing today (no consumer drains queue per ADR 0008). |
| 4 | EDGAR | E2 trading-time bucketing | **WARN** | No consumer exists; if revived, must split filings into pre-market / market-hours / after-hours buckets. |
| 4 | EDGAR | E3 Item-code encoding | **PASS** | `extract_8k_items` returns deduplicated set, not count |
| 4 | EDGAR | E4 ticker as-of filing date | **FAIL (latent)** | Ticker resolved from `company_tickers.json` (today's snapshot), not as-of filing date. Reverse mergers / ticker changes misattribute pre-change filings to post-change tickers. Latent: not load-bearing today. |
| 4 | EDGAR | E5 historical coverage | **N/A (data gap)** | candidates.db is forward-only from watchdog start (~2026); no 2011-2025 backfill. Any future EDGAR-feature backtest must rely on offline SEC scrape, not the live queue. |
| 5 | FF/Carhart | C1 CSV freshness + future rows | **PASS** | Manual refresh, parser solid |
| 5 | FF/Carhart | C2 definition match | **PASS** | Names, signs, percent→decimal all correct (`tests/test_factors.py:83-91` locks the conversion) |
| 5 | FF/Carhart | C3 period coverage gaps | **PASS** | No interpolation; sparse indices safely surface as NaN downstream |
| 5 | FF/Carhart | C4 ex-ante vs ex-post β estimation | **PASS** *(overruled agent's FAIL)* | See "Overruled finding" below |

## Overruled finding: FF C4

The Explore agent flagged `walk_forward.py:287` as a 20-50% α t-stat inflation due to "in-sample regression". Manual verification: the call site is `run_carhart_attribution(window_returns, window_factors)`, which performs **contemporaneous Carhart regression** of portfolio returns on factor returns over the same window. This is the **standard academic Carhart attribution** (Fama-French 1993, Carhart 1997, every published replication study) — it decomposes realized returns into systematic + idiosyncratic components and reports α as the unexplained intercept. **It is not a forecasting regression; there is no look-ahead.** The agent confused two distinct scenarios:

1. **Attribution** (post-hoc decomposition of realized returns): contemporaneous regression is correct, standard, no leakage.
2. **Prediction** (using β to forecast forward returns): would require ex-ante β from a strictly-prior window.

The codebase uses (1). C4 verdict reverses to **PASS**.

This is a load-bearing correction — if accepted naively, all 10 prior failure verdicts would have been suspect. They are not.

## Implications for prior 10 failures

For each FAIL/WARN above, did it confound any of the 10 prior verdicts?

| Finding | Affects prior verdicts? | Direction | Conclusion |
|---------|-------------------------|-----------|------------|
| **F4** delisting fire-sale | Layer 2d only (insider-cluster strategy) | Inflation works mostly on IS (where forward returns from delisted names are present); OOS less affected | Layer 2d FAIL'd anyway (IS t=2.14 → OOS t=0.68). True alpha is even weaker than reported. **Verdict stands, possibly stronger.** |
| **O2** survivorship by accident | All 10 backtests | Static ticker list happens to include delisted tickers, so universe is correct in practice — but maintained "by convention". | No prior verdict invalidated. **Process risk for future tests.** |
| **O3** adjustment factor | All backtests, low magnitude | Marginal | No material effect on prior verdicts |
| **M1/M2** FRED revisions/lag | Regime-gate diagnostic only (failure 8) | NFCI / BAA10Y could have inflated regime-gate signal coverage | Regime-gate Phase 1 was killed on different grounds (0% NFCI coverage in failure window — **even worse than the audit finding suggests**). Verdict stands. |
| **E1/E2/E4** EDGAR latent | None — no consumer drains queue today | N/A | Not load-bearing for any prior failure |

**Bottom line: NO prior verdict is invalidated.** The 10 failures are real, and several biases may have been working in the strategies' favor (would have made them look better than they were). This is reassuring about the methodology bundle's robustness, and unforgiving about the underlying alpha.

## Required fixes before next experiment (Exp 1+)

The Exp 1 design (multi-source two-stage CPU diagnostic per `feedback_literature_not_oracle.md`) needs these blockers cleared **first** depending on which features enter the whitelist:

### Tier 1 — must fix if feature in whitelist

1. **F4 — Form 4 delisting fire-sale exclusion** *(if insider features in whitelist)*
   - Fix in `ParquetInsiderScorer.__init__()`: load delisting events, return `None` for `(ticker, asof)` where `delisting_date - asof < 180 days`. Alternatively pre-filter at parquet migration step.
   - Add test: `tests/test_pit_form4_delisting_exclusion.py`.

2. **M1 + M2 — ALFRED + release-date masking** *(if any monthly FRED series in whitelist)*
   - Fix in `fred_client.py`: switch to ALFRED endpoints with `vintage_date` parameter; add `release_date` column; mask/forward-fill only on/after release.
   - Add test: `tests/test_pit_fred_release_date.py`.
   - **Daily series (VIX, term spread, credit spread daily) are exempt** — already PIT-clean.

3. **E1 + E4 — EDGAR submission timestamp + as-of-filing-date ticker mapping** *(if any EDGAR filing-type feature in whitelist)*
   - Fix in `alphalens/core/queue.py`: add `accepted_at TEXT` column at minute precision.
   - Fix in `alphalens/watchdog/sources/edgar.py`: resolve CIK → ticker using historical SEC snapshot at `period_of_report` date, not current `company_tickers.json`.
   - Acknowledge E5 data gap: candidates.db has no 2011-2025 history; backfill from offline EDGAR scrape required for any historical backtest with EDGAR features.

### Tier 2 — process invariants worth locking regardless

1. **O2 — Survivorship CI test**
   - Add `tests/test_pit_universe_delisted_inclusion.py`: assert that for a synthetic backtest window with delisting events, every delisted ticker appears in `BacktestEngine.run()`'s ticker list at every rebalance ≤ its delisting date.
   - This is **standalone hygiene**; lock it independently of Exp 1 feature whitelist.

2. **F5 — cluster scorer None ambiguity**
   - Add `has_cluster: bool` to parquet schema (or expose via API); document downstream contract.
   - Lower priority; not load-bearing for Exp 1.

### Tier 3 — accept and document

- **O3 adjustment factor leak** — magnitude too small to fix proactively; document in any insider-feature scorer that adjusted-close consistency is the consumer's responsibility.
- **E2 trading-time bucketing** — only relevant when EDGAR consumer is built; defer.
- **E5 candidates.db forward-only coverage** — accept as data gap; any historical backtest needs offline SEC scrape.

## Summary

**Real PIT bugs (active leakage in current code):** none affecting any source currently in active use for backtests.

**Latent PIT bugs (will activate the moment a new feature class is added):** F4 (insider survivorship), M1+M2 (FRED revisions/lag), E1+E4 (EDGAR ticker as-of-filing-date).

**Process risks:** O2 (survivorship maintained by convention, no CI guard).

**No prior verdict invalidated.** The 10 failures stand. Methodology bundle survives this audit.

**Action ladder for Exp 1:**

1. Decide feature whitelist for Exp 1 multi-source two-stage diagnostic (≤32-40 features per `feedback_literature_not_oracle.md`).
2. For each feature class in whitelist, clear corresponding Tier 1 fix.
3. Add Tier 2 O2 survivorship CI test regardless (cheap, locks past-and-future correctness).
4. Then proceed to pre-registration of `multi_source_two_stage_search` class in ledger and Exp 1 implementation.
