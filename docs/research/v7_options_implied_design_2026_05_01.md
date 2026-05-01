# v7 design — options-implied features (DRAFT v2 — adversarial-review-revised)

**Status:** DRAFT — pending probe v3 results + iVolatility support response (deadline 2026-05-08).

**🚨 HARD ABORT TRIGGER (top-of-memo per zen):** Probe v3 strict retention (T1+T2) on optionable subset < 85% → unconditional FAIL of Phase A. Do not proceed to feature extraction. Pivot vendor (ThetaData $80 only viable alt; Polygon Options Developer DISQUALIFIED — 4y < 8y backtest target).

**Symbology resolution status (2026-05-01 PM empirical):** Option 1 (persistent optionId) **INSUFFICIENT** for our feature stack. Empirical test: `/equities/eod/ivs?optionId=...` returns 400 Bad Request — surface and term-structure endpoints are equity-keyed, accept ticker only. Option 1 works for per-contract `single-stock-option-raw-iv` (verified for SIVB → 17 rows of IV/Greeks via optionId without Q-suffix re-mapping) but cannot bulk-extract surface or term structure. **Reduces to Option 2 (accept look-ahead, "mortal sin" per zen) or Option 3 (vendor switch).** Effectively: if iVolatility support delivers Master Symbology → proceed; else cancel iVolatility, blind-buy ThetaData $80 or stop v7.

**Class:** `options_implied_search_2026_05_xx` (NEW class). Program-level Bonferroni stays n=13 → naive |t|≥2.86 / Romano-Wolf m≈50 → |t|≥3.5.

**Author:** Kamil. **Date:** 2026-05-01 PM.

## Hypothesis

**H₁ (revised per zen 2026-05-01):** Options-implied features (ATM IV percentile, 25Δ skew percentile, IV-vs-HV ratio percentile, term-structure slope percentile) — **NORMALIZED to 1y rolling rank per ticker** — provide cross-sectional signal for next-20d equity returns **AFTER controlling for standard equity factors** (1m reversal, 6m momentum, 30d realized vol). Specifically: high cross-sectional skew percentile predicts next-20d **mean reversion in distress** (the "survival premium" / overshoot-correction effect — long the most distressed expecting rebound).

**Long-only economic intuition (explicit per zen)**: this is a **survival-premium / mean-reversion** hypothesis. Going long the highest-skew names is betting that crash-fear-pricing overshoots actual crash probability. NOT betting on trend continuation. The strategy would FAIL if 2024-2026 holdout had unusual cluster of confirmed defaults among the long-decile.

**H₀:** Options-implied features (normalized as percentiles) add no predictive power beyond equity factor baselines (1m reversal + 6m momentum + 30d HV), OR signal exists but is uneconomic after 30bps RT cost.

**Bottleneck identified across alt_data class 6/6 FAIL + nonlinear 1/1 FAIL + analyst v10 ABORT**: features were the constraint, not architecture. Options-implied is a genuinely fresh feature class.

## Class context (multiple-testing discipline)

Per `feedback_burnt_holdout_multiplicity.md`: pure model-class swap on identical data does NOT cleanse multiplicity. Options-implied IS a fresh feature space (different data source, different concept) → counts as new class but PROGRAM-level Bonferroni still applies.

- Cumulative tests on 2024-04-30 → 2026-04-30 holdout: n=12 (alt_data 6/6, nonlinear 1/1, analyst ABORT counted, multi_source 3/3, multi-source-two-stage 1/1).
- Next test naive Bonferroni: |t|≥2.86 (n=13).
- **Romano-Wolf m=30 (revised down from 50 per perplexity 2026-05-01)**: primary |t|≥3.27. Justification: 8 of 12 prior tests share Lasso+alt_data architecture; their test statistics are correlated, so true "effective independent" hypothesis count is lower than total run count. m=30 reflects ~half of architectural reuse.
- Per-class fresh threshold |t|≥1.96 is **NOT** valid per zen CR (multiplicity abuse).

## Universe construction (CRITICAL)

Per zen CR 2026-05-01: probe must measure on objective optionable universe to avoid SPAC/no-options-small-cap contamination.

**Step 1 — Optionable filter** (offline, one-time):
- Source: Polygon Starter `/v3/reference/options/contracts?underlying_ticker=X&as_of=Y`
- For each candidate ticker, query as_of = trading_date - 30d. If results > 0 → optionable at that point.
- Persisted in `~/.alphalens/survivorship/optionable_delisted_2018_2024.parquet` (built by `scripts/build_optionable_universe.py`).
- Active-listed universe: TBD — likely Polygon Starter `/v3/reference/tickers?market=stocks&active=true&as_of=...` cross-referenced with chain-ref.

