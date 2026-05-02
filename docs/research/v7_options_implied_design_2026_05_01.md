# v7 design — options-implied features (DRAFT v3 — smd-primary architecture, post-adversarial review)

**Status:** DRAFT — pre-reg NOT LOCKED. PIT integrity gate PASS (Pearson 0.9990, 2026-05-01). Remaining blocks: hypothesis direction commitment, feature-stack consolidation, long-short diagnostic, multiplicity reconciliation. Estimated unlock: 2026-05-02 after edits below land.

**Class:** `options_implied_search_2026_05_xx` (NEW class). Cumulative pre-reg count after this lock = 14. Naive Bonferroni primary |αt|≥2.86; Romano-Wolf m=30 stretch |αt|≥3.27.

**Author:** Kamil. **Date:** 2026-05-01 PM (rev 3).

## Why v3 supersedes v2

v2 was scoped to a 4-endpoint composite (`/ivx` + `/ivs` + `/hv` + `/stock-opts-by-param`) with cascading variant resolution and Q-suffix look-ahead concerns. Probe v5 (n=200, 99.5% T1 retention 2026-05-01 22:47) demonstrated `/equities/stock-market-data` (smd) returns 100+ pre-computed features per (ticker, asof) **with original ticker indexing preserved across delistings (T2=0)**. This single-call architecture moots the Q-suffix look-ahead concern — smd queries SIVB at 2023-03-08 directly without any post-bankruptcy re-keying.

PIT integrity replication probe (`scripts/probe_pit_replication.py`, AAPL, 12 asofs in 2023, 2026-05-01 23:27) confirmed vendor IVP uses strict backward-looking window: Pearson 0.9990 between empirically-recomputed IVP and vendor smd IVP across 8 valid pairs (4 NaN are weekend/holiday calendar artifacts; production code snaps to last trading day).

## Adversarial review (zen + perplexity, 2026-05-01)

Two showstoppers + five high-severity issues identified. PIT showstopper now resolved via the replication probe. Remaining blind spots addressed inline below; complete summary in `docs/research/v7_adversarial_review_2026_05_01.md` (TBD — to be created when v7 memo is locked).

## Hypothesis (DIRECTION-COMMITTED per zen + perplexity)

**Literature prior:** Xing-Zhang-Zhao 2010, Bali-Hovakimian 2009, An-Ang-Bali-Cakici 2014 converge on **NEGATIVE** sign — high implied vol / high put-skew predicts NEGATIVE next-month equity returns (vol risk premium going wrong way for the buyer of insurance). Cremers-Weinbaum 2010 finds positive PCP-deviation predicts positive returns but on a different feature class.

**H₁ (commit):** Cross-sectional ranking of options-implied vol features (IVP, IVX30 level, IVX180−IVX30 term spread, IVX30/HV20 ratio) **predicts next-20d equity returns with NEGATIVE sign on the vol-level features**. Top decile = **lowest** Lasso-fitted return (i.e. highest IVP/IVX → expected to underperform). Long-only strategy = LONG **bottom** decile by Lasso score (low-IV names).

**H₀:** Options-implied features add no predictive power beyond equity factor controls (1m reversal, 6m momentum, 30d HV), OR signal exists but is uneconomic after 30bps RT cost.

**Diagnostic flag (NOT auto-pass):** if Lasso fits POSITIVE coefficients on IVP/IVX in train, this contradicts the literature prior → Phase A diagnostic, document + investigate before Phase B. Do NOT pivot strategy direction post-hoc to chase the data.

## Feature stack (REDUCED to 7 orthogonal features, was 9-13)

zen+perplexity: throwing IVR + IVP + IVX30/60/90/180 + HVP + IVX30HV20 into Lasso = anti-pattern. All measure the same latent vol-level construct → Lasso arbitrarily zeroes most → false orthogonality in holdout.

**4 options features (orthogonal-by-construct):**

