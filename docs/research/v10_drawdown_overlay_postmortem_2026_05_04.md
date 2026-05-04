# v10 drawdown-control overlay — postmortem (2026-05-04)

**Pre-reg id:** `v10_drawdown_overlay_on_v9D_options_2026_05_04`
**Class:** `risk_management_overlay_2026_04_30` (overlay-class test #2)
**Verdict:** FAIL (3/6 gates miss). Class now 0/2 PASS.

## Headline numbers (multi-phase pooled, 501 obs, Ledoit-Wolf bootstrap)

| Metric | Value |
|---|---|
| Sharpe(overlay) | +1.489 |
| Sharpe(base) | +1.513 |
| Sharpe diff | **−0.024** |
| Bootstrap t (paired, block=21d, n=10000) | **−1.93** |
| 1-sided p (HA: overlay > base) | 0.985 |
| 95% CI for Sharpe diff | [−0.049, −0.001] |
| Mean per-phase MDD ratio | 0.87 (target ≤ 0.7) |
| Mean per-phase Δ Sharpe | −0.031 (target ≥ +0.30) |
| Mean per-phase base αt | **+2.29** (matches v9D recorded +2.29 to 0.001) |
| Phase dispersion of Δ Sharpe | 0.055 absolute |

CI excludes 0 on the negative side: the overlay **reliably hurts** Sharpe, by 0.5–5 bp annualised.

## What the test actually told us

### 1. v9D base reproduces perfectly across the multi-phase grid

Mean base αt across 5 phases = **+2.29**, identical to v9D's recorded single-phase value. This is a strong corroboration that the v9D scorer is *not* a single-phase fluke, while *also* confirming v9D's sub-Bonferroni position (no phase exceeded αt = +2.92, none below +1.86, pooled is +2.29). The +2.2-2.3 ceiling on options-implied retail signal is now a 5-phase-robust observation.

### 2. The overlay does what it's designed to do — when it can

Per-phase MDD reduction split:
- Phase 0 (single-period flash MDD = −32%): overlay ratio 1.000 — **no reduction**.
- Phase 1 (no significant drawdown, MDD = −5%): overlay ratio 1.000 — **nothing to do**.
- Phase 2 (gradual MDD = −11%): overlay ratio 0.703 — **30% reduction**.
- Phase 3 (gradual MDD = −18%): overlay ratio 0.700 — **30% reduction**.
- Phase 4 (mild MDD = −6%): overlay ratio 0.923 — small reduction.

The overlay is **structurally well-defined**: when drawdowns are gradual and exceed the trigger threshold over multiple stride periods, it correctly cuts exposure and the equity curve gets shallower. Phases 2 and 3 are existence proofs.

### 3. But cost-of-de-levering ≈ benefit — every time

Even in phases 2 and 3 where the overlay successfully truncated MDD by 30%, Δ Sharpe was **negative** (−0.058 and −0.040). The de-lever-and-relever transaction cost (charged via `dynamic_cost_drag` on every weight change) plus the missed-recovery-leg drag matched or exceeded the volatility-reduction benefit.

In phases 0 and 1 the overlay had nothing to add (flash crash too fast / no drawdown). In phase 4 the drawdown was too mild for the overlay to materially trigger. **In every regime sampled, the overlay was neutral-to-slightly-negative.**

### 4. The dispersion is nearly zero — the FAIL is structural, not noisy

Phase-dispersion of Δ Sharpe = **0.055 absolute** (G6 PASSes by an order of magnitude). The overlay underperforms *consistently*, not noisily. This is the cleanest possible FAIL diagnosis: this exact step-function design on this exact base, on the burnt 2024-04-30 → 2026-04-30 window, produces a stable −0.02 to −0.06 Sharpe drag.

### 5. The single-period-flash failure mode replicates

In phase 0, base MDD = −32.31%, overlay MDD = −32.31%. The same week-long −X% return appears in both series unmodified — overlay's `scale[t]` uses `returns[<t]`, so when the bad week IS week t, the overlay can't possibly de-lever in time. This is a fundamental causality limit, not a parameter choice. Smoke on COVID 2020 surfaced the same pattern locally.

## Why the structural FAIL was predictable in hindsight

Drawdown-control overlays are *reactive*: they observe a peak-to-trough decline and cut exposure on the next period. Two things must hold for them to add Sharpe:

1. The drawdown must unfold over multiple rebalance periods (not one).
2. The recovery must not be V-shaped (otherwise being de-levered into the bottom misses the rebound).

The 2024-04-30 → 2026-04-30 burnt holdout had a few corrections (Aug 2024, Apr 2025) but they were either single-period flashes (kill condition 1) or quickly-V-shaped (kill condition 2 / phase 1 had MDD = −5% at all-time-high mode where overlay never armed). The base portfolio's options-implied tilt also exhibits low low-frequency drift — most of the P&L variance is at the rebalance frequency itself, leaving little for vol-cycle overlays to harvest.

This was foreshadowed by:
- **Vol-target overlay v1 FAIL** on a different (no-alpha) base (paradigm failure #10) — same family of overlay, same regime behaviour.
- **COVID smoke result** during this v10 build (Δ Sharpe = −0.49 on 2020-Q1 V-shape) — design canary that flashed red BEFORE the holdout audit.

The smoke canary was correctly read at the time as "informative but not falsifying" because Phase 2 smoke gates only check structural correctness (overlay triggers, weight bounds, base αt sanity), all of which passed. The pre-reg's hard PASS bar lives in the 6-gate holdout audit and that's now in. The lesson is **not** "we should have aborted on smoke" — it's "in regimes without multi-period gradual drawdowns, this overlay family can't generate Sharpe-improvement, ever."

## Bonferroni accounting after v10

| Counter | Before v10 | After v10 |
|---|---|---|
| Program-level alpha-class n | 24 | **24** (overlay metric ≠ Carhart αt; no increment) |
| Overlay-class n (`risk_management_overlay_2026_04_30`) | 1 (vol-target FAIL) | **2** (vol-target FAIL, drawdown-control FAIL) |
| Naive intra-overlay-class threshold for next | n/a | t ≥ 2.39 |

A third overlay test on the same v9D base on this holdout would carry an escalated threshold and a stronger meta-multiplicity prior given the within-class 0/2 PASS.

## Implications for next experiments

The pre-reg's "no auto-pivot within same session" clause means **no v11 within this session**. But the postmortem can scope what the next overlay or non-overlay test could ask:

### What this FAIL closes off

- **Drawdown-control with step-function on equity curve, applied to v9D options base, on 2024-2026 burnt holdout** — closed. No reasonable parameter retune (different threshold, different recovery band, different lookback) saves this; the dispersion across 5 phases is too tight to suggest a tuning fix is hiding.
- **All overlays whose mechanism is `react to past portfolio returns`** are now suspect for v9D-style options portfolios. Vol-target uses past vol; drawdown-control uses past drawdown. Both classes failed for related reasons.

### What this FAIL leaves open

- **Predictive overlays** (use exogenous regime features — VIX term structure, credit spread direction, factor momentum) — the pre-reg explicitly excluded these because of overfit risk, but the structural failure mode here is causality, not overfitting. A legitimate predictive overlay isn't *guaranteed* to fail the same way. (Pre-reg discipline + adversarial review still required to avoid HARKing.)
- **Within-screen filters** (use base-scorer behaviour to gate which tickers enter — e.g. drop tickers showing accelerating drawdowns even before the portfolio level shows it) — Layer 2 selection, not Layer 4 sizing. Different layer, different multiplicity.
- **L5 → L2 feedback** (Carhart-residualized scorer; was candidate E pre-v10) — orthogonal architecture; not closed by overlay-class failures.
- **Fresh signal class on a DIFFERENT holdout window** — the only way to fully escape burnt-holdout multiplicity. Earliest fresh window with ≥1 year of data: ~2027-05.

### What this FAIL strengthens

- **The +2.2-2.3 honest options-implied αt** as the project's most replicated finding. Across 4 v7-v9D variants + 5-phase audit on v10 base = ~9 evaluations, all converging to the same number. Sub-Bonferroni but real.
- **Pre-reg + multi-phase + Ledoit-Wolf bootstrap** as a methodology bundle that produces clean PASS/FAIL boundaries (not "marginal but interesting"). v10 was always going to be PASS or FAIL in <1h of compute; the design pre-committed to a sharp answer.

## Operational notes

- Runpod cost: ~52 min × $0.64/h ≈ **$0.55** (RTX PRO 4500 Blackwell, SECURE EU-RO-1, only available SKU in stock at run time).
- Per-phase wall: ~9.5 min, dominated by feature build (108k rows × 9 cols × ~530 s).
- Pod terminated immediately on completion; volume `xymjkwj580` retained for future runs.
- Five phase JSONs + verdict bundle persisted under `docs/research/v10_drawdown_overlay/`.
