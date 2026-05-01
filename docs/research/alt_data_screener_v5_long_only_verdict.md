# v5 long-only verdict — alt_data_screener_search_2026_04_30 CLOSED 5/5 FAIL

**Date:** 2026-05-01 PM
**Pre-reg:** `alt_data_screener_v5_long_only_2026_05_01` (program-level n=10 → |t|≥2.81)
**Provenance:** Path β auto-pivot from v10 (analyst_alt_data_v10) Phase A gate 2 yfinance survivorship FAIL.

## Headline verdict: FAIL DECISIVE

| Metric | Value | Gate | Result |
|---|---|---|---|
| Carhart-4F α t-stat (HAC=5) | **−3.20** | ≥+2.81 (primary) | **FAIL — negative significant** |
| α 4F annualized | −192.7%/y | ≥3% | FAIL |
| Sharpe net (Lo-adj) | −1.11 | ≥0.5 | FAIL |
| Excess vs SPY (net) | −37.3%/y | ≥3% | FAIL |
| Max drawdown | −66.7% | ≥−25% | FAIL |
| in-CV IR | 0.338 | ≥0.5 PASS, ≥0.3 MID | MID-by-gate (informationally fail) |
| Holdout mean rank-IC | +0.0260 | >0 | PASS (informational only) |
| Nonzero Lasso coefs | 2/10 | ≥1 | PASS structurally |
| Mkt-RF β | −0.14 | informational | within bound |

**Verdict:** FAIL on 5 primary gates. Stretch threshold 3.2 not assessed (primary failed).

## Lasso fit (deterministic — identical to v3, v4)

```
λ chosen = 0.00289 (idx 4/24)
n_train = 221,920
nonzero coefs (2/10):
  rank_short_interest_pct_float = −0.0063
  filing_density_4q             = +0.0016
```

Confirms v3/v4 finding: bulk holdout rank-IC +0.0260 has signal at population level,
but selection-rule magnification + benchmark choice flips actionable α.

## Critical diagnostic — long leg vs benchmark

v4 reported "long leg +20.6%/y across sub-periods" as suggestive of
extractable signal in the long side. v5's long-only test confirms the long
leg performance number — **but reveals it was alpha illusion vs SPY benchmark**:

| Sub-period | n_rebal | long leg ann | SPY ann | Excess (long − SPY) |
|---|---|---|---|---|
| 2024-04-30 → 2024-12-31 | 34 | +29.9%/y | **+75.8%/y** | **−45.9%/y** |
| 2025-01-01 → 2025-12-31 | 50 | +16.2%/y | **+67.6%/y** | **−51.4%/y** |
| 2026-01-01 → 2026-04-30 | 10 | +10.9%/y | −68.7%/y | +79.5%/y |

Long-leg average 19.0%/y ≈ v4's reported 20.6%/y (within rebalance noise).
SPY benchmark in 2024-2025: **mega-cap-driven 60-70%/y rally** (Mag-7 dominated).
Lasso scoring with `−rank_SI%` + `+filing_density` systematically tilted **away from
mega-cap winners** (high-SI companies and low-filing-density steady-state companies),
hence underperformed SPY by 45-51 percentage points annually.

The 2026 partial flip (+79.5% excess) is one-quarter sample (n=10 rebal) where
SPY took a sharp drawdown; long leg was simply less drawdown-prone, not
better-performing in absolute.

## What v5 settles

1. **Selection-rule variable: SETTLED** — neither L/S decile (v4 αt=−2.57)
   nor long-only top-decile (v5 αt=−3.20) extracts alpha on this 10-feature
   stack. Long-only is **WORSE** by α t-stat in absolute terms because v4's
   short leg was at least bleeding off some negative momentum that v5 inherits.

2. **v4 long-leg "+20.6%/y" was an alpha illusion** when measured vs T-bill or
   vs short leg. Vs SPY benchmark it's −45 to −51%/y excess. v4's regime
   stratification (which used L/S spread) hid this because both legs got
   crushed by mega-cap rally.

