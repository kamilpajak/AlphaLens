# Strategy search 2026-04-29 — final synthesis (saturation reached)

**RESEARCH ONLY.** After 2 days × ~30 strategy variants, the experimental
search on the AlphaLens R2000-like PIT universe has reached saturation
for retail-feasible factor strategies. This document is the canonical
end-of-search report.

## Strategies tested

### Single-factor (all CLOSED for retail capital deployment)

| Strategy | OOS Sharpe net | OOS excess vs SPY | Verdict |
|---|---:|---:|---|
| Insider Form 4 cluster-buy (Layer 2d) | ~−0.45 | −52% | Behavioral marker for contrarian set, not info edge |
| 60d-drawdown contrarian + 5d bounce | −0.20 | −52% (at $5M ADV) | Tail-rebound artifact at zero-ADV |
| 12-1m Jegadeesh-Titman momentum | −0.45 | −68% to −89% | Famous 2023-2026 momentum crash |

### Two-factor

| Strategy | OOS Sharpe net | OOS excess net | t (Carhart) | t (FF5+UMD) | Status |
|---|---:|---:|---:|---:|---|
| **mom+lowvol** vol_w=1.0 ADV ≥ $5M | **0.55** | **+18.9%** | 1.45 | 1.25 | Best price-only |
| mom+lowvol vol_w=2.0 ADV ≥ $5M | 0.64 | +11.0% | 1.51 | – | Robust to weighting |
| mom+lowvol min-var weighting | 0.70 | +29.0% | 1.54 | – | Marginal Sharpe lift |

### Three-factor (limited IS due to SimFin coverage)

| Strategy | OOS Sharpe net | OOS excess net | t (Carhart) | t (FF5+UMD) | IS support |
|---|---:|---:|---:|---:|---|
| **mom+lowvol+ROE** roe_w=0.5 | **1.04** | **+40.8%** | **2.08** | 1.84 | 2y catastrophic (-33%) |
| mom+lowvol+ROE roe_w=2.0 | 1.05 | +35.2% | 1.78 | – | 2y catastrophic (-39%) |

### Failed orthogonal architecture changes

| Approach | OOS impact | Verdict |
|---|---|---|
| Long-short (top-15 minus bottom-15) | Sharpe net −0.15 OOS, IS_2017_2022 mean −66%/y | Bottom-15 mean-reverts in regime shifts; LS amplifies regime risk |
| SPY-hedged (long − SPY 100% notional) | Hedged Sharpe < long-only across all 12 configs | Strategy benefits from market beta; hedging removes upside |
| Vol regime overlay (SPY 60d vol < threshold) | 5 thresholds tested; IS_2017_2022 worse at all of them | Losses NOT vol-regime-concentrated |
| Breadth regime overlay (% R2000 above 50d MA) | 5 thresholds tested; opportunity cost > regime benefit | Filtering reduces in-market time too much |
| Formation horizon variants (3m, 6m, 12m × 21d, 30d, 60d) | 12-1m + 60d (BASE) is optimal; shorter horizons → higher turnover, lower net Sharpe | Short-horizon variants overfit (3m IS t=2.31 → OOS Sh 0.14) |

## Two emerging candidates

