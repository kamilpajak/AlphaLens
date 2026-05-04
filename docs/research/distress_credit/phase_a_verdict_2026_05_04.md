# Phase A verdict — distress_credit_v1_2026_05_04

**Date:** 2026-05-04
**Status:** AUTO-PIVOT TRIGGERED (Layer 4 dropped from PRIMARY)

## Outcome

The pre-committed Phase A overlay sanity gate (`a4_extended`) **FAILED** on TRAIN (2017-01-03 → 2024-04-29). Layer 4 (HY OAS regime overlay) is DROPPED from the PRIMARY hypothesis per pre-reg `success_criteria.auto_pivot_triggers.l4_overlay_sanity_failed`. New PRIMARY hypothesis: pure-Layer-2 long-only equal-weighted bottom-quintile Merton-PD portfolio under same |t|≥3.50 threshold.

## Numbers (BAA10Y proxy on TRAIN, n=127 monthly rebalances)

| Metric | Value | Gate | Pass? |
|--------|-------|------|-------|
| Pearson(spread_z, fwd_21d_SPY) full sample | **+0.047** | ≤ −0.05 | ❌ |
| Spearman full sample | **+0.049** | ≤ −0.05 | ❌ |
| First half (≈2017-2020) | +0.115 | — | — |
| Second half (≈2020-2024) | −0.090 | — | — |
| |first half − second half| | 0.205 | ≤ 0.4 | ✅ |
| **Overall** | | | **❌ DROP_LAYER_4** |

## Interpretation

The credit-spread regime gate would invert sign on TRAIN. Empirical reality 2017-2024:
- **First half** (~2017-2020 incl. COVID Q1 2020): spread widening associated with HIGHER forward 21d SPY returns (+0.12 Pearson). Recovery dynamics dominated.
- **Second half** (2020-2024): correlation flipped negative (−0.09) — the canonical "wider spreads = weaker equity" relationship reasserted.
- **Aggregate**: small positive bias (+0.047), within decade-window stability gate (drift 0.20 < 0.40) but failing the full-sample directionality gate (≤ −0.05).

This empirical pattern confirms the FATAL #1 concern from perplexity adversarial review (2026-05-04). A linear-interp gate that reduces exposure on widening spread would have de-levered into recovery rallies in 2020 and similar periods. Net effect: structurally underperforms buy-hold during recovery dynamics.

## Data substitution caveat

`BAMLH0A0HYM2` (FRED ICE BofA US High Yield OAS) is publicly available only from 2023-05-02 (ICE/BofA copyright restriction). For the Phase A overlay sanity check on TRAIN, we used `BAA10Y` (Moody's BAA Yield − 10Y Treasury) as a credit-spread proxy. BAA10Y has full coverage 1986-01-02 → 2026-04-27 (10079 obs).

Literature (Gilchrist-Zakrajsek 2012, Adrian-Crump-Moench 2013) confirms BAA10Y and HY OAS are highly correlated and have aligned direction-of-association with forward equity returns. The proxy substitution is well-grounded but should be flagged as a Phase A enhancement of the originally-locked design.

The secondary check on the limited HY OAS 2023-05..2024-04 subset failed for "insufficient samples (0)" because the 252-day rolling lookback consumes most of the available cache. This itself confirms the data-availability issue.

## New PRIMARY hypothesis (post auto-pivot)

> Long-only equal-weighted bottom-quintile Merton-PD portfolio drawn from S&P 1500 PIT (excluding top-50 mega-caps and excluding top-quintile-distress always) produces mean Carhart-4F α t-stat ≥ 3.50 across 5-phase OOS audit on holdout 2024-04-30 → 2026-04-30, with every-phase α t-stat ≥ 0, α t-stat dispersion ≤ 0.5 across phases, and excess_net_ann dispersion ≤ 50pp.

**Removed gates:** Sharpe-improvement ≥ 0.50 over Carhart-4F-residualized SP1500 baseline (was Layer-4 specific; downgraded to descriptive secondary).

**Retained gates:** primary 4F α t-stat ≥ 3.50, every-phase ≥ 0, dispersion ≤ 0.5, excess_net_ann dispersion ≤ 50pp.

## Honest re-assessment of pass-prob

Without Layer 4, this experiment becomes structurally similar to a low-leverage / quality factor test. Closely related to (but NOT identical to) the burnt `price_factor_search_2026_04_29` class which had 4/4 FAILs (mom, contrarian, mom+lowvol, quality+momentum). Differentiation:

- Merton DD uses **leverage** (Total Liabilities) which raw lowvol does not.
- Selection is on Merton PD (combines leverage + vol + drift) not pure realized vol.

Per perplexity ranking + holdout regime context, pass-prob revised:
- Original compound (with overlay): 8-12%
- Pure-Layer-2 (post auto-pivot): **~5-8%** — overlap with burnt low-vol cousin reduces edge

FAIL remains the most likely outcome. Pre-reg discipline holds — the auto-pivot was committed, and we run the contingent plan honestly.

## Next steps

1. Build production `LiabilitiesStore` + `ShareCountStore` reading companyfacts parquet.
2. Run remaining Phase A diagnostic checks (A1-A3, A5-A8). Emit JSON reports.
3. Implement `scripts/experiment_distress_credit_v1.py` (long-only safe-decile, NO overlay).
4. Run Phase B holdout audit on runpod (5 phases).
5. `alphalens preregister complete` with verdict + outcome memo.