1. **IVP30** (1y rolling percentile of IVX30) — rank-based, robust to vol outliers (drop IVR per zen — same construct, less robust).
2. **IVX30** (level) — captures absolute vol regime.
3. **IVX180 − IVX30** (term-structure slope) — captures forward vol expectations.
4. **IVX30 / HV20** (IV-vs-HV ratio) — captures vol risk premium magnitude.

**3 equity controls (must include per Carhart prior):**

5. **1m reversal** — `−1 × r_21d` from Polygon stock aggs.
6. **6m momentum** — `r_[t-126, t-21]` skip-month per Jegadeesh-Titman.
7. **30d realized vol** — annualized stdev log returns from Polygon aggs.

**Dropped from v2 stack:** IVR (redundant with IVP), IVX60/IVX90 (collinear with IVX30/180 via term structure), HVP (redundant with IVX30HV20), 25Δ skew (would require `/ivs` per-strike interpolation × 2000 tickers × 2000 dates — infeasible and breaks single-call smd architecture; defer to v8).

**P/C OI ratio handling (deferred to v7.1):** smd's aggregate `openInterest_call` / `openInterest_put` includes LEAPS + deep OTM and dilutes short-term signal. For v7.0, **omit P/C ratios entirely** to keep stack minimal and orthogonal. v7.1 (post-PASS or post-FAIL diagnostic) can add via `/equities/eod/stock-opts-by-param` constrained to **20-45 DTE × delta [0.25, 0.75]** (probe v5 unblocked at retail).

**Vendor BETA: omitted** (per zen — overengineering; equity controls 5-7 already provide market exposure adjustment via Carhart-style factors).

**ETL anomaly bounds (drop rows):**
- IVX30 > 3.0 (300%) — calibrated against SIVB peak 245% in 2023 distress.
- IVX30 < 0.05 — penny-stock / no-trading artifacts.
- |IVX180 − IVX30| > 1.5 — extreme term-structure inversions (likely data errors).
- IVX30/HV20 > 10 or < 0.1 — provisional, may need recalibration.
- Stock price < $1 — penny territory, drop.

## Universe construction

**Optionable filter (DYNAMIC, per zen — Polygon metadata flag has survivorship risk):**
- For each (ticker, asof_t), check that smd's `optVol` > 0 OR `openInterest_call + openInterest_put` > 0 on `t-1`. If yes, ticker was actively-optionable at asof_t.
- This replaces `Polygon /v3/reference/options/contracts?as_of=` which may have backfilled chain-ref data.
- Already available in smd response; no extra API call.

**Liquidity filter:**
- Min ADV (20-day avg dollar volume) ≥ $2M
- Min underlying price ≥ $1
- Drop OTC pink sheets (smd `exchange` field)
- Source ADV: Polygon stock aggs

**Expected universe:** ~2000-2500 names per asof.

**Delisting handling (locked):**
- Forward-fill last trading price 5 trading days post-delisting (covers settlement / OTC residual).
- After 5d: apply −50% return for "standard" delistings (acquisition, voluntary), −100% for "bankruptcy" (Ch11, FDIC receivership).
- Reason classification: from existing `~/.alphalens/survivorship/delisted_2021_2026.parquet`.
- Inclusion in cross-section is mandatory (excluding = re-introducing survivorship bias).

## Backtest design (UNCHANGED from v2)

- **Train:** 2018-04-30 → 2024-04-30 (6y)
- **Holdout (BURNT):** 2024-04-30 → 2026-04-30 (2y)
- **Rebalance:** 5d stride, 20d holding
- **Selection (primary):** bottom decile EW long-only by Lasso-fitted score (committing to NEGATIVE-sign hypothesis).
- **Cost:** 30bps RT (long-only)
- **Benchmark:** MDY (mid-cap)

