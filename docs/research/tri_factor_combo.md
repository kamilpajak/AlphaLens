# Tri-factor combo — mom + lowvol + ROE (2026-04-29)

**RESEARCH ONLY.** Extension of `momentum_lowvol_synthesis.md`. SimFin
fundamentals cache covers 2020-Q2 onward. We construct a 3-factor scorer:

  score = z(mom_12_1m) − z(vol_60d) + roe_weight × z(roe_ttm)

and compare to mom+lowvol baseline on 2021-2022 IS / 2023-2026 OOS.

## Results

| Period | Strategy | roe_w | Sharpe gross | Sharpe net | excess gross | excess net | α 4F | t |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| **IS 2021-2022** | mom+lowvol BASE | – | 0.36 | 0.19 | +17.3% | +14.7% | +40.9% | 0.83 |
| IS 2021-2022 | mom+lowvol+ROE | 0.5 | −0.36 | −0.48 | −31.3% | −33.3% | −23.3% | −0.40 |
| IS 2021-2022 | mom+lowvol+ROE | 1.0 | −0.37 | −0.50 | −32.4% | −34.5% | −24.1% | −0.40 |
| IS 2021-2022 | mom+lowvol+ROE | 2.0 | −0.42 | −0.54 | −37.3% | −39.4% | −30.6% | −0.51 |
| **OOS 2023-2026** | mom+lowvol BASE | – | 0.75 | 0.55 | +21.5% | +18.9% | +48.1% | +1.45 |
| **OOS 2023-2026** | **mom+lowvol+ROE** | **0.5** | **1.26** | **1.04** | **+43.2%** | **+40.8%** | **+62.8%** | **+2.08** |
| OOS 2023-2026 | mom+lowvol+ROE | 1.0 | 1.14 | 0.93 | +35.3% | +33.1% | +51.5% | +1.72 |
| OOS 2023-2026 | mom+lowvol+ROE | 2.0 | 1.24 | 1.05 | +37.0% | +35.2% | +53.3% | +1.78 |

## Findings

### OOS strikingly improves with ROE

mom+lowvol+ROE delivers OOS:
- **Sharpe net 0.93–1.05** (vs base 0.55) — nearly 2× improvement
- **Excess net +33% to +41%/y** (vs base +19%/y) — more than double
- **α t = 1.72 to 2.08** (vs base 1.45) — first config passing nominal t > 2.0 in entire investigation
- **Lower turnover** (18–23% vs 26%) — quality stocks are stickier

All three roe_weights ∈ {0.5, 1.0, 2.0} perform similarly OOS — robust across weighting.

### IS 2021-2022 catastrophic for ROE

Same 3-factor combo gives −33% to −39% net excess in IS_2021-2022 (only IS
window we can construct with SimFin coverage). Quality factor headwinds:
- 2021: meme/junk rally (GME, AMC, ARK names) crushed quality
- 2022: bear market, profitability didn't matter — beta dominated

The pure mom+lowvol BASE was +14.7% in this period; ROE-enhanced version flipped
to −33%. ROE was a 47-point headwind in this 2-year window.

### Regime amplification

This makes the strategy MORE regime-dependent, not less:
- Pure mom+lowvol: 2011-2016 +28%, 2017-2022 −13%, 2023-2026 +19% (range 41pp)
- mom+lowvol+ROE: 2021-2022 −33%, 2023-2026 +41% (range 74pp)

ROE amplifies the "good times better, bad times worse" pattern.

## Caveats

1. **IS is only 2 years.** SimFin coverage starts 2020-Q2; we need 4 quarters
   of TTM Net Income, so the earliest valid asof is ~2021. Two-year IS
   can't credibly characterize regime stability.

2. **OOS alignment with quality-favorable regime.** 2023-2026 had mega-cap
   tech quality rally (Mag 7 with high ROE outperformed). The ROE factor
   was a strong tailwind. Other periods (2017, 2021 junk rally) would have
   been opposite.

3. **Multiple-testing not corrected.** ~25 strategy variants tested in this
   investigation. Bonferroni n=25 → t_crit ≈ 3.05. Current best t=2.08
   does NOT pass this threshold.

