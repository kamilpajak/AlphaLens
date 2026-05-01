# alt_data_screener v2 — closing findings (2026-05-01)

**Pre-registration:** `alt_data_screener_v2_2026_04_30` (class
`alt_data_screener_search_2026_04_30`, n=2 → Bonferroni |t|≥2.24)
**Verdict:** **FAIL** (logged to ledger 2026-05-01)
**Audit JSON:** `docs/research/alt_data_screener_v2_audit.json`
**Auto-generated phase B report:** `docs/research/alt_data_screener_v2_phase_b.md`

## Headline

Outright FAIL on the **explicit zero-coefficient gate** that v2-v3 of the prior
class established as a hard pre-reg failure mode. 0/10 nonzero Lasso coefficients
on a 221,920-row training pool. HAC-5 alpha t-stat = +0.05 (~zero). MaxDD = -61.5%.

| Metric | Value | Pass-rule |
|---|---:|---:|
| α t-stat (HAC=5) | +0.05 | ≥ +2.24 |
| Lo-adj Sharpe (net) | 0.78 | ≥ 0.50 |
| α annualized (gross) | +7.79% | ≥ +3.00% |
| α annualized (net) | +5.70% | — |
| Excess vs SPY (net) | +7.32% | — |
| Max drawdown (net) | **−61.52%** | ≥ −35% |
| Nonzero coefs | **0 / 10** | ≥ 1 |

Three pre-reg gates fail: zero-coef, alpha t-stat, MaxDD.

## The "headline alpha" is structural

The 0.78 Sharpe and +7.32% net-excess look superficially attractive. They are
not signal — they are arithmetic artifacts. With 0 nonzero coefs, the Lasso
prediction is uniform across all tickers. "Top 30 by score" therefore picks
30 essentially-tied tickers; in practice the tie-break is row order in the
holdout DataFrame, which correlates with universe alphabetical / membership-
rotation patterns.

The Carhart-4F regression with HAC=5 strips factor exposure and reports
**α t = +0.05** — confirming the strategy carries effectively zero residual
alpha beyond Mkt-RF / SMB / HML / MOM.

The −61.5% MaxDD on the net-cumulative path further reinforces that the
selection is noise: a real signal at +7%/y net would have far less drawdown
than the universe-average benchmark.

## Why the v4 fresh class still failed

v4 v2 was the first pre-registered test against this target/universe/holdout
combination using a substantively different feature space (8 of 10 features
new vs prior class). Two anchor features carried over (`insider_log_count`,
`insider_log_dollar`) as deliberate diagnostics. All 10 features had
established or freshly-restored literature pedigree:

- 3 Foster-1977 SUE / PEAD / event-recency (with first-filed PIT + decay)
- 3 Diether-Lee-Werner short-interest (% float change, rank, days-to-cover)
- 2 Form 4 insider anchors
- 1 realized downside-skew rank (novelty bet, documented gap to implied)
- 1 EDGAR filing-density count

Lasso L1 + CV-MSE selected λ at the **maximum penalty in the 25-point grid**
(λ_grid index 0/24), zeroing every coefficient. The CV-MSE at this λ is
0.0291 (vs. unconditional target variance) — meaning none of the 10 features
provide enough cross-validated mean-squared-error reduction to justify even
the smallest non-zero coefficient under the L1 penalty.

This is the SAME pattern observed in prior class v2 (0/21 coefs at 5d) and
v3 (0/21 coefs at 20d). Substituting features, even literature-pedigreed
ones, does not break the pattern.

## What this confirms

The bottleneck for cross-sectional alpha on `AlphaLens PIT universe + ADV≥$5M
+ 20d-forward excess return + Lasso L1 + CV-MSE` is **NOT**:

- ❌ The architecture (single-global vs 4-regime tested in prior v1+v2)
- ❌ The horizon (5d vs 20d tested in prior v2+v3)
- ❌ The feature space content (21 OHLCV+macro+insider tested vs 10 alt-data
  tested — both produce zero coefs)
- ❌ Literature pedigree of features (event-driven SUE/PEAD have decades of
  empirical support and still got zeroed)
- ❌ Cross-sectional dispersion absent (NFCI/UMCSENT correctly excluded;
  remaining features all have per-asof variance)

The bottleneck is more likely:

1. **CV-MSE is the wrong objective for cross-sectional ranking.** It penalizes
   absolute return-prediction error, but we only need the RANK to be useful.
   ElasticNet with cross-sectional Spearman objective, or rank-IC-targeted
   loss, may surface signal that L1 + MSE buries.
