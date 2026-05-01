# multi_source_global_lasso Phase B + C — findings (2026-04-30)

**Pre-registration:** [`multi_source_global_lasso_2026_04_30`](preregistration/params_multi_source_global_lasso_2026_04_30.json)
**Class:** `multi_source_two_stage_search_2026_04_30` (now 2/2 FAIL)
**Status:** **FAIL** — phase 0 looked PASS by headline but model had ZERO nonzero coefs; multi-phase confirmed null result. Ledger entry completed 2026-04-30. **12th paradigm failure.**

## TL;DR

v2 was the single-variable ablation of v1's regime-conditional architecture: same 21
features, same 5d target, same λ procedure, same cost model — only Stage 1
removed and Lasso fitted globally. Result decisively isolates the architecture
variable: **removing Stage 1 made things slightly worse**, not better. v1's
marginal edge came from per-regime fitting allowing a small Q1_calm signal to
surface; globally, Lasso CV chose to zero everything.

| | v1 (4-regime) | v2 (global) |
|---|---:|---:|
| Phase 0 αt | +2.01 | +2.03 |
| **Mean phase αt (5)** | **+0.65** | **+0.55** |
| Mean excess_net ann | 0.0% | −0.2% |
| Dispersion (excess) | 4.9pp | 5.6pp |
| Phase-robust verdict | FAIL | **FAIL** |

## Architecture variable now settled

After two single-variable tests in this class:

- **Per-regime (v1):** 1 of 4 Lassos fitted real signal (Q1_calm, 15/21 nonzero); 3 of 4 zeroed.
- **Global (v2):** ALL 21 coefs zeroed.

The Lasso CV-MSE objective at this 5d horizon + 21 features + retail-cost regime
declares "no signal worth keeping" everywhere except a low-VIX subsample. The
4-regime split was a partition trick that surfaced that subsample; pooling kills it.

**Implication:** the bottleneck is NOT architecture. Next ablation variable must
be features, target, or model class.

## Phase 0 PASS-by-headline was a structural artifact

The single most important diagnostic from v2:

```
global fit: n_train=310517, λ chosen=0.002566, nonzero coefs=0/21
```

With every coefficient zero, `model.predict(X) = intercept_` for all rows.
`predict_scores_global` returns a Series of identical values. The downstream
`sort_values(ascending=False).head(30)` is a stable sort over identical values
→ returns the first 30 rows by dataframe order = first 30 alphabetically-and-
ADV-filtered tickers in that asof.

The reported phase 0 `Sh_net=+1.31, t=+2.03, α_4F=77.4%` is therefore **not
alpha from features** — it's:
1. Equal-weight 30 alphabetically-first liquid tickers (small-cap tilt vs
   cap-weighted SPY)
2. SMB / Mom factor loadings in the Carhart regression
3. The HOL window 2024-04-30 → 2026-04-30 happened to favor that tilt

Phase 1's reversal (t=−0.71) confirms the structural alpha is itself
phase-fragile: at phase=1 (1-day stride offset) the alphabetical-first-30
composition shifts (different ADV-filter outcomes, different leadership) and
the residual factor tilt swings.

**Methodological note added to memory** (`feedback_zero_coef_lasso_diagnostic.md`):
when Lasso CV zeros everything, the headline metrics need to be flagged
explicitly as "structurally degenerate" regardless of whether they clear the
pre-reg gates. Phase B reported MID by gate, but the underlying mechanism is
null.

## Phase B headline (phase=0, single-OOS)

```
HOLDOUT 2024-2026 | ADV≥$5M cost=10bps | n=99 topN=30.0 turn=13.2% |
Sh gross=1.49 net=1.31 | excess gross=8.5% net=6.5% | α 4F=77.4% t=2.03
verdict: MID (refine and re-pre-register before deploy) | t=2.03 Sh_net=1.31 α_4F=77.4% MaxDD=-10.1%
```

## Phase C — multi-phase audit (5 offsets on 2014-2026)

| phase_offset | α t-stat | Sharpe net | Sharpe gross | excess net ann | turnover |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | +2.03 | +1.31 | +1.49 | +6.5% | 13.2% |
| 1 | −0.71 | −0.27 | −0.05 | −7.1% | 14.5% |
| 2 | +1.10 | +0.59 | +0.76 | −4.0% | 13.6% |
| 3 | +0.12 | −0.02 | +0.15 | +4.2% | 13.4% |
| 4 | +0.19 | −0.02 | +0.15 | −0.5% | 12.4% |
| **mean** | **+0.55** | +0.32 | +0.50 | **−0.2%** | 13.4% |
| **std**  | 1.05 | 0.55 | 0.55 | 5.6pp | — |