**Step 2 — Liquidity filter** (per asof, REVISED per zen 2026-05-01 PM — earlier filters too restrictive, eject the death-spiral regime where signal lives):
- Min ADV (20-day avg dollar volume) ≥ **$2M** (was $5M — too tight for distress regime)
- Min underlying price ≥ **$1** (was $5 — captures Ch11 death-spiral, $2-$4 distress)
- ~~Min market cap ≥ $300M~~ — DROPPED. Distress strategies want microcaps in death spiral.
- Drop OTC pink sheets only (avoid hopelessly illiquid)
- TBD: source for ADV PIT data — Polygon stock aggs available

**Expected universe size**: ~2000-2500 names per asof (slightly larger than alt_data due to looser bounds).

## Symbology layer (KNOWN RISK — flagged by zen)

Per zen CR: querying iVolatility's post-delisting ticker (SIVB→SIVBQ) introduces look-ahead bias because Q-suffix is assigned AT/AFTER bankruptcy. Using post-delisting ticker to query pre-delisting data uses information from the future.

**Mitigation options** (decision pending iVolatility support response on Master Symbology):

1. **Pure-symbol-as-of** (preferred if iVolatility support confirms): Use iVolatility's `/equities/option-series` with `optionId` (which iVolatility claims is persistent across corporate actions per their docs). Query optionId historically without ticker re-mapping.

2. **Acceptable look-ahead** (fallback): Document Q-suffix mapping as known limitation. Constraint: only use for retention/coverage measurement, NOT for production feature extraction at asof. For asof-t feature extraction, query iVolatility with the ticker AS KNOWN at asof-t.

3. **Vendor change** (if 1+2 unworkable): switch to ThetaData $80 or Polygon Options Developer $79. Both unverified for delisted retention but neither has Q-suffix problem.

**Decision rule**: if iVolatility support cannot confirm option_series-on-date works without forward ticker re-mapping → de-prioritize iVolatility for v7 production despite high retention number.

## Feature stack (REVISED v2 — adds equity controls + percentile normalization per zen)

**Options features** (all converted to **1y rolling rank percentile per ticker** — raw IV cross-sectionally invalid: 60% IV biotech ≠ 60% IV utility):

1. **ATM IV 30d percentile** — `/equities/eod/ivx` `30d IV Mean`, then 1y rolling rank
2. **25Δ skew 30d percentile** — `/equities/eod/ivs` interpolated to 25Δ put−call IV, then 1y rolling rank. *Empirical: AAPL has 28 contracts in delta [0.20, 0.30] neighborhood per asof — interpolation robust*. *Skew construction convention pending perplexity literature gap — likely 25Δ put IV − 25Δ call IV (most common); alternative Xing 2010 RNS = 25Δ put IV − ATM IV*. **Note:** cleaner endpoints `/ivs-by-delta`, `/ivs-parameterized` (with parabolic coefs a, b, c) are 403 tariff-blocked — only available via Historical PRO / Backtesting PRO plans. Must use raw `/ivs` per-strike + interpolate. **Performance optimization (verified via context7)**: use server-side `OTMFrom/OTMTo` + `periodFrom/periodTo` filters to query 25Δ neighborhood only (~5-10× smaller payload than full 338-record surface).
3. **IV-vs-HV ratio percentile** — `/ivx` 30d IV Mean ÷ `/hv` 30d HV, then 1y rolling rank
4. **Term-structure slope percentile** — `/ivx` 90d IV Mean ÷ 30d IV Mean, then 1y rolling rank

**Equity controls** (Lasso must include alongside options features per zen — without these, model rediscovers momentum/vol):

5. **1m reversal** — −1 × stock return last 21 trading days
6. **6m momentum** — stock return [t-126, t-21] (skip last month per Jegadeesh-Titman convention)
7. **30d realized volatility** — annualized stdev of log returns last 30 days

**Lasso fit**: regress 20d forward return on all 7 features simultaneously. **Options features must survive penalization alongside equity controls** — only then is options-implied alpha demonstrated. If Lasso zero-coefs all options features but keeps equity controls → H₀ confirmed.