2. **The universe + cost structure makes net alpha thin.** AlphaLens PIT +
   ADV≥$5M is largely small-mid cap; transaction costs (30 bps round-trip)
   eat ~2% annualised drag at our turnover. Even a real signal of 3-5% gross
   alpha barely clears the threshold after costs.
3. **The holdout window 2024-2026 is too short to surface low-base-rate
   anomalies.** 99 rebalances at stride=5 with 75% holding overlap = ~25
   independent observations. Many published cross-sectional anomalies need
   500+ months to clear standard significance bars.

## Class status after v2

`alt_data_screener_search_2026_04_30`:
- v1 abandoned (FINRA infra blocked) — no holdout observed, but counts in
  ledger n
- v2 completed FAIL (αt=0.05)
- **n=2 in class → next pre-reg in class would face |t|≥2.24** (unchanged
  from v2's threshold; n stays at 2 until next add)
- **If n=3 ever registered → |t|≥2.39**

## Wider research-program status

This is the **11th paradigm failure** since the strategic pivot of 2026-04-25
that reframed the project as research infrastructure. Prior failures:

1. Themed momentum scorer (Layer 2b, CLOSED 2026-04-22)
2. Fundamental gate Phase 2 (CLOSED)
3. Fundamental gate holding-60 (CLOSED 2026-04-23)
4. Layer 2d insider clusters (CLOSED 2026-04-24)
5. Tactical sector rotation (CLOSED 2026-04-25)
6. Tri-factor (FAIL 2026-04-29 phase-robust)
7. Mom+lowvol combo (FAIL 2026-04-29 phase-robust)
8. Quality+momentum combo (FAIL 2026-04-30)
9. Vol-target overlay (FAIL 2026-04-30)
10. multi_source_two_stage_search class 3/3 (FAIL 2026-04-30)
11. **alt_data_screener_search v2 (FAIL 2026-05-01)** ← this entry

## Next-step options (NOT prescriptive — record of what would be defensible)

Per `feedback_keep_searching_screeners.md`, the search remains open. Defensible
next-class hypotheses, ordered by structural distance from prior failures:

**A. Different objective function (high distance).** Re-frame as a rank-based
loss: minimize negative cross-sectional Spearman correlation between predicted
score and forward return, instead of MSE. Could re-use v4's 10 features OR
prior class's 21 features. New class label `alt_data_rankloss_*` or similar.

**B. Tree-based model (medium distance).** Switch from Lasso to LightGBM /
XGBoost with similar feature set. Tree models don't have the L1 zero-coef
failure mode; they will always produce some splits. Risk: overfit at 222k
train rows + 10 features is much higher; need careful regularization.

**C. Different universe (medium distance).** Restrict to a sub-universe where
known anomalies have stronger empirical backing — e.g. R2000 small-cap-only
(short interest is more salient at the small end), or low-volume-decile
(microstructure mean-reversion).

**D. Different holding period (low distance, already tested twice).** 5d, 20d
both failed. 60d-monthly holding period would test the slow-anomaly hypothesis
but cuts holdout n=99 to ~25 — borderline statistically.

**E. Walk-forward refit (medium distance).** Refit Lasso every 6-12 months
instead of one global fit on full train pool. Captures regime drift.

None of A-E should be undertaken in the same session as this verdict. Document,
sleep on it, return to choose deliberately.

## Memory + ledger artifacts

- Ledger: `alt_data_screener_v2_2026_04_30` → `completed → FAIL (αt=0.05)`
- Audit JSON: `docs/research/alt_data_screener_v2_audit.json`
- Phase B auto-report: `docs/research/alt_data_screener_v2_phase_b.md`
- Phase A plumbing log: `docs/research/alt_data_screener_v2_phase_a_2026_04_30.md`
- PIT audit + adversarial review: `docs/research/v4_alt_data_pit_audit_2026_04_30.md`
- Pre-reg JSON: `docs/research/preregistration/params_alt_data_screener_v2_2026_04_30.json`
- v1 superseded JSON: `docs/research/preregistration/params_alt_data_screener_v1_finra_blocked_2026_04_30.json`
- New code retained as reusable infrastructure (RESEARCH_ONLY status):
  - `alphalens/data/alt_data/polygon_short_interest.py`
  - `alphalens/data/fundamentals/sue.py` (Foster + first-filed PIT)
  - `alphalens/screeners/alt_data/features.py` (10-feature joiner)
  - `scripts/experiment_alt_data_lasso_20d.py` (driver)
- Total Polygon SI cache: ~265k records across 1618 tickers, ~25MB on disk
  at `~/.alphalens/polygon_short_interest/`
