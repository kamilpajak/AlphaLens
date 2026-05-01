# v6c design — Continuous score-weighted portfolio (post-v6a cache exploitation)

**Status:** REJECTED-PRE-RUN 2026-05-01 PM after zen adversarial review.

**Reason:** Mathematically pre-doomed by IC × breadth ceiling. rank-IC=0.024
→ theoretical max Sharpe ~0.34 BEFORE costs → t-stat ≈ 0.48 for 2-year
holdout. To hit Romano-Wolf |t|≥3.5, would need Sharpe ≈ 2.47 annualized —
unreachable. Plus continuous L/S turnover 100-200%/mo → cost drag ~85%/y
vaporizes any signal.

**Implication for cache-enabled alt_data exploration:** ALL selection-rule
variants (decile, continuous, sector-neutral, L/S decile, etc.) on this
10-feature stack share the same IC×breadth ceiling. To break through:
need NEW feature class (ThetaData options-implied), longer holding (cache
invalidation), or different universe entirely.

**Next path:** v7 ThetaData options-implied (requires $50-75/mo subscription).

**Class:** in-class continuation `alt_data_screener_search_2026_04_30` (7th hypothesis test).

**Threshold:** in-class n=7 → naive |t|≥2.66. PROGRAM-LEVEL n=13 → naive |t|≥2.86.
**Romano-Wolf m≈50+ → primary |t|≥3.5, stretch |t|≥4.0.** Same conservative
threshold as v6a-revised (no relaxation post-FAIL of conservative test).

## Hypothesis

**H₁:** v3-v6a all use **decile-tail selection** (top 10% EW long pool). Holdout
mean rank-IC = +0.024 is a population-level statement about the WHOLE
cross-section, not just the tail. Continuous score-weighting via
`w_i = (rank_percentile_i - 0.5)` (centered, dollar-neutral) harvests
the bulk signal across all tickers proportionally rather than discarding
the middle 80% of cross-section information. Tests: does the bulk +0.024
rank-IC translate to actionable alpha when we don't truncate to decile tail?

**H₀:** Bulk rank-IC of +0.024 is too small to overcome benchmark drift
even with continuous weighting; decile FAIL is intrinsic, not selection-rule
artifact.

## Design

ONE variable changes vs v6a-revised: portfolio construction.

| Variable | v6a-revised | v6c | Rationale |
|---|---|---|---|
| Universe filter | drop top-10% mkt-cap | **drop top-10% mkt-cap** (unchanged) | continuity for cap-matched comparison |
| Lasso fit | on filtered subset | **on filtered subset** (unchanged) | retrained per perplexity ★★★ |
| Score → portfolio | top-decile EW long-only | **continuous w_i = 2 × (rank_pct_i − 0.5)** | NEW |
| Portfolio constraint | dollar-long only | **dollar-neutral L/S by construction** | continuous weighting → dollar-neutral when rank centered at 0 |
| Decile size | floor(N/10) | n/a (continuous) | |
| Benchmark | MDY primary, SPY descriptive | **MDY primary, SPY descriptive** | continuity |
| Cost | 30 bps RT (long only) | **60 bps RT (L/S, both legs)** | dollar-neutral pays both sides |
| Cost adjustment | half-spread × turnover | half-spread × turnover (sum of |Δw_i|) | turnover via ‖Δw‖₁ |
| Train/holdout | 2018-2024 / 2024-2026 | unchanged | |
| Stride / holding | 5d / 20d | unchanged | |
| Threshold (primary) | |t|≥3.5 | **|t|≥3.5** | unchanged Romano-Wolf |

## Continuous weight definition

At each asof, after Lasso scoring on filtered subset:

```
N = number of long-eligible names (post-ADV + non-mega-cap)
rank_pct_i = rank(score_i) / N           # in [1/N, 1]
w_i_raw = 2 × (rank_pct_i − 0.5)          # in approx [−1, +1]
w_i = w_i_raw / sum(|w_j|)                # L1-normalized so total gross exposure = 1
```

