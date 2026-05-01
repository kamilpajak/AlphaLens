# multi_source_global_lasso_20d Phase B — findings (2026-04-30)

**Pre-registration:** [`multi_source_global_lasso_20d_2026_04_30`](preregistration/params_multi_source_global_lasso_20d_2026_04_30.json)
**Class:** `multi_source_two_stage_search_2026_04_30` (now 3/3 FAIL)
**Status:** **FAIL** — Phase B zero-coef structural artifact, Phase C skipped per pre-reg. **13th paradigm failure.**

## TL;DR

v3 was the horizon ablation from v2: same 21 features, same Global Lasso, same procedure
— only target horizon 5d → 20d (with overlapping 4-tranche to preserve power, HAC=5
explicit, Lo-2002 Sharpe). **Result: Lasso CV again zeroed every coefficient.** The
apparent t=+1.32 with excess_net=+20.7%/y is the same structural artifact pattern v2
exhibited (alphabetically-first 30 ADV-liquid tickers tilt vs cap-weighted SPY). Phase B
verdict_for() correctly flagged FAIL via the pre-reg's "≥1 nonzero coef" gate.

| | v1 (4-regime, 5d) | v2 (global, 5d) | **v3 (global, 20d)** |
|---|---:|---:|---:|
| Phase 0 αt | +2.01 | +2.03 | +1.32 |
| Nonzero coefs | 15/21 (Q1 only) | **0/21** | **0/21** |
| Mean phase αt | +0.65 | +0.55 | n/a (FAIL on Phase B) |
| Verdict | FAIL | FAIL | **FAIL** |

## Decisive cross-experiment finding

After three single-variable ablations within this signal class, the bottleneck is
**neither architecture (v1 vs v2) nor horizon (v2 vs v3)**. It is the **features
themselves** under L1 regularization.

Lasso CV at the optimal λ:
- 4-regime split @ 5d: **only Q1_calm** surfaced 15 nonzero coefs (small subsample);
  all other regimes shrunk to zero
- Global pool @ 5d: 0/21 zero
- Global pool @ 20d: 0/21 zero

The 21-feature whitelist genuinely has no predictive signal for forward equity returns
at the L1 + CV-MSE minimum that the procedure selects, regardless of horizon or
partition. This is the most decisive negative result the project has produced.

## Phase B v3 numerical detail

```
HOLDOUT 2024-2026 | ADV≥$5M cost=10bps | n=95 topN=30.0 turn=12.9% |
Sh gross=1.01 net=0.98 | excess gross=22.7% net=20.7% | α 4F=125.3% t=1.32
verdict: FAIL (zero-coef structural artifact — see v2 finding)
```

Inference machinery (per zen + Perplexity adversarial review):
- HAC maxlags=5 explicit (correct for MA(3-4) overlap autocorrelation)
- Lo (2002) variance-ratio-adjusted Sharpe via `sharpe_autocorr_adjusted(max_lag=5)`
- 20d-aggregated Carhart factors at each stride=5 asof
- Sample preserved at n=95 (vs the n=25 that my original Proposal A would have given)

These corrections were correctly implemented; the verdict is honest. The structural
artifact still leaks through because it pre-dates the inference layer — once Lasso
zeros all coefs, no regression machinery downstream can recover real alpha because
there is none in the model.

## MaxDD = −62%

Substantially worse than v1 (−9.8%) and v2 (−10.1%). Diagnostic: at 20d holding the
"alphabetically-first 30 ADV-liquid" portfolio compounds 20-day drift before any
rebalance can react. The underlying basket (essentially a static large-cap portfolio
filtered by liquidity) had a bad 20d window in 2025 that the structural metrics
captured. With the model contributing no actual signal, drawdown is purely beta×SMB×Mom
loading + idiosyncratic large-cap noise.

This **strengthens** the FAIL verdict: even taking the result at face value (ignoring
zero-coef diagnostic), MaxDD=−62% violates the pre-reg's `≥−35%` gate.

