# layer4-v2 de-quantization — pre-registered what-if candidate

**Status:** PRE-REGISTERED (formula frozen 2026-07-06, BEFORE any post-2026-07-05 outcome exists; no code ships now)
**Date:** 2026-07-06
**Decision class:** ordering-base redesign candidate (brief sort key base); evaluation-gated, what-if reconstruction — NO shadow column, NO pipeline change, NO version bump until (and unless) it passes
**Related:** [`selection_score_v2_ext_tilt_decision_2026_07_06.md`](selection_score_v2_ext_tilt_decision_2026_07_06.md) (the burnt-panel declaration and the V2A ext-axis revisit this shares its September look with), [`edge_signal_attribution_2026_07_06.md`](edge_signal_attribution_2026_07_06.md)

---

## 1. Problem

`layer4_weighted_score` (`scorer.py::compose_weighted_score`) is an INTEGER 1..5: a sum of four quantized components clipped to [1, 5]. On the current panel 156/415 plannable candidates share the value 3. Consequences:

- Continuous information the pipeline already computes (`catalyst_strength` 0..1, `fcff_yield_sector_percentile` 0..100, RSI/MA50 distance) is collapsed to 0/1 (or 0/1/2) BEFORE ranking.
- The sub-1.0 `atr_penalty` can only reorder candidates WITHIN a layer4 tie group — with a median 5.5 candidates/day, top-3 membership is nearly insensitive to any penalty tuning. This was the structural ceiling identified by the 2026-07-06 scorer-v2 calibration ("layer4 is a coarse integer 1..5 … sub-1.0 differences only reorder within layer4 tie groups").

Hypothesis: de-quantizing the base — same inputs, same semantics, continuous transforms — improves the ordering's correspondence with realized outcomes by letting the information that is already there act.

## 2. Why what-if, not shadow logging