**Secondary diagnostic (per perplexity, addresses long-only power loss):**
- Same Lasso fit, same panel, but report **L/S decile spread (long bottom − short top)** alongside long-only.
- **Not** the primary verdict (kept long-only for retail-realism + short-cost asymmetry); but if long-only FAILs and L/S PASSes, log as "constraint-driven power loss, not absence of alpha."
- L/S diagnostic does NOT count as additional Bonferroni test (same Lasso, same null).

## Multi-phase audit

5 phases, disjoint train tranches, common holdout. Rejection if mean αt across phases <2.86 OR phase dispersion (range αt) >50pp. Per `feedback_phase_aliasing_in_strided_backtests.md`.

**Regime stratification (per perplexity):** in attribution, split holdout into VIX>20 vs VIX<20 sub-periods. If alpha lives only in high-VIX regime, document as "tail-risk premium, regime-dependent" rather than "systematic alpha."

## Multiple-testing correction (zen + perplexity disagreed; locked here)

- Naive Bonferroni primary: **|αt| ≥ 2.86** (n=14 pre-reg discipline).
- Romano-Wolf stretch: **|αt| ≥ 3.27** (m=30, project-level FWER per zen).
- **Do NOT** allow framing "PASS by m=14, FAIL by m=30" — both must be reported in the verdict; primary is naive, stretch is supplementary.
- perplexity's m=14 argument is methodologically purer; zen's m=30 reflects project-search reality. Compromise: log both, decide via primary (naive).

## Pre-registration JSON (TEMPLATE — not yet locked)

```json
{
  "class": "options_implied_search_2026_05_xx",
  "version": "v7_smd_primary_4options_3equity_neg_sign_committed",
  "hypothesis": "options-implied vol features (IVP, IVX30, term-spread, IV-HV ratio) predict next-20d returns with NEGATIVE sign per Xing 2010 / Bali 2009",
  "test_program_count": 14,
  "primary_threshold": "|αt| >= 2.86 (naive Bonferroni n=14) AND phase dispersion <50pp",
  "stretch_threshold": "Romano-Wolf m=30 -> |αt| >= 3.27",
  "data_provider": "iVolatility $399 retail, /equities/stock-market-data primary",
  "feature_stack": ["ivp30", "ivx30", "ivx180_minus_ivx30", "ivx30_over_hv20", "reversal_1m", "momentum_6m", "rv_30d"],
  "selection_primary": "bottom decile EW long-only by Lasso-fitted score (NEGATIVE-sign hypothesis)",
  "selection_diagnostic": "L/S decile spread (bottom long, top short) — power-loss check only",
  "universe": "smd-derived dynamic optionable + ADV>=$2M + price>=$1, drop OTC pink",
  "delisting_handling": "forward-fill 5d, then -50% standard / -100% bankruptcy",
  "train_window": "2018-04-30..2024-04-30",
  "holdout_window": "2024-04-30..2026-04-30 (BURNT)",
  "rebalance": "5d stride, 20d holding",
  "cost_model": "30bps RT",
  "benchmark": "MDY",
  "phases": 5,
  "phase_dispersion_max_pp": 50,
  "regime_stratification": "VIX>20 vs VIX<20 sub-periods reported in attribution",
  "auto_pivot_triggers": [
    "PIT replication probe Pearson <0.95 -> ABORT (RESOLVED 2026-05-01: 0.9990 PASS)",
    "Lasso zero-coefs all 4 options features in train -> FAIL (selection-mechanism artifact)",
    "Phase dispersion >50pp -> FAIL",
    "Lasso flips sign vs literature prior across phases -> diagnostic flag, no auto-pass"
  ]
}
```

## Adversarial review checklist (BEFORE locking pre-reg)