**ETL anomaly bounds (drop rows)** (per zen blind spot 2, calibrated 2026-05-01 PM):
- ATM IV > **3.0 (300%)** — calibrated DOWN from zen's initial 500% per empirical verification: SIVB max IV during halt week was 245%; 500% would be over-permissive. UNVERIFIED if any legitimate distress IV exceeds 300% — may need re-tuning.
- |25Δ skew| > 2.0 — UNVERIFIED, accept zen's bound provisionally
- IV-vs-HV ratio > 10 or < 0.1 — UNVERIFIED, accept zen's bound provisionally
- Stock price < $1 — penny territory (post-filter check)

**VERIFICATION STATUS of zen+perplexity prescriptions (annotated 2026-05-01 PM)**:
- ✅ Raw IV cross-sectional invalid → percentile normalization: VERIFIED (KO 16.9% / NVDA 44.2%, 2.6× variance across stable mega-caps)
- ⚠️ Universe filter $5 ejects distress: PARTIAL — true for slow decline ($20→$4 falls through filter), false for halt events (SIVB pre-halt was $267, ejected only post-bankruptcy at $0.10). $1 floor still recommended.
- ❌ IV > 500% bound: empirical max in distress sample was 245% — **calibrated to 300%**.
- ✅ Polygon Developer 4y < 8y train: VERIFIED arithmetically.
- ⏳ Equity controls (1m reversal, momentum, HV): UNVERIFIED — would require running Lasso. Adopted on academic-consensus basis (Carhart-4F).
- ⏳ Romano-Wolf m=30 vs m=50: UNVERIFIED — methodological judgment, requires more research on effective independent test count.

**Rejected at current tier (BLOCKED 403)**:
- ~~Put/Call OI ratio~~ — UNBLOCKED 2026-05-01 PM via `/equities/eod/stock-opts-by-param` (retail tier endpoint, requires dteFrom/dteTo/cp + delta/moneyness range)
- ~~Put/Call volume ratio~~ — UNBLOCKED via same endpoint
- ~~GEX~~ — UNBLOCKED via same endpoint (returns gamma, openinterest, underlying_price per contract)

**EXPANDED feature stack (9 features total)** — original 6-options stack RESTORED:

8. **P/C OI ratio percentile** — sum openinterest by call_put from stock-opts-by-param @ 30d±15dte, then 1y rolling rank
9. **P/C volume ratio percentile** — same, sum volume
10. **GEX percentile** — sum(gamma × OI × underlying_price) per side (call positive, put negative dealer-positioning model), net, then 1y rolling rank

**Verified empirically 2026-05-01 PM** on SIVBQ pre-halt 2023-03-08:
- P/C OI ratio: 2.151 (bearish positioning detected)
- P/C vol ratio: 2.171
- GEX net: −3359 (vol amplification regime)
- These are exactly the v7 features we needed.

**Architectural note**: 6-options + 3-equity-controls = 9-feature stack now feasible at retail tier. Reduction-then-restoration was driven by docs discovery via Playwright (context7 missed `stock-opts-by-param` accessibility). Same Q-suffix look-ahead bias risk persists — needs Master Symbology resolution from iVolatility support.

## Backtest design

- **Train**: 2018-04-30 → 2024-04-30 (6y)
- **Holdout** (BURNT): 2024-04-30 → 2026-04-30 (2y) — same as alt_data class
- **Rebalance**: 5d stride, 20d holding (matches alt_data convention)
- **Cross-section**: top decile EW long-only (matches alt_data v5)
- **Selection**: rank by Lasso-fitted 20d return with 4 features
- **Cost**: 30bps RT (long-only)
- **Benchmark**: MDY (mid-cap, per v6a learning — SPY mega-cap drift contaminated alt_data v5 verdict)

## Multi-phase audit (5 phases, per `feedback_phase_aliasing_in_strided_backtests.md`)

Each phase = Lasso fit on disjoint train tranche, evaluate on common holdout. Rejection if mean αt across phases <2.86 OR phase dispersion (range αt) >50pp.

## Pre-registration JSON template (LOCK before run)

