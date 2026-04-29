# Momentum + low-vol combo — definitive synthesis (2026-04-29)

**RESEARCH ONLY.** First strategy in the AlphaLens R2000-like-universe
investigation that survives OOS realistic constraints. But subsample
stability check reveals **regime-dependent behavior**: works in
post-crisis recovery / mega-cap concentration regimes, fails in mid-cycle
disruption regimes.

## Setup

- Score: `z(mom_12_1m) − vol_weight × z(vol_60d)`
- Universe: per-subperiod PIT R2000-like
- Top-15 by score, weekly stride=5, holding=60d signal-only
- Per-rebalance ADV ≥ threshold, RealisticCostModel cost stress
- Comparison vs SPY benchmark (large-cap)

Script: `scripts/experiment_momentum_lowvol_combo.py`

## OOS sweep (2023-01-01 → 2026-04-22, N=154 weekly rebalances)

| vol_w | ADV ≥ $1M | ADV ≥ $5M | ADV ≥ $20M |
|---|---|---|---|
| **0.5** | Sh=−0.14 / excess=−30.6% | Sh=0.00 / −19.9% | Sh=−0.03 / −21.9% |
| **1.0** | Sh=0.24 / −0.5% | **Sh=0.55 / +18.9%** | **Sh=0.56 / +19.4%** |
| **2.0** | Sh=0.15 / −7.5% | **Sh=0.64 / +11.0%** | **Sh=0.63 / +9.8%** |

(Sharpe net of 5bp half-spread cost; excess net annualized vs SPY)

**Robust zone**: vol_w ∈ [1.0, 2.0] AND ADV ∈ [$5M, $20M]. 4 of 9 configs
deliver Sharpe net 0.55-0.64 with excess +9.8% to +19.4% net of 5bp cost.
At 15bp stress, net excess remains +7.7% to +17.0%.

## Subsample stability — the regime warning

Running vol_w=1.0 ADV=$5M on per-subperiod IS:

| Period | N | Sharpe gross | Excess vs SPY | α 4F | t-stat | R² |
|---|---:|---:|---:|---:|---:|---:|
| Full IS 2011-2022 | 604 | 0.43 | +7.9% | +26.7% | 1.65 | (0.0?) |
| **IS 2011-2016** | 302 | **0.77** | **+28.0%** | +40.1% | **1.82** | 0.038 |
| **IS 2017-2022** | 302 | **0.16** | **−11.3%** | +14.2% | **0.63** | 0.046 |
| OOS 2023-2026 | 154 | 0.75 | +21.5% | +48.1% | 1.45 | – |

**Three-way regime pattern: GOOD → BAD → GOOD.**

The subperiod 2017-2022 — which includes the 2018 mid-year drawdown, 2020
COVID crash + recovery, and 2022 bear market — produced **negative excess
return (−11.3%/y)**. Pooled IS 2011-2022 averages this against the strong
2011-2016 result, masking the regime fragility.

The OOS period 2023-2026 happens to share regime characteristics with
2011-2016 (post-crisis recovery, low-vol mean-reverting, large-cap
concentration). The fact that OOS works could reflect genuine factor
persistence OR a fortunate regime alignment.

## Per-subperiod diagnostic flag check

Per `docs/research/layer2d_subsample_3a.md` rule:
> If R²_full / min(R²_subperiod) < 0.5, suspect joint-window factor
> instability inflating pooled α.

Full-IS R² unobtained directly (not in summary log), but per-subperiod R²
of 0.038 and 0.046 are 4-10× higher than typical pooled-IS values for
this universe (Layer 2d had 0.005). The diagnostic likely fires.

The earlier Layer 2d work showed how this artifact looks: pooled-IS α
inflates 2× over subperiod mean. If the same operates here, the true
per-subperiod expected alpha is ≈ (40% + 14%) / 2 = 27%, not the 47%
pooled-IS figure. Even split, IS 2017-2022 gives α=14% t=0.63 — does
not pass nominal significance alone.

## Comparison vs other strategies tested today

| Strategy | OOS net Sharpe at $5M ADV | OOS net excess vs SPY | Verdict |
|---|---:|---:|---|
| Pure 12-1m momentum | −0.45 | −68.3% | CLOSED |
| 60d-drawdown contrarian + 5d bounce | −0.20 | −52.0% | CLOSED |
| Insider cluster (V0_count) | −0.45ish (extrapolated) | −52% | CLOSED |
| **mom+lowvol vol_w=1.0** | **+0.55** | **+18.9%** | **CANDIDATE** |
| **mom+lowvol vol_w=2.0** | **+0.64** | **+11.0%** | **CANDIDATE** |