### Candidate 1: mom+lowvol BASE (price-only)
- OOS net Sharpe 0.55, excess +18.9% vs SPY net 5bp
- IS support 12 years (2011-2022) but with documented regime hole 2017-2022 (-11%/y)
- α 4F t=1.45 (does not pass nominal 2.0)
- α F5+UMD t=1.25 (Δα=−5.4pp; F5 doesn't kill the residual)
- R² ≈ 0.01-0.02 across periods
- Turnover 25%, low-cost regime (5bp fits)

### Candidate 2: mom+lowvol+ROE tri-factor (with fundamentals)
- OOS net Sharpe 1.04, excess +40.8% vs SPY net 5bp
- α 4F t=2.08 (passes nominal 2.0 — first in entire investigation)
- α F5+UMD t=1.84 (Δα=−6.3pp)
- IS support only 2 years (2021-2022) — catastrophic in window (-33% to -39% net)
- R² still ~0.04 (96% return variance unexplained)
- Quality regime mismatch: 2021-2022 IS = late-cycle junk rally → quality headwind; 2023-2026 OOS = mega-cap quality rally → quality tailwind

## Key findings

### 1. Regime-dependence is structural, not gatable

Tested 4 orthogonal regime gating approaches; none fixed mom+lowvol's 2017-2022 hole:
- SPY trailing volatility (5 thresholds)
- R2000 breadth indicator (5 thresholds)
- SPY-hedged spread (would only neutralize beta, not regime exposure)
- Long-short cross-sectional (amplifies regime risk via toxic short leg)

Per Perplexity peer review: *"the regime governing performance is not observable from common market indicators."* This is consistent with academic literature on factor cyclicality (Asness-Israel 2024, AQR low-vol cycles 2014).

### 2. FF5+UMD does NOT explain the apparent α

R² goes from 0.04 (Carhart-4F) to 0.05 (FF5+UMD) across all candidates — minimal change. Adding RMW (profitability) and CMA (investment) factors does not absorb the residual α. This rules out the simplest "factor exposure mis-attributed as alpha" hypothesis.

But it does NOT prove the strategy captures novel signal — could still be:
- Higher-order factor interactions
- Small-cap-specific factors (high-frequency reversals, bid-ask bounce)
- Regime-conditional luck on a specific OOS window

### 3. Multiple-testing burden is severe

~30 distinct strategy variants tested across 2 days. Bonferroni n=30 → t_crit ≈ 3.10 for nominal p=0.05. Best result: tri-factor OOS t=2.08 (Carhart) / 1.84 (FF5+UMD). **Neither candidate passes formal multi-test correction.**

Furthermore: tri-factor was developed POST-HOC after observing 2017-2022 weakness in mom+lowvol. Adding ROE was opportunistic, not pre-registered. Per Harvey-Liu-Zhu 2016 framework, this is sequential adaptive search → even higher t_crit required.

### 4. R²-instability flag (Layer 2d diagnostic) does not fire here

Per `docs/research/diagnostic_flag_retrospective.md`: pooled-IS R²/min(subperiod R²) < 0.5 flags pooling artifact. For mom+lowvol pooled IS R²=0.046, subperiod 0.038 (2011-2016) and 0.046 (2017-2022) → ratio 0.046/0.038 = 1.21. Does NOT fire. The IS regression is not factor-loading-instability-contaminated like Layer 2d was. This is a positive sign — at least the methodological artifact present in Layer 2d's headline α is not contaminating mom+lowvol's headline.

## What WAS NOT tested (recommended for Phase 3)

Per Perplexity peer review:

1. **EDGAR companyfacts backfill** — extend SimFin coverage backward to 2010+ via direct SEC parsing. Would enable proper 5-7 year IS for tri-factor (current 2y is below academic minimum). HIGHEST PRIORITY for tri-factor validation.

2. **Credit spread term structure** as regime gate — CDS slope or corporate bond curve. Distinct from realized vol; may capture regime info that vol-based gates missed.

3. **VIX term structure** — curve slope (long-term vol vs short-term vol) as forward-looking regime indicator.

4. **Valuation dispersion within universe** — CAPE scatter or P/E quintile spread as regime gate.

5. **PCA-based statistical arbitrage** — orthogonalize systematic exposures, isolate idiosyncratic mean reversion.

6. **Ensemble of multiple weak signals** — weighted blend of mom+lowvol + insider strength + reversals + quality. May reduce reliance on single-factor regime exposure.

7. **Risk-parity weighting** (similar to min-var but with explicit risk targets per stock).

## Honest verdict

**The AlphaLens R2000-like PIT universe in 2011-2026 is near-saturated for retail-feasible single-factor and two-factor strategies.** Both candidate strategies have:
- Marginal IS support (mom+lowvol regime-fragile, tri-factor 2-year only)
- Marginal OOS significance (t<2 with FF5+UMD)
- Documented regime risk (2017-2022 hole, quality factor cyclicality)
- Low R² (96% unexplained — could be novel signal or higher-order specification)
- Multi-test contamination (~30 variants tested)

**Neither candidate is deploy-ready.** Both are Phase 2 candidates. To advance:

**For mom+lowvol** (faster path):
1. Document strategy with regime-risk disclosure
2. Forward-walk OOS from 2026-Q3 onward with pre-registered pass-gates (Sharpe ≥ 0.5, excess ≥ 10%/y net)
3. Monitor regime indicators (credit spreads, breadth) for additional context
4. Continue at low capital allocation with explicit regime-conditional sizing

**For tri-factor** (better OOS, requires more validation):
1. EDGAR backfill TTM ROE to 2015+ (engineering sprint)
2. Re-validate on extended IS across multiple quality cycles
3. Test FF5 redundancy explicitly (β_RMW only -0.05 today suggests our ROE is differently positioned vs F5 RMW)
4. Forward-walk OOS with pre-registered pass-gates (Sharpe ≥ 0.9, excess ≥ 30%/y net)

## Closing note

This concludes the 2026-04-28 / 2026-04-29 strategy search. The repositioning of AlphaLens (2026-04-25) as research infrastructure rather than active alpha generator stands — mom+lowvol and tri-factor are interesting research candidates but neither warrants capital deployment in current state. The investigation has produced:

- 5 closed-layer postmortems (Layer 2d definitive + 3 single-factor closures + LS/hedge architecture closures)
- 2 emerging Phase 2 candidates with documented limitations
- Generalizable methodology improvements (low-R²+high-α diagnostic flag, R²-instability check, FF5+UMD validation framework)
- Literature priors mapping (QMJ regime cyclicality, momentum crashes, quality factor cycles)
- Reusable infrastructure (`build_str_factor.py`, multiple backtest scripts, parquet-cached signals)

Next decisive step IF strategy deployment is the goal: **EDGAR backfill** to extend tri-factor IS validation. Without this, both candidates remain research-only.