Every input of the frozen formula below is already persisted per `(brief_date, ticker)` on the thematic-briefs parquet: `fcff_yield_sector_percentile`, `catalyst_strength`, `technical_rsi`, `technical_ma50_distance_pct`, `magic_formula_rank`, `magic_formula_cohort_n`, `deep_drawdown_reversal`, `technical_atr_pct`, `layer4_weighted_score`. Reconstruction is therefore EXACT at any later date (unlike the novelty case, #643, where inputs were not persisted and stamping was required). What is epistemically load-bearing is not WHEN the score is computed but WHEN the formula is frozen relative to seeing outcomes — September outcomes do not exist yet, so this dated commit provides the same guarantee as a stamped column, at zero pipeline cost.

Residual risks accepted: input-column semantics drift is tracked by the existing config-version stamps and column contracts (silent drops are test-guarded); freeze discipline is this commit.

## 3. Frozen formula (candidate name: `layer4-v2-dequant-2026-07-06`)

No outcome fitting anywhere. Each term de-quantizes its v1 counterpart with zero or symmetric-by-construction parameters. Missing/NaN input → term contributes 0 (v1's "never reward unknown", unchanged).

```
layer4_v2 = fcff_term + valrev_term + tech_term + cat_term          # float in [0, 5]
```

1. **`fcff_term` ∈ [0, 1]** = `fcff_yield_sector_percentile / 100`.
   (v1: `int(percentile >= 50)`. Same input, step → identity ramp. Missing percentile → 0, matching v1's False-on-None; note this also removes v1's thin-cohort midpoint-50 concern — a fallback 50 now contributes 0.5 instead of a full +1.)
2. **`valrev_term` ∈ {0, 1}** = `int(is_top_quartile(magic_formula_rank, magic_formula_cohort_n) OR deep_drawdown_reversal)`.
   (UNCHANGED from v1 — deliberately NOT de-quantized: the July attribution found the magic-formula/quality cluster to be an ATR proxy; giving it continuous weight would amplify a known confound. Both alpha drivers keep the shared binary slot.)
3. **`tech_term` ∈ [0, 1]** = `rsi_kernel(technical_rsi) × ma50_kernel(technical_ma50_distance_pct)`.
   v1 is the AND of two window tests (RSI ∈ [30, 70]; |MA50 dist| ≤ 15%). v2 replaces each hard window with a plateau + linear skirt whose width equals the plateau half-width (the single, symmetry-frozen rule — no tuned constants):
   - `rsi_kernel`: 1.0 inside [30, 70]; linear to 0.0 at 10 and 90 (plateau half-width 20 → skirt width 20); 0.0 outside [10, 90]. Missing → 0.
   - `ma50_kernel`: 1.0 inside [−15, +15]; linear to 0.0 at ∓30 (half-width 15 → skirt 15); 0.0 beyond ±30. Missing → 0.
   (Product, not min, so being marginal on both dimensions scores below being marginal on one — consistent with the July finding that MA50-extension is its own fade axis.)
4. **`cat_term` ∈ [0, 2]** = `2 × catalyst_strength` (clipped to [0, 2] if strength ever exceeds 1).
   (v1: `catalyst_floor` step 0/1/2 at 0.45/0.70. Same input, steps → linear ramp. Missing → 0.)

No [1, 5] floor-clip (v1's floor-at-1 is cosmetic; ordering is unaffected by an additive floor). For ordering comparison the candidate sort key is `selection_score_v2 = layer4_v2 − atr_penalty(technical_atr_pct)` with the LIVE v1 penalty (frozen 5.77/8.37, λ=1.0) — one change at a time; the base is the hypothesis, the penalty is held fixed.

## 4. Pre-registered evaluation protocol (~2026-09)

Shares the September look (and its program-level multiplicity accounting) with the V2A ext-axis revisit from the scorer-v2 decision memo — **two pre-registered candidates, two Bonferroni charges, one fresh window, both declared here and there before any look**.

- **Holdout: fresh data only** — outcomes from `brief_date > 2026-07-05`. The panel through 2026-07-05 is BURNT (declared in the scorer-v2 memo) and must not be re-used for adjudication. This memo's formula was frozen without evaluating it on ANY outcome data, burnt or otherwise.
- **Trigger gates (both):** (a) ≥40 car_10 brief-days of post-2026-07-05 outcomes; (b) regime diversity vs the burnt 2026-05-27..06-18 block. Same gates as V2A — one combined September session.
- **Procedure:** what-if reconstruction of `layer4_v2` from persisted parquet columns; ticker-episode dedup (chained 5-session window, keep first); compare ordering by `selection_score_v2` vs live `selection_score` on: (i) Spearman vs car_10; (ii) top-3-per-day mean car_10, day-block bootstrap (B=2000) of the difference; (iii) loser-avoidance (bottom-quintile car_10 episodes placed in top-3).
- **Success line (ships as live base, with `SCORER_CONFIG_VERSION` bump and frozen v1 cohort):** top-3 mean car_10 delta vs live with 95% CI excluding 0 on the positive side, robust to ±1-week cut placement, direction confirmed on Spearman.
- **Kill line:** anything less → REJECTED memo update, formula retired (a materially different base would be a NEW pre-registration with a new charge).
- **Ordering of September looks:** per the scorer-v2 memo, the v1-ATR-tilt decay controls (C_atrrank, C_l4only) run FIRST; if the ATR tilt is retired, this candidate is re-based on the then-live sort key before evaluation (the base hypothesis is orthogonal to the penalty decision).

## 5. Out of scope

- Any input swap (e.g. `valuation_fcf_margin` replacing the FCFF sector percentile) — that is a different hypothesis, not de-quantization; would need its own pre-registration.
- Insider re-integration (held to the existing Phase-4 offline lift-test plan).
- Re-weighting components (all weights stay at v1's implicit 1×/1×/1×/2×-max) — weight tuning on 23 burnt days is the exact trap the scorer-v2 calibration documented.