mom+lowvol is the **first strategy in this entire investigation** to deliver
positive OOS net excess at retail-realistic ADV ≥ $5M with cost stress.

## What's real vs. what could be artifact

**Real** (high confidence):
- Low-vol filter genuinely hedges against momentum crashes (Asness QMJ 2018
  thesis empirically validated — pure momentum collapsed −68%, mom+lowvol
  delivered +19%)
- Stable across vol_w ∈ [1.0, 2.0] AND ADV ∈ [$5M, $20M] — not a
  single-magic-config artifact
- Stable to cost stress (5bp → 15bp drops Sharpe by ~0.2 but stays positive)

**Potentially artifact** (medium confidence):
- Pooled-IS α may be inflated by joint-window factor instability
- Per-subperiod IS_2017_2022 alpha doesn't pass significance
- OOS regime alignment with 2011-2016 may be coincidence

**Likely real but regime-dependent** (high confidence):
- Strategy works in low-vol mean-reversion regimes (2011-2016, 2023-2026)
- Fails in high-vol disruption regimes (2017-2022 COVID + Fed pivot)
- Same QMJ behavior as Asness-Frazzini-Pedersen 2018 documented in
  large-cap US equity universe

## Verdict

**MID — Phase 2 candidate, not Phase 3 / deploy.**

This is the most promising strategy found in the current AlphaLens
investigation, but it:
- Has documented regime risk (2017-2022 lost 11%/y)
- Has marginal IS significance per-subperiod (t=0.63 in 2017-2022)
- Has not been tested with multiple-testing correction (we ran ~15 strategy
  variants; Bonferroni n=15 → t_crit ≈ 2.93, current OOS t=1.45 fails)
- Has unknown sensitivity to alternative formation horizons / vol windows
- Has no regime overlay — would need one for actual deployment

## Pre-registered Phase 3 plan

Before considering capital deployment:

1. **Pre-register the spec**: vol_w ∈ {1.0, 2.0}, ADV ∈ {$5M, $20M},
   formation = 12-1m momentum + 60d realized vol, top-15 equal weight,
   weekly stride. Document the spec EXACTLY so future tests can verify
   no fitting after-the-fact.

2. **Forward-walking OOS** through 2026-04-29 onward (we are at the OOS
   boundary today). Run weekly forward starting 2026-Q3, log returns,
   compute trailing 6m / 12m metrics.

3. **Add regime filter** based on rolling realized SPY volatility or
   trailing CAPE — only deploy when regime resembles "low-vol mean
   reversion" (e.g., SPY 60d vol < 15%). Test if regime-filtered Sharpe
   improves vs unconditional.

4. **Multiple-testing correction**: at the strategy-search level
   (Harvey-Liu-Zhu 2016 framework), 15 candidate strategies run today
   means t_crit ≈ 2.93 for nominal p=0.05. The current best OOS t=1.45
   fails this — would need either (a) bigger effect size, (b) longer
   sample, or (c) pre-registration before next iteration.

5. **STR factor decomposition** of mom+lowvol α: does the apparent excess
   load on Jegadeesh 1990 STR factor? Already-built `factors/str_daily.csv`
   makes this trivial — just need to run.

6. **Bootstrap CI on excess return**: 95% block-bootstrap CI must exclude
   0 with the strategy-spec frozen.

If items 2-6 are satisfied AND a forward-walking OOS confirms positive
excess for 6+ months, this becomes a Phase 3 candidate. Otherwise it
joins the catalog as "interesting but not actionable".

## Updates 2026-04-29 — additional experiments (long-short, regime overlay, SPY-hedge, horizons)

After establishing mom+lowvol as the candidate, I ran **four orthogonal "fix the regime hole" experiments**. All four failed to improve on the BASE configuration:

### Long-short variant (top-15 long, bottom-15 short)