## Phase C skipped

Pre-reg fail_rule explicitly: "If holdout FAIL outright, skip Phase C (saves ~85min
compute) and log FAIL directly." Applied. Saved compute will be redirected toward
v4 design.

## Comparison across 13 paradigm failures (phase-robust era)

| Strategy | mean αt | mean excess_net | dispersion | Lasso coefs |
| --- | ---: | ---: | ---: | ---: |
| mom+lowvol_combo | +0.49 | −5.7% | 44.5pp | n/a (linear z) |
| quality+momentum | +0.38 | +10.3% | 69.0pp | n/a (linear z) |
| vol_target_overlay | +0.49 | −7.0% | 40.3pp | n/a (overlay) |
| multi_source_two_stage (v1, 4-regime, 5d) | +0.65 | 0.0% | 4.9pp | 15 in Q1 only |
| multi_source_global_lasso (v2, global, 5d) | +0.55 | −0.2% | 5.6pp | 0/21 |
| **multi_source_global_lasso_20d (v3, global, 20d)** | **n/a** | **n/a** | **n/a** | **0/21** |

## What this rules out for v4

**Out** (variables now exhausted within this class):
- Stage 1 architecture (regime conditioning) — settled by v2 ablation
- Target horizon 5d vs 20d — settled by v3 ablation
- Pooled vs per-regime fitting — both produce zero coefs at both horizons

**Could be tested** (each pays Bonferroni n=4 → 2.50 in same class):
- ElasticNet vs Lasso (l1_ratio adjusts L1 vs L2 mix)
- CV-Sharpe or CV-IC vs CV-MSE (rank-correct objective, not RMSE)
- Tree-based nonlinear (LightGBM, with discipline to limit tuning surface)

**Stronger alternative** (NEW signal class, fresh n=1):
- Different feature set entirely: alt-data (news sentiment, options-implied vol skew,
  earnings revisions, analyst dispersion, short interest)
- Fundamentals-based features that aren't proxies for OHLCV (cash flow accruals,
  capital expenditure intensity, inventory turnover, operating leverage)

## Recommendation for v4

Given that 3 architecture+horizon variations of the SAME features all produce zero
coefs, **the most informative next experiment is in a fresh signal class with
substantially different features**, NOT another ablation in this class.

Two reasons:
1. Statistical: Bonferroni in this class is now n=4 → 2.50, and we have strong evidence
   (3/3 zero-coef at the L1 minimum) that the features lack signal. Spending budget
   on ElasticNet/CV-Sharpe is unlikely to surface signal that Lasso couldn't find.
2. Information value: a fresh-class test of different features answers a different
   question ("do retail-accessible alt-data signals work?"), whereas same-features
   ablations only refine our null result.

Provisional v4 design (NOT yet pre-registered):
- New class: `alt_data_screener_search_2026_05_XX` (fresh Bonferroni n=1, threshold 1.96)
- Features: TBD; candidates include analyst revision dispersion (free from
  Refinitiv/Estimize via Yahoo, EPS revision count), short interest change (FINRA),
  options put/call ratio (CBOE), news sentiment (free tier from Polygon).
- Procedure: same Lasso + CV + HAC framework (proven correct in v1-v3); only the
  feature inputs differ.

This is **not yet committed** — needs explicit user approval and feature-data
sourcing investment before pre-registration.

## Adversarial review value confirmed

zen + Perplexity review prevented a wasted ~3h of compute on my original Proposal A
(stride=20 + n=25 + per-regime). Their corrections (overlapping 4-tranche, HAC=5,
single-variable from v2) were technically correct and produced an interpretable
verdict in 17 minutes instead of 1.5 hours.

The fact that **even with the correctly-implemented v3 we still got zero coefs** is a
much more decisive negative result than a noisy single-OOS would have been. The
methodology validates itself.