`multi_phase.robust_verdict` = FAIL: mean αt = 0.55 < 1.0.
Pre-reg pass-rule (every-phase t≥1.5) broken by 4 of 5 phases.

Audit JSON: `docs/research/multi_source_global_lasso_multi_phase_audit.json`

## Comparison across 12 paradigm failures (phase-robust era)

| Strategy | mean αt | mean excess_net | dispersion |
| --- | ---: | ---: | ---: |
| mom+lowvol_combo | +0.49 | −5.7% | 44.5pp |
| quality+momentum | +0.38 | +10.3% | 69.0pp |
| vol_target_overlay | +0.49 | −7.0% | 40.3pp |
| multi_source_two_stage (v1) | +0.65 | 0.0% | 4.9pp |
| **multi_source_global_lasso (v2)** | **+0.55** | **−0.2%** | **5.6pp** |

v1+v2 stand out from the prior three by an order-of-magnitude tighter
dispersion. The ML pipeline (Lasso L1 + nested CV + multi-source features)
produces *stably weak* fail rather than *lucky-single-phase* fail. Methodologically
more honest signal: there is *some* edge in the structural tilt vs SPY, but
not enough to clear cost + Bonferroni.

## Next-experiment design — adversarial review by zen + perplexity

### Why Proposal A (my original — per-regime + 20d + stride=20) was rejected

zen (Gemini-3 Pro) flagged a fatal flaw: stride=20 over the 2024-2026 holdout
yields ~25 independent observations. Bonferroni n=3 threshold |t|≥2.39 with
n=25 needs an effect size practically unheard-of in retail equities. Empirically
confirmed by Perplexity: underpowered studies (<30% power) overestimate
effects by 78%+ when they do clear significance ([PMC 12856849]).

Additionally, going back to per-regime architecture from v2 violates the
single-variable ablation rule — v3 would change 2 variables vs v2.

### Approved v3 design (zen + Perplexity concurrence)

`multi_source_global_lasso_20d_2026_04_30`:

- **Single-variable ablation from v2** (only target horizon changes, 5d → 20d)
- **Architecture:** Global Lasso (same as v2)
- **Target:** 20d-forward excess return (target.py already supports holding param)
- **Holding:** 20d
- **Stride:** stays at 5d (overlapping 4-tranche portfolios — preserves n=99 holdout obs)
- **HAC maxlags:** explicit override to 5 to handle MA(4) overlap autocorrelation
- **Sharpe annualization:** Lo (2002) variance-ratio-adjusted via existing `sharpe_autocorr_adjusted`
- **Bonferroni:** n=3 in class → t≥2.39

### Why this design over alternatives

| Variable | Test | Rationale |
| --- | --- | --- |
| Target horizon (B) | First | Empirical: v2 zero-coef finding implicates SNR at 5d. Theoretical: most published anomalies are 1-12 month, not 1-week. Cost dynamics: 20d × stride=5 overlapping ≈ ¼ the cost drag of 5d × stride=5. |
| Regularization (C, ElasticNet) | After B | If 20d also zeroes → confirms features inadequate, not just horizon. ElasticNet is the next isolation. |
| Model class (D, GBM) | Last | Highest tuning surface, highest overfit risk. Only after horizon + regularization eliminated. |
| New features (A, alt-data, news) | Future class | Requires new signal class + feature engineering investment. Defer until simple tests exhaust. |

## Pre-registration ledger

Class `multi_source_two_stage_search_2026_04_30` now 2/2 FAIL. Any further
hypothesis in this class pays Bonferroni n=3 → 2.39 t-stat threshold. v3
already drafted under this discipline.

```bash
.venv/bin/alphalens preregister complete multi_source_global_lasso_2026_04_30 \
  --verdict FAIL --mean-alpha-t 0.55 --mean-excess-net -0.002 \
  --audit-path docs/research/multi_source_global_lasso_multi_phase_audit.json
```

## What was learned (durable artifacts)

1. **Lasso 0/N coef diagnostic** (`feedback_zero_coef_lasso_diagnostic.md`): mandatory check before believing any headline alpha when CV zeroes everything.
2. **Architecture isolation in this class** is settled: per-regime ≈ global within ±0.1 mean αt, both ≈ 0.5-0.6 mean.
3. **Adversarial review value**: zen + Perplexity caught the n=25 power flaw before it consumed compute.
4. **Single-variable ablation discipline** preserved across v1 (vs prior class) and v2 (vs v1) and v3-design (vs v2).
