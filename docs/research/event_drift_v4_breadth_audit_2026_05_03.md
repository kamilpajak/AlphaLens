# event_drift v4 — Breadth Audit Verdict (Phase 2)

**Date**: 2026-05-03
**Pre-reg id**: `event_drift_v4_pead_quality_sp1500`
**Class**: `event_drift_search_2026_05_03` (in-class extension; 2/2 attempts after v3 ABANDONED)
**Verdict**: **<TO BE FILLED — PASS / FAIL>**

## Pre-reg gates

```
min_daily_portfolio_breadth: 10 (mean across audit window)
min_daily_portfolio_breadth_p10: 5
```

## Primary attempt — locked params

Top-quintile SUE × below-median accruals × Day-1 sign confirmation,
trailing-90d cohort quantile, single-active-window invariant, ex Fin/Util,
ADV ≥ $5M, 60d holding window. Universe: **S&P 1500 PIT (FALLBACK proxy)** — pivot from
v3's R2000 per pre-committed contingency in v3 breadth-audit postmortem L121-125.

| Metric | Value | Gate | Pass |
|--------|------:|-----:|:---:|
| n_asofs (Friday strides) | <FILL> | — | — |
| mean_daily_breadth | <FILL> | ≥10 | <FILL> |
| median | <FILL> | — | — |
| p10 | <FILL> | ≥5 | <FILL> |
| p25 | <FILL> | — | — |
| p75 | <FILL> | — | — |
| p90 | <FILL> | — | — |
| min | <FILL> | — | — |
| n_zero_days | <FILL> | — | — |

## Pre-reg-permitted retry (only if primary FAILs)

```
sue_top_pct: 25 (vs 20)            # OR
day1_sign_confirmation: DISABLED   # single-axis only
```

| Metric | Value | Gate | Pass |
|--------|------:|-----:|:---:|
| mean_daily_breadth | <FILL> | ≥10 | <FILL> |
| p10 | <FILL> | ≥5 | <FILL> |

## Pipeline funnel diagnosis

| Stage | Count | Drop |
|-------|------:|-----:|
| S&P 1500 PIT (FALLBACK union) | <FILL> | — |
| With cached OHLCV | <FILL> | <FILL> |
| Tickers with ≥1 announcement in window | <FILL> | <FILL> (no companyfacts EPS) |
| Total announcements in audit window | <FILL> | — |
| Skipped: no Foster SUE | — | <FILL> |
| Skipped: no Sloan accruals | — | <FILL> |
| Skipped: excluded sector | — | <FILL> |
| Event windows built | <FILL> | <FILL> |
| After single-active-window invariant | <FILL> | <FILL> |
| After Day-1 sign confirmation (primary) | <FILL> | <FILL> |

**Key comparison vs v3:** accruals attrition was 71.9% (7163 of 9967) on R2000 small-caps. v4 expected ~10-20% on S&P 1500's denser GAAP tagging.

## Decision

Per pre-reg `fail_classification.breadth_collapse`:
> "retry once with sue_quartile or no Day-1 gate; if still <10 close class"

**<FILL VERDICT MATRIX>**

- If primary PASS → proceed to Phase 3 (holdout single-shot)
- If primary FAIL + retry PASS → proceed to Phase 3 with retry params (must amend pre-reg note)
- If both FAIL → class CLOSES; pivot to alt-data v2 holdout (deferred candidate per v4 design)

## Survivorship caveat

S&P 1500 PIT FALLBACK proxy uses CURRENT iShares ETF holdings (IVV/IJH/IJR) labeled with
backdated as_of. Companies that left the index between snapshot date and historical asofs
are MISSING from the universe. Estimated bias: ~150-300 bps/y on the 2-year holdout.
Documented in v4 design memo + this verdict. If holdout PASS, prospective replication
on accruing data post-2026-04-30 is not affected by this caveat.

## Diagnostic output files

- `/tmp/event_drift_breadth_v4_full.json` — primary attempt summary
- `/tmp/event_drift_breadth_v4_retry.json` — retry summary (if needed)

## Adversarial-review reminder

zen DISSENTED on universe (recommended S&P 600 small-cap-only over S&P 1500 — PEAD
arbitraged away in S&P 500 dilutes signal). User elected S&P 1500 for safer breadth at
cost of expected lower αt ceiling. If breadth PASS but holdout αt in [1.0, 1.8], v5 =
S&P 600 isolated to validate dilution hypothesis (per v4 design `dilution_diagnostic`).

## Next steps if VERDICT = PASS

1. Phase 3 — holdout single-shot run via `experiment_event_drift_v4.py --mode holdout`
2. Compute Carhart-4F αt with HAC(maxlags=5) on (long-only-PEAD-quality - MDY) returns
3. Multi-phase robustness audit (5 phases, αt range ≤ 0.5 gate)
4. Update `~/.alphalens/preregistration/ledger.json` via `alphalens preregister complete`

## Next steps if VERDICT = FAIL

1. Mark v4 ABANDONED in ledger; class closes 2/2
2. Pivot to alt-data v2 holdout (already pre-registered, ~2-3 hr)
3. iVolatility decision 2026-05-07: cancel (no surviving Layer-1 base to compound with)
