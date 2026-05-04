# event_drift v3 — Breadth Audit Verdict (Phase 2)

**Date**: 2026-05-03
**Pre-reg id**: `event_drift_v3_pead_quality_clean`
**Class**: `event_drift_search_2026_05_03` (1/1 ABANDONED)
**Verdict**: **BREADTH FAIL → CLASS CLOSES** (per pre-reg fail_classification.breadth_collapse)

## Pre-reg gates

```
min_daily_portfolio_breadth: 10 (mean across audit window)
p10 floor: 5
```

## Primary attempt — locked params

Top-quintile SUE × below-median accruals × Day-1 sign confirmation,
trailing-90d cohort quantile, single-active-window invariant, ex Fin/Util,
ADV ≥ $5M, 60d holding window. Universe: R2000-PIT 1612 small-caps.

| Metric | Value | Gate | Pass |
|--------|------:|-----:|:---:|
| n_asofs (Friday strides) | 104 | — | — |
| mean_daily_breadth | **3.84** | ≥10 | **FAIL** |
| median | 3 | — | — |
| p10 | **2** | ≥5 | **FAIL** |
| p25 | 3 | — | — |
| p75 | 6 | — | — |
| p90 | 6 | — | — |
| min | 0 | — | — |
| n_zero_days | 2 | — | — |

## Pre-reg-permitted retry — sue_quartile + no-Day-1 (most aggressive single relaxation)

```
sue_top_pct: 25 (vs 20)
day1_sign_confirmation: DISABLED
all other params: unchanged
```

| Metric | Value | Gate | Pass |
|--------|------:|-----:|:---:|
| mean_daily_breadth | **9.80** | ≥10 | **FAIL (just shy)** |
| median | 12 | — | — |
| p10 | **3** | ≥5 | **FAIL** |
| p25 | 5 | — | — |
| p75 | 13 | — | — |
| p90 | 14 | — | — |
| min | 0 | — | — |
| n_zero_days | 1 | — | — |

Bimodal distribution (median 12, p10 3) reflects earnings clustering: between
reporting seasons, the 60d holding window naturally drains — pre-existing
windows expire before new announcements arrive.

## Pipeline funnel diagnosis

Universe construction → window construction → invariant → Day-1 → cohort gates:

| Stage | Count | Drop |
|-------|------:|-----:|
| PIT-yaml union (R2000-PIT, 2019-2026) | 1612 | — |
| With cached OHLCV | 1612 | 0 |
| Tickers with ≥1 announcement in window | 1575 | 37 (no companyfacts EPS) |
| **Total announcements in audit window** | **57527** | — |
| Skipped: no Foster SUE (insufficient EPS history) | — | 1659 |
| Skipped: **no Sloan accruals** | — | **7163** |
| Skipped: excluded sector | — | 0 (sic_map empty in audit) |
| **Event windows built** | **1145** | -56382 (97.9% data attrition) |
| After single-active-window invariant | 1035 | 110 (overlap drops) |
| After Day-1 sign confirmation (primary) | 543 | 492 (47.5% drop, expected ~40%) |

**Root cause: Sloan accruals coverage gap on R2000-PIT.**

7163 of 9967 (1145+7163+1659=9967, ratio 71.9%) accruals computations failed
due to missing US-GAAP concept tags in companyfacts:

- `LongTermDebtCurrent` — sparsely tagged for many small-caps
- `IncomeTaxesPayable` — sparsely tagged (HC02 0-fallback applies)
- `DepreciationAndAmortization` — alternative concept names common
  (`DepreciationDepletionAndAmortization`, `Depreciation`, etc.)
- Mismatched period_end keys across concepts (one quarter has CA but not CL)

This is structural for the R2000-PIT universe (small-caps file leaner XBRL
than large-caps). Loosening to alternative concept names was discussed
as v4-future-work but rejected for this v3 pre-reg (deviation from
locked Sloan 1996 canonical formula).

## Why retry's marginal failure is informative

Retry result (mean 9.80, p10 3) shows the strategy AT THE MAXIMUM ALLOWED
RELAXATION still cannot clear the breadth floor. The p10=3 violation
specifically means there are days when only 3 names are tradable, well
below the 10-name diversification threshold for IC×breadth Sharpe ceiling
(per project's v6c precedent).

Even ignoring the gate, deploying with mean breadth ~10 would put the
strategy at the IC×breadth boundary that perplexity/zen flagged in earlier
class reviews. Sharpe ceiling ~0.34 → t-stat ~0.48 << threshold 3.50.

## Decision

Per pre-reg `fail_classification.breadth_collapse`:
> "retry once with sue_quartile or no Day-1 gate; if still <10 close class"

Both retry options exhausted (combined for max effect). Both <10 → **class closes**.

`ledger.abandon(id="event_drift_v3_pead_quality_clean", reason="...")` invoked
2026-05-03. **No holdout run. No Bonferroni budget burned.**

Program n unchanged at 19; next test still |t|≥3.27 (or +1 escalation depending on context).

## Diagnostic output files

- `/tmp/event_drift_breadth_v3_full.json` — primary attempt summary
- `/tmp/event_drift_breadth_v3_retry.json` — retry summary (sue_quartile + no-Day-1)

## What this means for future class design

1. **Universe matters more than parameters.** Strategy was sound (Engelberg
   2021 magnitude in published data), but R2000-PIT companyfacts coverage is
   too sparse for the quality conditioning leg. Future PEAD-class
   experiments should use a universe with denser fundamentals tagging
   (e.g., S&P 1500 + Russell Top 1000) before re-attempting accruals
   conditioning.

2. **Pre-reg breadth gates work.** The Phase 2 breadth-audit gate caught
   structural infeasibility BEFORE running the holdout. This is exactly
   the methodology bundle's purpose: fail cheap, fail early, preserve
   Bonferroni budget.

3. **Foster SUE plumbing is reusable.** No defects found in
   `alphalens.data.fundamentals.sue.FosterSUEStore` (used 9967 times
   across the audit). The bottleneck was strictly the accruals computation.

4. **Class closure ≠ thesis disproof.** PEAD-with-quality may still work on
   a denser-coverage universe. The class CLOSES on this universe + this
   data layer, NOT on the underlying anomaly.