This is a **dollar-neutral** portfolio (sum(w_i) ≈ 0 by symmetry of rank percentile)
with **gross leverage = 1** (sum(|w_i|) = 1). Pure factor exposure with minimal
benchmark beta.

Returns:
```
portfolio_return = sum(w_i × forward_return_i)
```

**Equivalence to alternative formulations:**
- `2 × (rank_pct − 0.5)` is identical to standardized rank-target after centering.
- Equivalent to the cross-sectional regression coefficient interpretation:
  return ≈ β × score, where β captures rank-IC.
- Maximum theoretically extractable Sharpe ≈ rank-IC × sqrt(rebalances_per_year /
  CV_of_realized_excess) under iid assumption.

## Primary metric

Carhart-4F regression of `(portfolio_return − MDY_return)` on Mkt-RF/SMB/HML/Mom
with HAC maxlags=5, subtract_rf=False.

**Note:** Dollar-neutral L/S portfolio has near-zero β to Mkt-RF (theoretically).
MDY benchmark subtraction removes any residual cap-tilted bias. Carhart
α captures pure factor alpha not explained by 4 standard factors.

## What "PASS" means

Same gates as v6a-revised, except MaxDD threshold loosened slightly because
dollar-neutral portfolios can have higher path-volatility:

1. Carhart-4F α t-stat (HAC=5) on (portfolio − MDY) ≥ **3.5**
2. ≥1 Lasso nonzero coef
3. in-CV mean rank-IR ≥ 0.5
4. Lo-adjusted Sharpe net ≥ 0.5
5. α annualized ≥ 3%
6. **MaxDD ≥ −20%** (tighter than v6a's −25% — dollar-neutral should be steadier)
7. Holdout mean rank-IC > 0 (within filtered subset)

**Stretch:** primary PASS + α t-stat ≥ 4.0 → "robust exploratory positive."

## HARKing acknowledgment

v6c is post-hoc designed against v6a's diagnostic ("bulk +0.024 rank-IC
exists but decile tail doesn't extract it"). Same burnt holdout. Mitigations:

1. Same Romano-Wolf threshold (3.5) as v6a-revised — no relaxation.
2. EXPLORATORY framing — fresh-OOS confirmation required for any escalation.
3. Continuous weighting is a **theoretically motivated** transformation (rank-IC
   harvest), not arbitrary selection rule choice. Reduces multiplicity inflation
   somewhat (it's mechanical translation of rank-IC, not new hypothesis search).

## Open risks for adversarial review

1. **Cost drag dominates.** Dollar-neutral L/S has sum(|w_i|) = 1 turnover EVERY
   rebalance because weights re-rank with new scores. Cost drag could be 60+
   bps RT × 50 rebals/yr = ~3%/y, eating most of any small-α signal.

2. **rank-IC = 0.024 is small.** Theoretical max Sharpe under iid =
   0.024 × sqrt(50.4) / σ_resid ≈ 0.17 ÷ σ_resid. With realistic σ_resid ≈ 0.5,
   Sharpe ≈ 0.34 — borderline, even before costs. Not enough headroom for |t|≥3.5.

3. **Continuous weighting amplifies small-cap idiosyncratic noise.** Bottom-rank
   names (negative weight = short) include illiquid micro-caps with high noise.

4. **Burnt holdout ANY new test.** Even if continuous-weighting argument is
   theoretically motivated, it's another test on the same data → multiplicity
   inflated. No fresh information.

## Phase A sanity (cached, fast)

1. Cache hit verification: feature parquet should load in <30s.
2. Filtered subset rank-IC: same +0.0238 as v6a-revised expected.
3. Realized weight distribution at sample asof: verify sum(w) ≈ 0,
   sum(|w|) ≈ 1 by construction.

## Estimated effort

- Spec + zen review: 15-20 min
- Pre-reg JSON: 10 min
- v6c driver: clone v6a-revised, replace decile selection with continuous-weight
  return computation, ~30 min
- Phase B run: 2-3 min (cache HIT + retrain Lasso + new portfolio construction
  + Carhart regression)
- Verdict memo + ledger close: 20 min

**Total: ~1.5h.**
