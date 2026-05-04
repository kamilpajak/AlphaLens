# v8 Literature-Direct Design (Xing 2010 Replication)

**Date:** 2026-05-03
**Pre-reg:** `docs/research/preregistration/params_v8_literature_direct_options_implied_2026_05_03.json`
**Class:** `options_implied_search_2026_05_02` (same null space as v7)
**Status:** PRE-REG LOCKED 2026-05-03

## Why this exists

v7 FAIL'd a 5-phase multi-phase audit on 2026-05-02:

- Mean αt = +2.60 across 5 phases (failed program-Bonferroni n=14, |t|≥2.86)
- Phase dispersion 67.2pp on `excess_net_ann` (failed <50pp gate)
- Sign-flip on `ivx30` and `ivp30` across **all 5 phases** — Lasso fitted POSITIVE coefficients on vol-level features, contradicting Xing-Zhang-Zhao 2010 / Bali-Hovakimian 2009 NEGATIVE-sign prior committed ex-ante in pre-reg
- L/S decile spread αt = -2.78 — Xing direction IS empirically present in 2024-2026 holdout, but the train-fitted Lasso predicted opposite

**Strategic narrative.** Train 2018-04-30 → 2024-04-29 contained COVID-2020 + meme-2021 + AI-2023 bullish-vol periods where high-IV stocks recovered and outperformed (Lasso learned positive coef). Holdout 2024-04-30 → 2026-04-30 is a low-vol-following-stress regime where Xing 2010 reasserts. Classic regime-shift overfit — pre-reg discipline held, `auto_pivot_trigger` fired, no post-hoc cherry-pick.

## Adversarial review

Perplexity Sonar Reasoning Pro (2026-05-03) ranked v8 redesign axes for HARKing-risk × statistical-power × Bonferroni-cost defensibility:

| Rank | Axis | Defensibility |
|---:|---|---|
| 1 | **E. Literature-direct (raw `-ivp30` sort, no model)** | **Highest** — no fit = no sign-flip surface |
| 2 | A. Sign-constrained Lasso | High *if* v7 pre-reg was prescriptive on sign |
| 3 | F. ElasticNet + sign constraint | Medium-High |
| 4 | C. Rank-target Lasso | Medium (patch, doesn't fix mechanism) |
| 5 | D. Rolling 2-3y train | Low (multiplicity cascade) |
| 6 | B. Regime-conditioning (VIX heads) | Low-Medium (HARKing on threshold) |

Perplexity verdict on dispersion gate: "Dispersion of 67.2pp is likely to persist, even with sign correction, because dispersion reflects cross-sectional heterogeneity in alpha delivery, which is orthogonal to directional bias. A sign-flip from +2.60 to ≈2.50 shrinks mean alpha but does not mechanically reduce the standard deviation of per-phase alphas." → v8 may FAIL informatively on dispersion gate even if mean αt clears 2.95.

Perplexity verdict on burnt holdout: STICK with 2024-04-30 → 2026-04-30 window. Window-shopping = HARKing. Burnt-holdout escape is prospective replication post-2026-04-30 at unadjusted p<0.05.

## Hypothesis

The v7 NEGATIVE-sign prior committed ex-ante per Xing-Zhang-Zhao 2010 / Bali-Hovakimian 2009 reasserts itself in low-vol-following-stress regimes. By removing the optimizer entirely, v8 tests whether the literature signal — applied deterministically — passes program-Bonferroni n=15 (|t|≥2.95) on the same burnt holdout.

**Scoring rule (LOCKED).**

```
score(asof, ticker) = -features.loc[(asof, ticker), "ivp30"]
```

Top-decile by score = bottom-decile by `ivp30` = LOW-IV names = LONG leg per Xing.

## Why ivp30 not ivx30

`ivp30` is the 1y rolling cross-sectional percentile of `ivx30`. Rank-based features:
- Are robust to vol-level outliers (high-IV outliers don't dominate cross-section)
- Match Xing 2010 / Bali 2009 specifications more closely than raw vol level
- Were pre-computed by vendor with PIT integrity verified in `docs/research/pit_replication_probe_2026_05_01.md` (Pearson 0.9990 vs empirical recompute, AAPL n=12 monthly asofs 2023)

Picking among the 4 v7 options features (`ivp30`, `ivx30`, `ivx180_minus_ivx30`, `ivx30_over_hv20`) post-v7-results would be HARKing on the L/S diagnostic evidence. Locking `ivp30` ex-ante by literature citation removes the choice-cardinality concern.

## Pre-commitments locked before pre-reg JSON write

| Choice | Value | Rationale |
|---|---|---|
| Feature | `ivp30` | Xing 2010 canonical 1y-rolling IV-percentile |
| Direction | long top-decile by `-ivp30` (= LOW-IV) | Xing prior |
| Bonferroni threshold | \|t\|≥2.95, program n=15 | Naive Bonferroni, 1-tailed α=0.05/15 |
| Dispersion gate | 50pp on `excess_net_ann` | HELD — relaxing post-v7 would be HARKing |
| Holdout | 2024-04-30 → 2026-04-30 | Same burnt window per perplexity verdict |
| Burnt-holdout escape | unadjusted p<0.05 prospective post-2026-04-30 | Per pre-reg `capital_deploy_clause` |
| Stretch tier | DROPPED | v7's Romano-Wolf m=30 stretch flag dropped — single PASS bar |

## Verdict rules

```
PASS:  primary holdout αt ≥ 2.95
       AND multi-phase mean(αt) ≥ 2.95
       AND phase dispersion < 50pp on excess_net_ann
       AND ivp30 coverage ≥ 70%
FAIL:  any gate misses → log to ledger, n=16, next |t|≥3.04
```

## Secondary post-hoc (no Bonferroni cost)

Sign-constrained Lasso on full 7-feature stack (force `coef_options ≤ 0`). Reported alongside primary as exploratory sensitivity. Does NOT enter verdict computation. Per perplexity guidance: this is amendment-to-prior, not new-hypothesis. Deferred to post-PASS analysis — if v8 primary FAILs, secondary is moot.

## Engineering scope

- `alphalens/screeners/options_implied/literature_direct.py` (NEW, ~30 LoC)
- `tests/test_options_implied_literature_direct.py` (NEW, 10 tests, RED→GREEN)
- `scripts/experiment_v8_literature_direct.py` (NEW, ~400 LoC; copy-modify of v7 minus Lasso/sign-alignment/auto_pivot)
- `scripts/audit_multi_phase.py` (MODIFY 1 line to add v8 entry)
- 1634 baseline tests (1624 + 10 new) green throughout

## Reused infrastructure (no changes)

- `alphalens/screeners/options_implied/features.py` — `build_feature_frame`
- `alphalens/screeners/options_implied/target.py` — `forward_raw_return`, `load_delisting_events_index`
- `alphalens/data/alt_data/ivolatility_smd_cache.py` — same 1449+ Tier 1 cache
- `alphalens.attribution.cost_model.CostModel.from_profile("long_only_30bps")`
- `alphalens.attribution.factor_analysis.run_regression` (HAC=5)
- `alphalens.backtest.multi_phase` (`summarise_phase_results`, `robust_verdict`)
- `scripts/audit_multi_phase.py` (phase-loop driver)

## Citations

- Xing, Y., X. Zhang, and R. Zhao. 2010. "What Does Individual Option Volatility Smirk Tell Us About Future Equity Returns?" *Journal of Financial and Quantitative Analysis*.
- Bali, T. G., and A. Hovakimian. 2009. "Volatility Spreads and Expected Stock Returns." *Management Science*.
- An, B., A. Ang, T. G. Bali, and N. Cakici. 2014. "The Joint Cross Section of Stocks and Options." *Journal of Finance*.
- Frazzini, A., and L. H. Pedersen. 2014. "Betting Against Beta." *Journal of Financial Economics*. (decile-spread convention)
- Perplexity Sonar Reasoning Pro adversarial review, 2026-05-03 (verdict + ranking).
