# v6a-revised verdict — Mega-cap exclusion + MDY benchmark + retrained Lasso

**Date:** 2026-05-01 PM
**Pre-reg:** `alt_data_screener_v6a_revised_2026_05_01` (Romano-Wolf m≈50 → |t|≥3.5 primary, |t|≥4.0 stretch)
**Provenance:** Cache-enabled exploratory test post-v5 FAIL. Original v6a REJECTED by zen + perplexity adversarial review as mathematically self-defeating; revised with 5 mitigations.

## Headline verdict: FAIL — but dramatically less catastrophic

| Metric | Value | v5 baseline | Δ vs v5 | Gate | Result |
|---|---|---|---|---|---|
| Carhart-4F α t-stat (HAC=5) | **−0.67** | −3.20 | **+2.53** | ≥+3.5 | FAIL — but neutral, not anti-alpha |
| α annualized | −37.6%/y | −192.7%/y | 5× less extreme | ≥3% | FAIL |
| Sharpe net (Lo-adj) | −1.05 | −1.11 | similar | ≥0.5 | FAIL |
| Excess vs benchmark (net) | −18.4%/y vs MDY | −37.3%/y vs SPY | −19pp | ≥3% | FAIL |
| Max drawdown | −45.2% | −66.7% | improved | ≥−25% | FAIL |
| in-CV IR | 0.402 | 0.338 | +0.064 | ≥0.5 | MID-by-gate |
| Holdout mean rank-IC (subset) | +0.0238 | +0.0260 | similar | >0 | PASS (informational) |
| Nonzero Lasso coefs | 2/10 | 2/10 | refit slightly different mags | ≥1 | PASS structurally |

**Verdict:** FAIL on primary gates. Stretch threshold 4.0 not assessed (primary failed).

## Lasso fit (retrained on non-mega-cap subset)

```
λ chosen = 0.002565 (idx 4/24)  # vs v5's 0.00289 — slightly less regularization
n_train = 200,297 (filtered subset, vs v5's 221,920)
nonzero coefs (2/10):
  rank_short_interest_pct_float = −0.0057  # v5: −0.0063 (very similar)
  filing_density_4q             = +0.0025  # v5: +0.0016 (1.5× larger)
```

**Key empirical:** Refitting Lasso on non-mega-cap train pool DID change coefficient
magnitudes mildly (perplexity Q4 was right that retraining matters), but signs
and ranking remained stable. The 1.5× larger filing_density coefficient suggests
this signal is somewhat stronger in the non-mega-cap subset, but still
insufficient for actionable α.

## Critical diagnostic — SPY vs MDY benchmark gap decomposition

| Sub-period | n_rebal | Long ann | SPY ann | Excess vs SPY | MDY ann | Excess vs MDY |
|---|---|---|---|---|---|---|
| 2024-04-30 → 2024-12-31 | 34 | +34.1%/y | +75.8%/y | **−45.9%** | +42.8%/y | **−8.7%** |
| 2025-01-01 → 2025-12-31 | 50 | +12.4%/y | +67.6%/y | **−51.4%** | +40.3%/y | **−27.9%** |
| 2026-01-01 → 2026-04-30 | 10 | +10.9%/y | −68.7%/y | +79.5% | −4.6%/y | +15.5% |

**Decomposition arithmetic:**
- 2024: SPY-gap was 45.9pp, MDY-gap is 8.7pp → SPY mega-cap concentration explained
  **(45.9 − 8.7) / 45.9 = 81%** of v5's 2024 underperformance.
- 2025: SPY-gap was 51.4pp, MDY-gap is 27.9pp → SPY mega-cap explained
  **(51.4 − 27.9) / 51.4 = 46%** of v5's 2025 underperformance.
- 2026: gap-direction flips both vs SPY and MDY.

**Headline diagnostic:** SPY mega-cap concentration was a MAJOR but NOT SOLE driver
of v5's catastrophic FAIL. Cap-matched MDY benchmark cuts the gap roughly
in half on average — but the residual −18.4%/y net excess vs MDY is STILL
significantly negative.

## Why v6a-revised STILL fails (with cap-matched benchmark)

The 2025 sub-period is decisive: long leg +12.4%/y, MDY benchmark +40.3%/y
→ even with mega-caps EXCLUDED from selection AND benchmark, the mid-cap
universe (where our long pool sits) **also rallied 40%/y** in 2025. Our
ranking via `rank_SI` (−0.0057) selects HIGH-SI / LOW-rank-SI names that
don't participate in the mid-cap rally either.

