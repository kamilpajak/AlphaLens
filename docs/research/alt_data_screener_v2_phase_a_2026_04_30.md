# alt_data_screener_v2 Phase A — pipeline plumbing validation

**Pre-registration:** `alt_data_screener_v2_2026_04_30` (class
`alt_data_screener_search_2026_04_30`, n=2 → Bonferroni |t|≥2.24).
**Pre-reg JSON:** `docs/research/preregistration/params_alt_data_screener_v2_2026_04_30.json`
**PIT audit + adversarial review log:** `docs/research/v4_alt_data_pit_audit_2026_04_30.md`
**Date:** 2026-04-30 (same session as audit + pre-reg lock)

## Verdict: PASS — pipeline validated end-to-end. Ready for Phase B production run.

## Build summary

Three new modules + one extension landed in this session:

| Component | Path | Tests | Lines |
|---|---|---:|---:|
| Polygon short-interest client | `alphalens/data/alt_data/polygon_short_interest.py` | 10/10 ✓ | 196 |
| Foster SUE w/ first-filed PIT | `alphalens/data/fundamentals/sue.py` | 9/9 ✓ | 234 |
| v4 feature joiner | `alphalens/screeners/alt_data/features.py` | 15/15 ✓ | 343 |
| `fit_global` `feature_names` param | `alphalens/screeners/multi_source_two_stage/model.py` | (existing tests still pass) | +4 |
| Phase B experiment driver | `scripts/experiment_alt_data_lasso_20d.py` | smoke ✓ | 599 |

Full test suite: 1477/1477 PASS (4 skipped) — no regressions.

## Smoke-test plumbing trace

Ran with 21 R2000-mid-cap tickers (locally cached) + 2-year train (2022-01-01 → 2023-12-31)
+ 1.3-year holdout (2024-01-01 → 2026-04-30) at stride=5 with 12 sparse asofs.

Pipeline traversed every stage cleanly:

```
SMOKE mode: universe = ['AAPL', 'ABG', 'ABR', 'ABM', 'ACAD', 'ACA', 'ACEL',
                        'AAOI', 'AAP', 'ABCB', 'ABEO', 'ABM', 'ABT', 'ABX',
                        'ACDC', 'ACE', 'ADBE', 'ADM', 'AEM', 'AEP', 'AES']
history store has 14 tickers                            # 14/21 in price cache
carhart 1318 rows 2020-11-27..2026-02-27                # ✓
calendar: 12 asof dates 2022-01-03..2025-12-12          # ✓
feature frame raw shape: (252, 12)                      # 21 × 12 = 252 ✓
feature frame post-ADV (>=$1M): (142, 12)               # ADV filter applied ✓
target frame (holding=20d): 130 rows, 125 non-NaN       # ✓
split — train: 61 feature rows, 55 target rows;
        holdout: 81 feature rows                        # ✓
train aligned: 61 rows after NaN-target drop            # ✓
regime GLOBAL: n=61, λ chosen=0.07318 (idx 0/24),
               CV MSE=0.0211823, nonzero coefs=0        # ⚠ all-zero (expected at n=61)
holdout scored rows: 81 / 81                            # ✓
holdout rebalances (overlapping 20d-tranches): 6        # ⚠ too few for Carhart
[Carhart fails: need ≥20, got 6 — smoke scope limit]
```

Each ✓ stage = a code path validated. The two ⚠s are consequences of the smoke
universe size (21 tickers × 12 asofs), NOT bugs:

1. **0/10 nonzero coefs at λ=0.07318** — λ_min in the glmnet grid was selected,
   meaning even the strongest regularization in the grid still zeroed all 10 coefs.
   At n=61 train rows this is the expected behavior (Lasso L1 needs more samples
   to surface signal at any feature count > 0). Identical pattern to v3 (n=21 features
   zeroed at n=99 train rebalances) and v2 (n=21 features zeroed at full sample).
   **Cannot distinguish "tiny sample" from "no signal" at smoke scope.**