```json
{
  "class": "options_implied_search_2026_05_xx",
  "version": "v7_atmiv_skew_ivhv_termslope",
  "hypothesis": "options-implied features predict 20d returns cross-sectionally",
  "test_program_count": 13,
  "primary_threshold": "|αt| >= 2.86 (naive Bonferroni n=13) AND phase dispersion <50pp",
  "stretch_threshold": "Romano-Wolf m=50 -> |αt| >= 3.5",
  "data_provider": "iVolatility $399 trial",
  "feature_stack": ["atm_iv_30d", "skew_25d_30d", "iv_hv_ratio_30d", "term_slope_30_90d"],
  "universe": "Polygon-verified optionable + ADV>=$5M + price>=$5 + mcap>=$300M",
  "symbology_strategy": "TBD pending iVolatility support response 2026-05-08",
  "train_window": "2018-04-30..2024-04-30",
  "holdout_window": "2024-04-30..2026-04-30 (BURNT)",
  "rebalance": "5d stride, 20d holding",
  "selection": "top decile EW long-only by Lasso-fitted score",
  "cost_model": "30bps RT",
  "benchmark": "MDY",
  "phases": 5,
  "phase_dispersion_max": 50,
  "auto_pivot_triggers": [
    "Phase A coverage <85% optionable retention -> ABORT",
    "Phase A symbology look-ahead unconfirmed -> ABORT pending support",
    "Phase B mean rank-IC <0.005 -> ABORT pre-Phase C"
  ]
}
```

## Adversarial review checklist (BEFORE running Phase B)

- [ ] iVolatility retention probe v3 ≥ 85% on optionable subset (PENDING — pre-filter currently running)
- [ ] iVolatility support response on Master Symbology reviewed
- [ ] Symbology strategy chosen (option 1, 2, or 3 above)
- [ ] zen + perplexity adversarial review of feature stack (per `feedback_adversarial_review_saves_compute.md`)
- [ ] IV outlier handling in ETL (zen blind spot 2: drop rows where IV >5.0 or skew inverted/flat from wide NBBO during distress)
- [ ] PIT integrity: confirm /equities/eod/ivx data is frozen-as-of, NOT retrospectively revised after corporate actions
- [ ] Bonferroni n=13 threshold locked (NOT reset to in-class n=1 |t|≥1.96 — multiplicity abuse)
- [ ] Phase-robust audit driver ready (5 phases, dispersion <50pp gate)
- [ ] Cost model parity check (30bps RT applied correctly to long-only)

## Decision matrix (vendor selection)

| Path | Cost | Coverage | Symbology | Verdict deadline |
|------|------|----------|-----------|------------------|
| iVolatility $399 + Master Symbology | $399/mo | TBD probe v3 | If support delivers Master Symbology mapping (NOT optionId — empirically dead for /ivs/ivx) | 2026-05-08 |
| ~~iVolatility $399 + accept look-ahead~~ | — | — | "Mortal sin in quant design" per zen — DROPPED | — |
| ThetaData $80 (blind) | $80/mo | unverified | Unknown — needs separate probe | viable IF iVolatility fails |
| ~~Polygon Options Developer $79~~ | — | — | Mathematically disqualified — 4y history < 8y backtest target | — |
| **Cancel iVolatility, defer v7 to later cohort** | $0 | n/a | n/a | safest if all paths fail |

## Open questions (to resolve before locking pre-reg)

1. iVolatility support response on Master Symbology / persistent optionId behavior across corporate actions
2. Probe v3 retention rate on optionable-filtered universe
3. Confirmation that Polygon Options Developer $79 covers 2018-2024 train period (4y history may NOT reach back to 2018)
4. ThetaData EOD trial availability (user reported NO trial; may have changed)
5. ETL anomaly bounds (zen blind spot 2): IV cap at 500%, skew bounds, distress-period filtering

## Adversarial reviewers consulted

- zen (gemini-3-pro-preview) — vetted probe v2 design, found 2 bugs, then 3 more, prescribed strict T1+T2 gate, prescribed Polygon-verified optionable filter
- perplexity Sonar Reasoning Pro — backed zen's verdict 100%, added "hard PIT violation" framing for BBBY/OSTK
- Self exploratory testing — found 5/5 T4 are no-options small-caps/SPACs

## Files

- `scripts/probe_ivolatility_options_survivorship_v2.py` — probe v3 (with bug fixes and --optionable-only flag)
- `scripts/build_optionable_universe.py` — Polygon pre-filter for optionable universe
- `tests/test_probe_ivolatility_v2.py` — 21 unit tests (TDD)
- `docs/research/options_provider_evaluation_2026_05_01.md` — vendor evaluation memo
- `docs/research/ivolatility_survivorship_probe_v2_2026_05_01.json` — probe artefacts (overwritten per run)
- `~/.alphalens/survivorship/optionable_delisted_2018_2024.parquet` — Polygon-verified optionable pool

## Status flags for next session

- Probe v3 with --optionable-only: PENDING (pre-filter running, ETA ~23 min)
- iVolatility support email: SENT 2026-05-01, awaiting response
- Pre-reg JSON: NOT LOCKED — will lock after probe v3 + support response