- [x] Probe v5 retention ≥85% on optionable subset — **99.5% PASS**
- [x] PIT integrity replication probe Pearson ≥0.95 — **0.9990 PASS** (`docs/research/pit_replication_probe_2026_05_01.md`)
- [x] zen + perplexity adversarial review of feature stack — **DONE 2026-05-01, findings absorbed into v3**
- [x] Hypothesis direction committed ex-ante (Xing 2010 NEGATIVE prior) — **YES, locked**
- [x] Lasso multicollinearity addressed via stack reduction (7 features, orthogonal-by-construct) — **YES**
- [x] Universe filter dynamic (smd optVol/OI), not metadata — **YES, switched**
- [x] Delisting handling pre-committed (5d forward-fill + −50/−100%) — **YES**
- [x] Long-short secondary diagnostic implemented in backtest engine — **DONE 2026-05-02** (`BacktestEngine.bottom_n` parameter, `RebalanceSnapshot.bottom_n_*` fields, `BacktestReport.portfolio_returns_short` + `portfolio_returns_long_short` properties; 7 unit tests in `tests/test_backtest_engine_long_short.py`)
- [x] Phase-robust audit driver re-verified for 5-phase config + dispersion gate — **DONE 2026-05-02** (`robust_verdict()` extended with `dispersion_threshold_pp=50.0` kwarg; 5 unit tests in `tests/test_multi_phase_aggregator.py`; `dispersion_pp` surfaced in `audit_multi_phase.py` JSON output; smoke on mom_lowvol IS dispersion 48.8pp confirms gate doesn't false-trip)
- [x] Cost model parity check (30bps RT, long-only) — **DONE 2026-05-02** (`"long_only_30bps"` profile added to `_PROFILE_BPS`; 5 caller-composition unit tests in `tests/test_cost_model_v7_parity.py`)
- [ ] Pre-reg JSON locked via `alphalens preregister add` — **READY** (all engineering blockers cleared; full suite 1560/1560 green; smoke audit_multi_phase.py mom_lowvol verifies no regression)

## Files

- `scripts/probe_ivolatility_options_survivorship_v2.py` — probe v5 retention probe (smd-primary architecture)
- `scripts/probe_pit_replication.py` — PIT integrity replication gate (NEW 2026-05-01)
- `tests/test_probe_pit_replication.py` — 25 unit tests (NEW 2026-05-01)
- `tests/test_probe_ivolatility_v2.py` — 26 unit tests for retention probe
- `docs/research/options_provider_evaluation_2026_05_01.md` — vendor evaluation memo
- `docs/research/pit_replication_probe_2026_05_01.{json,md}` — PIT verdict artefacts (PASS)
- `docs/research/ivolatility_survivorship_probe_v2_2026_05_01.json` — retention verdict (PASS)

## Adversarial reviewers consulted (2026-05-01)

- **zen (gemini-3-pro-preview, thinkdeep + thinking_mode=high)** — flagged PIT showstopper, prescribed rolling-replication test (executed, PASS); prescribed 4-feature orthogonal stack (adopted); prescribed dynamic optionable filter via OI/volume (adopted); prescribed delisting penalty mechanic (adopted).
- **perplexity Sonar Reasoning Pro** — citation-backed: Xing 2010, Bali-Hovakimian 2009, An-Ang-Bali-Cakici 2014 converge on NEGATIVE sign (adopted as literature prior); long-only loses 30-50% power vs L/S benchmark in Quantpedia/Blitz et al. 2019 (added L/S diagnostic); m=14 vs m=30 reconciliation (logged both); regime-stratification recommendation (added to attribution).

## Status flags for next session

- PIT integrity gate: **PASS 0.9990** ✓
- Retention gate: **PASS 99.5%** ✓
- Adversarial review: **DONE** ✓
- Pre-reg lock: **UNBLOCKED 2026-05-02** — all 3 engineering items landed via TDD (plan `/Users/jacoren/.claude/plans/gentle-yawning-otter.md`, zen-reviewed); ready for `alphalens preregister add`.
- Decision deadline: 2026-05-08 (iVolatility trial expiry). Remaining work: lock pre-reg, build Phase A feature joiner (smd + Polygon aggs), run Phase B Lasso fit + 5-phase audit. ~5 days.