zen's adversarial prediction confirmed: "asking a linear model to extract
a +2.85 t-stat alpha from a baseline -35% gross excess return is asking
for a miracle." With MDY benchmark, gross excess improved to −18% but
αt=−0.67 still says "no alpha, no anti-alpha — the model is essentially
neutral." The signal we computed exists in bulk (rank-IC=+0.0238 positive)
but is not concentrated enough at decile tails to overcome benchmark drift.

## What v6a-revised settles

1. **SPY mega-cap concentration was a real but partial driver of v5's FAIL.**
   Cap-matched benchmark recovers ~half the gap but doesn't surface alpha
   that wasn't there. v5's +20.6%/y long-leg was alpha illusion vs ANY
   benchmark in this rally regime — mega-cap concentration just amplified
   the illusion's visibility.
2. **Retraining Lasso on non-mega-cap subset doesn't materially change
   feature ranking.** Same 2 nonzero coefs, similar magnitudes, similar
   directional signs. Eliminates "training set contamination" concern but
   confirms signal-or-lack-thereof is intrinsic to the feature stack.
3. **Features are bottleneck (CONFIRMED 6× across classes).** Now also
   tested under universe-restricted Lasso refit. No selection rule, model
   class, or universe filter rescues this 10-feature stack on burnt holdout.
4. **alt_data_screener_search_2026_04_30 class CLOSED 6/6 FAIL.**
5. **Program-level Bonferroni → n=12.** Next test |t|≥2.85 (naive) or
   |t|≥3.5 (Romano-Wolf if same class iteration). True effective m≈50+
   per perplexity adversarial review.

## Operational gain — cache key bug + fix

Discovered during v6a kickoff: original cache key serialized realized
asof_dates calendar, which drifts day-to-day as yfinance bars accrue.
Result: v6a-revised first attempt was a cache MISS despite identical
INPUTS to v5 (different calendar from one extra SPY trading day).

**Fix:** cache key now keyed on (sorted universe, train_start, holdout_start,
holdout_end, stride, holding, features_v) — INPUT params only, NOT realized
calendar. Benchmark also dropped from key (features are benchmark-agnostic
since SPY/MDY are excluded from PIT universe by construction).

**Result:** v6a-revised re-run loaded cache in 24ms, total runtime ~2 min
(vs original ~50 min build). Existing v5 cache file renamed to new hash
(features identical, key just normalized).

**Saved:** ~50 min compute on v6a-revised; ~50 min × N future iterations.

## What v6a-revised does NOT settle

- Whether ANY benchmark choice would surface alpha (RSP equal-weight S&P,
  IWM Russell 2000, custom universe-EW). MDY is closest cap-match but
  tested only one alternative.
- Whether long-only without benchmark (just Sharpe of long leg vs T-bill)
  would change verdict — but capital deploy off-table regardless.
- Whether OTHER feature spaces (options-implied, news sentiment, analyst
  events from a non-yfinance provider) would surface alpha on this same
  burnt holdout.

## Files

- Audit JSON: `docs/research/alt_data_screener_v6a_revised_audit.json`
- Phase B report: `docs/research/alt_data_screener_v6a_revised_phase_b.md`
- Pre-reg artifact: `docs/research/preregistration/params_alt_data_screener_v6a_revised_2026_05_01.json`
- Driver: `scripts/experiment_alt_data_v6a_revised.py` (cache-aware, input-only key)
- Feature cache: `~/.alphalens/feature_cache/alt_data_features_cb60bae3dff3233b.parquet` (renamed from de036... after key fix)
- Adversarial review docs: `docs/research/v6a_mega_cap_exclusion_design_2026_05_01.md`

## Next experiment options (cache-enabled, fast iteration)

1. **v6b — Sector-neutral ranking** (within non-mega-cap pool): test if
   sector concentration (tech weight) was driver. Cache-compatible.
2. **v6c — Score-percentile-weighted continuous portfolio** (no decile cut):
   harvest bulk +0.024 rank-IC across full cross-section instead of tail
   only. Cache-compatible.
3. **v6d — Russell 2000 explicit universe** (when membership history
   available): exogenous criterion vs our top-10% percentile.
4. **v6e — L/S market-neutral** (zen alternative): isolate factor signal
   from benchmark beta entirely. Same cache, different cost model.
5. **v7 — Fresh feature class** (options-implied via ThetaData $50-75/mo):
   structurally fresh feature space. NOT cache-compatible (new joiner).
   Per perplexity Q6 ranking, this is highest information value.

**Recommended next:** v6c (continuous score-weighted) — tests bulk-IC
extraction hypothesis without tail-decile assumption. Settles whether
selection-rule discretization itself is responsible for failure.