3. **alt_data_screener_search_2026_04_30 class CLOSED 5/5 FAIL.**
   - v1 (FINRA daily flow): ABANDONED (infra block)
   - v2 (raw return target, top-30): αt=+0.05, FAIL (0/10 coefs)
   - v3 (rank target, top-30): αt=−4.32, FAIL (top-30 inverted on regime)
   - v4 (rank target, decile L/S, SI≤15%): αt=−2.57, FAIL (short squeeze)
   - **v5 (rank target, long-only top-decile vs SPY): αt=−3.20, FAIL (mega-cap rally)**

4. **Bottleneck CONFIRMED: features not selection rule.** Both selection
   ablations (top-30 → decile L/S → long-only) FAIL'd; same 2 nonzero coefs
   throughout. Need fresh feature space (analyst events Path γ failed yfinance
   survivorship → ABORTED; next: options-implied via ThetaData).

5. **Program-level Bonferroni count → n=11.** Next test on this burnt holdout
   needs |t|≥2.83 (Bonferroni adjustment). Capital deploy off-table on this
   window indefinitely.

## What v5 does NOT settle

- Whether v4's `+20.6%/y long-leg` would survive on a non-mega-cap-rally
  regime. Out-of-sample replication would require: (a) different time window
  (post-2026-05+ as it accrues), or (b) different feature universe entirely.
- Whether mega-cap dominance was the SOLE driver. The Lasso β=−0.14 suggests
  long leg WAS slightly market-neutral on average, so the entire excess gap
  was alpha attribution loss to mega-cap factor, not a passive market beta hit.

## Operational note: feature parquet cache built

Cache file: `~/.alphalens/feature_cache/alt_data_features_de036dd57f581d40.parquet`
(19MB, 676,324 rows × 12 cols, hash key from sorted universe + asof_dates +
holding=20 + benchmark=SPY + features_v=alt_data_v3_v4_v5_10feat).

Future iterations on identical (universe, asof, holding) configuration load
in <30s via `pd.read_parquet`. Use `--force-rebuild-features` flag to
invalidate manually if features.py contract changes.

## Next research questions (post-v5, post-v10)

Both Path γ (analyst features) and Path β (long-only) on 10-feature alt_data
stack are now closed. Three open directions per memory + adversarial reviews:

1. **Options-implied feature class (ThetaData $50-75/mo)**: structurally fresh
   feature space, retail-accessible, explicit delisted-chain retention per
   provider research. New class `options_implied_search_2026_05_xx`,
   in-class n=1 → fresh threshold 1.96, but program-level still 2.83+.

2. **Macro-conditional regime gates**: not a new feature class but a layer-2
   selection-gate test on existing 10-feature stack (gates layer is RESEARCH_ONLY
   per CLAUDE.md). Would condition v5 long-only ON regime classifier (e.g.,
   "VIX > 20" or "yield curve inversion"). Risk: same burnt holdout, no fresh
   data.

3. **Wait for fresh OOS data accrual (post-2026-04-30 continuation)**.
   User policy `feedback_no_passive_pivot.md` rejects this. NOT pursued.

Recommended next step: design memo for options-implied feature class
(`v6_options_implied_design_2026_05_01.md`), pre-adversarial-review.
ThetaData has explicit PIT documentation + delisted-chain retention →
addresses both v10 failure modes (yfinance survivorship + look-ahead leak).

## Files

- Audit JSON: `docs/research/alt_data_screener_v5_long_only_audit.json`
- Phase B report: `docs/research/alt_data_screener_v5_long_only_phase_b.md`
- Pre-reg artifact: `docs/research/preregistration/params_alt_data_screener_v5_long_only_2026_05_01.json`
- Driver: `scripts/experiment_alt_data_lasso_long_only_20d.py` (cache-aware)
- Feature cache: `~/.alphalens/feature_cache/alt_data_features_de036dd57f581d40.parquet`