`scripts/experiment_longshort_mom_lowvol.py`:
- IS_2011_2016: LS Sharpe net 0.14 (vs long-only 0.55) — cost drag (both legs trade) eats it
- IS_2017_2022: **LS Sharpe net −0.84, mean −66%/y!!** (vs long-only −0.02). Bottom-15 (low mom + high vol) mean-reverted UP dramatically
- OOS: LS Sharpe net −0.15 (vs long-only 0.55)
- LS amplifies regime risk; β_MKT ≈ 0 confirms market-neutral but not in a useful direction

### Regime overlay (deploy when SPY 60d vol < threshold)

`scripts/experiment_regime_overlay.py`, 5 thresholds × 3 periods:
- IS_2011_2016 thr=0.15: cond Sharpe 0.80 (vs uncond 0.55), excess +29.6% (vs +28%) — improves
- IS_2017_2022: ALL thresholds WORSE than uncond. thr=0.15 cond excess −46.3% (vs uncond −11.3%)
- OOS_2023_2026: most thresholds slightly hurt (thr=0.15 excess +14.9% vs uncond +18.9%)
- 2017-2022 losses are NOT vol-regime concentrated; vol filter doesn't fix them

### SPY-hedged version (long top-15 minus SPY 100% notional)

Quick analysis script (Sharpe of excess vs Sharpe of long-only):
- All 12 (4 configs × 3 periods) tests: hedged Sharpe < long-only Sharpe
- OOS vol_w=1.0 $5M: hedged Sharpe net 0.11 (vs long-only 0.55)
- Strategy benefits from market beta; subtracting SPY removes the directional upside

### Formation horizon sweep

`scripts/experiment_constrained_momentum.py` extended inline test:
- 12-1m mom + 60d vol (BASE): OOS Sh net **0.55**, excess **+18.9%**
- 12-1m mom + 30d vol: OOS Sh net 0.45, excess +13.9% (slightly worse)
- 6-1m mom + 60d vol: OOS Sh net 0.18, excess −1.7% (worse)
- 6-1m mom + 30d vol: OOS Sh net 0.35, excess +10.6%
- 3m-21d mom + 30d vol: highest IS_2011_2016 t=2.31 but OOS collapses to Sh 0.14 — short-horizon overfit
- All variants fail 2017-2022; BASE is optimal

### Combined verdict (all 4 fix attempts failed)

**The 2017-2022 regime risk in mom+lowvol is structural.** It is NOT:
- A market-beta issue (long-short doesn't help)
- A volatility-regime issue (vol overlay doesn't help)
- A horizon-mismatch issue (no formation horizon fixes it)
- A benchmark-coupling issue (SPY-hedge doesn't help)

The 2017-2022 weakness reflects the strategy's exposure to specific regime characteristics: 2018 small-cap drawdown, 2020 COVID factor crash, 2022 mega-cap concentration leaving mid-cap mom+lowvol behind. None of these can be filtered with simple price-only signals.

To address it would require either:
- Different universe (mega-cap inclusive, e.g., S&P 500 — not currently cached)
- Multi-asset regime overlay (bonds, commodities, FX — far beyond current scope)
- Earnings/fundamentals signals (SimFin only has 2020+ — coverage gap)

**For now, mom+lowvol BASE config (vol_w ∈ [1.0, 2.0], ADV ≥ $5M, 12-1m mom + 60d vol) remains the best strategy found**, with the clearly documented regime risk that ~5/14 years would deliver negative excess. This is REGIME-FAIR-WEATHER alpha, not a robust all-weather strategy.

## Lessons (generalizable)

1. **Combining factors works where individual ones fail.** Pure
   momentum (−68% OOS) + low-vol filter combined = +19% OOS. The
   Asness-Frazzini-Pedersen QMJ thesis validated empirically on this
   universe.

2. **Subsample IS check is critical.** Pooled IS 2011-2022 hid a 39pp
   regime-dependent gap (28% in 2011-2016 vs −11% in 2017-2022).
   Anyone who used pooled IS would have over-estimated the strategy's
   robustness.

3. **OOS performance can be regime-coincident.** The OOS 2023-2026
   period happens to resemble 2011-2016 in regime characteristics.
   Strong OOS doesn't automatically mean strategy generalizes —
   forward-walking validation under different regime would be more
   informative.

4. **Parameter sensitivity is real but bounded.** vol_w=0.5 fails OOS
   while 1.0 and 2.0 work. Single-config-magic suspicion was wrong —
   the result is robust to a range, not a single point. But 0.5 doesn't
   fit; the strategy needs a non-trivial vol penalty to hedge effectively.