4. **R² = 0.04** in the 4-factor regression — Carhart explains essentially
   nothing. The 63% α is residual; no factor specification absorbs it.
   Same low-R² × high-α pattern that flagged Layer 2d as artifact-suspect.

5. **No subsample stability check possible** within IS — only 101 weekly
   rebalances, splitting further would be too small. Cannot test pooled-IS
   factor instability hypothesis here.

## Honest verdict

This is the **most striking single-window OOS result** found in the entire
AlphaLens investigation:
- Sharpe net > 1.0
- α 4F > 60%/y
- α t > 2.0
- Stable across roe_weight ∈ [0.5, 2.0]
- Lower turnover than base

But it has the LEAST credible IS support (only 2-year window, catastrophically
negative). Two interpretations:

**Optimistic**: Quality + low-vol + momentum is a fundamentally well-grounded
combination (Asness-Frazzini-Pedersen QMJ + Jegadeesh momentum). The OOS result
captures real factor performance in the post-2022 quality-favorable regime. Phase
3 forward-walking validation could confirm.

**Pessimistic**: 2-year IS that goes opposite direction is a major red flag.
The OOS result is regime-coincident with quality factor tailwinds (Mag 7
concentration). When the regime turns (which it did in 2021), the strategy
catastrophically underperforms. Multiple-testing suspect.

**Realistic position**: This IS the best price+fundamentals strategy
discoverable from current AlphaLens data. It is also the strategy with the
WORST documented regime risk (74pp range across 2 documented sub-windows).
Any deployment would require:
- Extending SimFin coverage backward (manual SEC EDGAR ingestion or data
  vendor purchase) to characterize IS over 2010s
- Multiple-testing-corrected hypothesis testing
- Forward-walking OOS through 2026-Q3+ before any capital commitment
- Regime-conditioning on quality factor performance (positive momentum-on-quality
  or VIX-conditional gating)

## Where this leaves the search

Two strategy candidates emerged from the day's search:

| Spec | Best OOS Sharpe net | OOS excess net | OOS α t | IS support |
|---|---:|---:|---:|---|
| mom+lowvol (12-1m + 60d, vol_w=1.0, $5M) | 0.55 | +18.9% | 1.45 | 12 years (2011-2022) — regime hole 2017-2022 |
| **mom+lowvol+ROE (roe_w=0.5)** | **1.04** | **+40.8%** | **2.08** | 2 years (2021-2022) — catastrophic |

The price-only candidate has WEAKER OOS but LONGER IS. The fundamentals-augmented
candidate has STRONGER OOS but SHORTER IS that goes opposite direction.

Both candidates have unaddressed regime risk and fail Bonferroni multi-test
correction. Neither is a "deploy" candidate today. Both are credible Phase 2
candidates needing Phase 3 forward-walking validation before any capital
commitment.

## Status

- Layer 2d (insider clusters): CLOSED, definitive postmortem
- Pure 60d-drawdown contrarian: CLOSED for retail
- Pure 12-1m momentum: CLOSED for retail
- mom+lowvol BASE: Phase 2 candidate, regime-fair-weather
- **mom+lowvol+ROE**: New Phase 2 candidate, stronger OOS but shorter IS

## Pre-registered Phase 3 plan if pursuing the tri-factor

1. Extend SimFin coverage backward to 2010+ (Polygon EDGAR ingestion or paid
   data vendor) to enable 10+ year IS validation
2. Pre-register exact spec: roe_w=0.5, vol_w=1.0, ADV ≥ $5M, top-15, weekly
   stride, 12-1m mom + 60d vol, TTM ROE
3. Forward-walking OOS from 2026-Q3 onward; log returns, compute trailing
   metrics monthly
4. Multiple-testing correction: pre-register the specific combination tested
   (avoid future strategy-search inflation)
5. Regime-conditional gating on quality factor performance
6. Bootstrap CI on net excess return; require 95% CI to exclude 0 over IS
   AND OOS in the long-horizon validation
