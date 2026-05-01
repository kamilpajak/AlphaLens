# PIT Leakage Audit — Pre-Audit Plan (2026-04-30)

**Status:** Pre-registration of audit scope BEFORE findings. Locks the question set so the audit cannot be silently widened/narrowed post-hoc.

**Type:** Research observation (NOT strategy hypothesis test). Bonferroni-immune — does not enter the price-factor or any other strategy class denominator.

**Motivation.** 10 phase-robust strategy failures have been logged in the pre-registration ledger across linear factor screeners, gates, and overlays. Before designing the next experiment (multi-source ML / two-stage regime classifier per `feedback_literature_not_oracle.md`), audit each PIT-correct data source independently for look-ahead and survivorship leakage. Discovering that any source has hidden bias would itself be a load-bearing observation — it would mean the historical ledger entries are partially confounded by data error, not just by strategy weakness, and would change the prior on the next experiment.

## Sources in scope

| # | Source | Path | Used by |
|---|--------|------|---------|
| 1 | Form 4 cluster parquet | `~/.alphalens/insider_form4.parquet/` | Layer 2d (CLOSED) — would be re-used by any future insider ML experiment |
| 2 | OHLCV per-ticker parquet | `~/.alphalens/prices/` + `alphalens/data/store/history.py` | Every backtest |
| 3 | Survivorship events | `alphalens/data/store/survivorship_pit.py` (YAML + parquet) | Every backtest universe construction |
| 4 | FRED macro | `alphalens/data/macro/fred_client.py` (per-series parquet) | Regime gates, macro features |
| 5 | EDGAR candidates queue | `~/.alphalens/candidates.db` | Layer 1 watchdog (live), historical viewer |
| 6 | Fama-French / Carhart factors | `alphalens/data/factors.py` (CSV loader) | Carhart-4F attribution in every audit |

## Per-source check matrix

For each source, audit answers these questions and produces a verdict + invariant statement.

### Form 4 cluster parquet
- [ ] **F1.** Is `date` column the **filing date** (publicly observable at t) or the **transaction date** (private until filing)?
- [ ] **F2.** Cluster aggregation window — does `cluster_window_days` reflect a window ending at `asof - 1` (no peeking forward), or is it centered/forward-looking?
- [ ] **F3.** `asof` semantics — is the `features_as_of(ticker, asof)` API actually filtering filings with `filing_date ≤ asof`, or only `transaction_date ≤ asof`?
- [ ] **F4.** Delisting fire-sale bias — are insider trades within ~6 months before delisting included? If yes, naive backtests overestimate alpha (insider sales pre-bankruptcy are not "informed", they're unloading).
- [ ] **F5.** Cache miss vs. has_features=False — does the API distinguish "no row" from "row with has_features=False"? Both return `None` per the contract; verify this is symmetric for backtests.

### OHLCV parquets + survivorship
- [ ] **O1.** Per-ticker parquet truncation — for delisted tickers, do bars stop exactly at the CRSP/Polygon delisting date, or are there stale bars after?
- [ ] **O2.** Universe construction — does the backtest universe at time `t` include tickers that delist after `t`? (Must include them — otherwise survivorship bias.)
- [ ] **O3.** Adjusted close — splits/dividends backward-applied, no forward-leak (today's adjustment factor must not retroactively change historical prices in a way that uses post-`t` info).
- [ ] **O4.** Timezone — bars timestamped in market timezone (ET) or UTC? Joins with EDGAR/Form-4 dates must agree on convention.

### FRED macro
- [ ] **M1.** Vintage data — do we use ALFRED snapshots (publication-date-aware) or current FRED values (post-revision)?
- [ ] **M2.** Publication lag — for monthly/quarterly series (UNRATE, CPI, IP), does the loader respect "available no earlier than release date"? E.g., Jan 2024 unemployment is not knowable on Feb 1 2024 — released first Friday of February.
- [ ] **M3.** Frequency mismatch handling — daily backtest joining monthly series: is the series forward-filled **only on release dates**, or stale-broadcast (incorrectly) every day?
- [ ] **M4.** Daily series (VIX, term spread) — these are real-time; verify no off-by-one (today's close knowable at end-of-day, not at open).

### EDGAR candidates queue
- [ ] **E1.** Submission timestamp — is `accepted_at` (or equivalent) stored? At what granularity (minute, day)?
- [ ] **E2.** Trading-time bucket — for backtest replay, does the consumer split filings into pre-market / market-hours / after-hours? Or is everything attributed to the same trading day regardless of submission hour?
- [ ] **E3.** Item-code aggregation — if a single 8-K has multiple Items (e.g., 2.01 + 5.02), are they binary-encoded per Item or count-encoded? (Per Perplexity research: count-encoded introduces spurious correlation.)
- [ ] **E4.** Ticker mapping — when `accession_no → ticker` was resolved, was the resolution as-of the filing date (not as-of today)? Reverse mergers and ticker changes break this if not handled.

### Fama-French / Carhart factors
- [ ] **C1.** CSV freshness — does the loader use end-of-day published values from Kenneth French website, with no future-dated rows?
- [ ] **C2.** Definition match — Mkt-RF, SMB, HML, RMW, CMA, UMD/MOM aligned to upstream definitions (no sign-flipped or scale-mismatched columns).
- [ ] **C3.** Period coverage — does the CSV cover the full backtest window without gaps?
- [ ] **C4.** Ex-ante vs. ex-post estimation — when computing rolling factor exposures (β_i), the regression window must be entirely strictly before the prediction date `t`, not centered on `t`.

## Methodology

1. **Read-only audit** — do not modify any source files. If invariants are violated, file follow-up tasks; do not silently fix.
2. **Per-source verdict:** PASS (PIT clean) / WARN (works but fragile) / FAIL (active look-ahead bias).
3. **Quantify when possible** — for each FAIL, estimate the Sharpe-impact in bps (rough: rerun a representative scorer with and without the fix, compare).
4. **Output:** `docs/research/pit_audit_2026_04_30_findings.md` with per-source verdict table, full check-by-check rationale, and a tests-to-add list for new invariants worth locking in (`tests/test_pit_*.py`).

## Success criteria for the audit itself

- Every check above produces an explicit verdict (no "I forgot to look").
- Every FAIL has either (a) a quantified Sharpe-impact estimate or (b) a written reason why quantification is infeasible.
- Aggregate finding feeds into the next experiment design (Exp 1 multi-source two-stage), with explicit feature whitelist amendments where leakage was found.

## Out of scope

- Polygon Starter live fundamentals (RESEARCH_ONLY, not used in any historical ledger entry).
- QuiverQuant Congress signals (archived, Layer 2d related, audit happens only if revisited).
- Lean Docker pipeline (closed layer; backtest replay only, no new strategy depends on it).
- Literature review corpus (text artifact, not a numeric feature source).

## Pre-registration timestamp

Committed to git at the moment this file is added. The audit findings file (`pit_audit_2026_04_30_findings.md`) is written separately AFTER the per-source audit completes; the diff between this plan and the findings should not show post-hoc rewrites of the question set.