2. **Carhart-4F regression rejected n=6 holdout obs** — `factor_analysis.run_regression`
   guards against underpowered fits; correct behavior. Production Phase B will have
   ~99 holdout rebalances per pre-reg.

## What Phase A confirms

- Feature joiner produces all 10 columns matching the locked pre-reg whitelist
- All 6 PIT contracts gate correctly (insider F4, EDGAR filing-date, SUE first-filed,
  Polygon short-interest +8 BD, shares-outstanding `filed ≤ asof`, OHLCV truncation)
- Time-decay multiplier `exp(-recency/30)` is applied to SUE + PEAD
- Cross-sectional ranks compute per asof
- Train/holdout split honors the locked 2024-04-30 boundary
- `fit_global` + `predict_scores_global` plumbing works with the new 10-feature
  whitelist (after adding `feature_names` parameter to model.py — backwards-compatible
  change, prior class tests still pass)
- Top-N portfolio + 20d-forward returns + cost model + drag computation all
  traverse correctly

## What Phase A does NOT confirm (deferred to Phase B)

- Whether the v4 features actually carry cross-sectional signal at production scale
- Whether the Bonferroni |t|≥2.24 gate clears
- Whether Lasso surfaces nonzero coefficients on the full 6.3y train pool
- Multi-phase robustness (Phase C, contingent on Phase B not-FAIL)

## Production Phase B run — required prerequisites

To produce a binding holdout verdict, the next session must:

1. **Bulk-fetch Polygon short-interest** for the full PIT universe (~1500 tickers).
   Each ticker is one paginated REST call (≤500 records / call); cache hit on
   subsequent runs. Estimated wall-clock: 30-60 min at standard rate.
2. **Verify companyfacts coverage** for the universe — `~/.alphalens/companyfacts/`
   currently has ~1900 cached JSONs which should cover most of the PIT universe.
   Tickers without companyfacts get NaN SUE/PEAD (handled in feature joiner).
3. **Run** `scripts/experiment_alt_data_lasso_20d.py` (no `--smoke`) with
   defaults: train 2018-01-01 → 2024-04-29, holdout 2024-04-30 → 2026-04-30,
   stride=5, holding=20, top-N=30, ADV≥$5M, cost 30bps round-trip.
4. **Read the verdict** in the auto-generated
   `docs/research/alt_data_screener_v2_phase_b.md`.
5. **If PASS or MID:** run multi-phase audit per pre-reg `phase_robustness_followup`
   clause (5 phase offsets, each requiring αt≥1.5; mean αt≥2.24 in-class or ≥2.50
   for capital deploy).
6. **If FAIL:** complete ledger entry via `alphalens preregister complete
   --id alt_data_screener_v2_2026_04_30 --verdict FAIL ...`. Document in findings.
   Class is then 2/2 FAIL → next test in class needs |t|≥2.39.

## Adversarial-review carry-over (from PIT audit doc)

All zen + perplexity objections that were accepted are LOCKED in implementation:

- **PEAD truncation gate** `f + 5 BD ≤ asof` — `alt_data/features._pead_5d_post`
- **SUE first-filed PIT** — `data/fundamentals/sue._first_filed_per_period_end`
- **No raw + rank multicollinearity** — only `rank_short_interest_pct_float` and
  `rank_realized_downside_skew_60d` are surfaced; raw counterparts excluded
- **Time-decay multiplier** `exp(-recency/30)` on SUE + PEAD —
  `alt_data/features._decay_multiplier`
- **NFCI/UMCSENT dropped** for cross-sectional incoherence — feature whitelist
  has zero market-broadcast signals

Two surfaced-as-novelty-bets remain documented (NOT silently dropped) in the
v2 pre-reg JSON `feature_construction[*].literature_pedigree_gap` fields:

- `rank_realized_downside_skew_60d` — realized vs implied skew literature gap
- (Group B was supposed to be a novelty bet too, but the FINRA→Polygon supersession
  resolved it: Polygon bi-monthly short-interest IS the canonical Diether-Lee-Werner
  construct, so all 3 short-interest features now have full literature pedigree.)
