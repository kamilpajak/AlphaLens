# v10 drawdown-control overlay — multi-phase verdict: **FAIL**

Pre-reg id: `v10_drawdown_overlay_on_v9D_options_2026_05_04`
Phases: 5 | Pooled observations: 501 | Bootstrap: block=21d, n=10000

## Per-phase results

| Phase | n | Base αt | Base Sh net | Base MDD | Overlay αt | Overlay Sh | Overlay MDD | MDD ratio | Δ Sharpe | bootstrap t | w mean |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 101 | +2.03 | 1.86 | -32.31% | +2.00 | 1.83 | -32.31% | 1.000 | -0.025 | -0.48 | 0.87 |
| 1 | 100 | +2.47 | 1.57 | -5.16% | +2.47 | 1.56 | -5.16% | 1.000 | -0.003 | -0.50 | 0.99 |
| 2 | 100 | +1.86 | 1.45 | -11.13% | +1.80 | 1.40 | -7.83% | 0.703 | -0.058 | -1.22 | 0.91 |
| 3 | 100 | +2.18 | 1.87 | -17.83% | +2.13 | 1.83 | -12.48% | 0.700 | -0.040 | -0.46 | 0.79 |
| 4 | 100 | +2.92 | 2.03 | -6.49% | +2.91 | 2.00 | -5.99% | 0.923 | -0.027 | -1.34 | 0.96 |

## Pooled bootstrap (cross-phase Sharpe-diff)

- Sharpe(overlay) = +1.489
- Sharpe(base)    = +1.513
- Sharpe diff     = -0.024
- bootstrap t     = -1.929
- p (1-sided)     = 0.9849
- 95% CI          = [-0.049, -0.001]

## Gates

| Gate | Rule | Observed | Verdict |
|---|---|---|---|
| G1 | pooled t≥2.5 AND p<0.01 | t=-1.93, p=0.985 | ❌ FAIL |
| G2 | mean Δ Sharpe ≥ 0.3 | -0.031 | ❌ FAIL |
| G3 | mean MDD ratio ≤ 0.7 | 0.865 | ❌ FAIL |
| G4 | |mean αt − 2.29| ≤ 0.5 | αt=+2.29, diff=0.001 | ✅ PASS |
| G5 | weight ∈ [0,1] | min=0.000, max=1.000 | ✅ PASS |
| G6 | Δ Sharpe range ≤ 0.5 | 0.055 | ✅ PASS |

## Verdict: **FAIL**

Failed gates: G1, G2, G3
