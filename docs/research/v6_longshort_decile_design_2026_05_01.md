# v6 design — long/short decile spread on v3 rank-target Lasso

**Status:** REVISED 2026-05-01 PM after zen + perplexity adversarial review.

**Two critical findings from review (locked into pre-reg):**

1. **Burnt-holdout HARKing risk (zen #3, perplexity #1+#3, MODERATE-HIGH).** v3
   already observed 2024-04-30 → 2026-04-30 holdout and surfaced +0.0260 mean
   rank-IC. v4's hypothesis "decile spread captures that bulk rank-IC" is
   constructed against that specific observation. Pre-reg + n=4 Bonferroni
   |t|≥2.50 is necessary but insufficient. Polygon SI history (2017-12-29
   onward) leaves no genuinely fresh OOS window on this train/holdout split.
   **Mitigation: v4 is DIAGNOSTIC ONLY — capital deploy is OFF-TABLE
   regardless of outcome.** A PASS verdict cannot trigger capital allocation;
   it can only motivate a v5 design with TRULY fresh data (post-2026-04-30
   continuation, or different feature universe).

2. **Hard-to-borrow short leg (zen #2, perplexity #5, FATAL if unaddressed).**
   Bottom-decile by predicted score = high-SI names per v3's `rank_short_
   interest_pct_float = -0.0063` coef sign. Real Russell-2000 borrow fees on
   high-SI names spike to 20–100% annualized (Geczy-Musto-Reed 2002, IHS
   Markit Securities Finance 2020 [10]) and prime brokers can force buy-ins
   under squeeze. The 1.5% mid-range estimate undercounts drag by an order of
   magnitude. **Mitigation: ex-ante SI > 15% exclusion on the short leg
   before decile cut.** Names with `short_interest_pct_float > 0.15` at asof
   are removed from the short-eligible universe. Long leg unfiltered. This
   trades signal purity for borrow-feasibility.

Two zen recommendations NOT adopted (HARKing flexibility if added post-review):

- **Inverse-volatility weighting on short leg** (zen rec #2): would be a NEW
  design variable post-adversarial-review; pre-reg discipline forbids. If v4
  exhibits idiosyncratic short-leg blowups, a separate v5 pre-registration
  with inverse-vol weighting is the correct response.
- **Smaller decile (5% / 20% variants)**: HARKing flexibility. Decile (10%)
  is the canonical academic cut; locked here.

**Class:** Same-class ablation of `alt_data_screener_search_2026_04_30` (n=4 → |t|≥2.50).

## Hypothesis (one-liner)

v3 produced two structurally informative signals: (1) **2/10 nonzero Lasso coefs**
with literature-canonical signs (rank_short_interest_pct_float = −0.0063 matching
DLW 2009; filing_density_4q = +0.0016 matching corporate-event-activity premium);
(2) **+0.0260 mean per-asof Spearman rank-IC on holdout** (model predictions
positively correlated with realised 20d returns across the ~1033-ticker cross-
section). v3's FAIL came from selection-rule magnification: top-30 of ~1033
samples the extreme tail of the prediction distribution where 2024-2026 short-
squeeze regime inverted the DLW direction. **A long/short decile spread (top-103
EW − bottom-103 EW) averages over ~20% of the cross-section per leg, which more
faithfully reproduces the +0.0260 bulk rank-IC the model demonstrably possesses,
without amplifying tail behavior.**

If the bulk rank-IC is real, the decile spread should produce positive realised
alpha t-stat on holdout. If the +0.0260 rank-IC is concentrated entirely in
middle deciles (and top/bottom decile difference is flat or negative), the
decile spread will FAIL too — providing a cleaner diagnostic that the linear
Lasso + 10-feature alt-data combo cannot produce a tradeable cross-sectional
signal at this universe + horizon.

## What changes vs v3

| Variable | v3 | v4 |
|---|---|---|
| Features | 10-feature alt-data whitelist | **same 10 features** |
| Target raw | 20d-forward excess return | **same** |
| Target transform | per-asof percentile rank − 0.5 | **same (rank)** |
| Universe + ADV | AlphaLens PIT, ADV≥$5M | **same** |
| Train | 2018-01-01 → 2024-04-29 | **same** |
| Holdout | 2024-04-30 → 2026-04-30 | **same** |
| Stride / holding / overlap | 5d / 20d / 4-tranche | **same** |
| HAC / Sharpe | maxlags=5 / Lo-2002 | **same** |
| Model class | Lasso L1 (sklearn) | **same Lasso L1** |
| CV | argmin mean fold MSE, 3-fold expanding, 60d embargo | **same** |
| **Selection rule** | top-30 long EW | **top-decile EW long − bottom-decile EW short** |
| **Cost** | 30bps round-trip on long leg | **30bps × 2 legs = 60bps total per rebalance** |
| **Bonferroni n** | 3 → \|t\|≥2.39 | **4 → \|t\|≥2.50** |

ONE strategy variable changes: **the portfolio is market-neutral long/short**.
All training pipeline, all features, all evaluation machinery is identical.

## Detailed specification

### Selection rule (the only change)

For each holdout asof:

1. Score every ticker in the post-ADV-filtered cross-section using v3's rank-
   trained Lasso (loaded directly via `target_transform='rank'`).
2. **Compute long-eligible universe** = full post-ADV cross-section. Sort by
   predicted score descending. **Long leg = top decile (10%, decile size =
   `floor(n_long_eligible / 10)`)**. Equal-weight.
3. **Compute short-eligible universe** = post-ADV cross-section, FURTHER
   filtered to remove names with `short_interest_pct_float > 0.15` at asof
   (zen-recommended HTB safeguard). Sort by predicted score ascending.
   **Short leg = bottom decile (10% of short-eligible) by score**. Equal-weight.
4. Per-asof return = mean(long-leg 20d-forward returns) − mean(short-leg
   20d-forward returns).
5. **Cost drag** = `2 × half_spread × turnover_per_rebal` (factor 2 because
   both legs pay round-trip cost; turnover statistic computed per-leg).
6. **Borrow fee drag (separate accounting)**: 1.5% annualized on short-leg
   notional reported alongside primary cost. NOTE: v4 acknowledges this may
   still under-state real borrow on tail high-SI names even after the 15%
   filter; reported AS-IS in honest verdict notes, NOT used to gate.

Asof slices with cross-section < 30 (decile size < 3) on EITHER leg are
excluded from the holdout return series.

### Why ex-ante SI > 15% filter on short leg

Without the filter, the short leg would concentrate in the bottom-decile-of-
score AND high-SI subset — exactly the names that are physically hardest to
short. The Lasso coefficient `rank_short_interest_pct_float = -0.0063`
guarantees that low-SI names score HIGH (long-leg eligible) and high-SI names
score LOW (would naturally land in the short leg under a no-filter rule). The
filter says: even if the model wants to short these, we can't physically do
it without prime broker friction; remove them from short-eligible universe.

This DAMPENS the expected signal magnitude (some genuine short-side alpha
lives in the high-SI tail, per DLW 2009). It is a deliberate trade of signal
purity for tradeable validity. The filter is locked at 15% per the
short-anomaly literature convention (Drake-Rees-Swanson 2011 cite 10–15% as
the boundary of conventionally short-able SI %).

### Why this might capture the bulk signal

The v3 holdout per-asof rank-IC = +0.0260 is the average Spearman correlation
between predictions and forward returns ACROSS THE FULL CROSS-SECTION. Decile
spreads are a standard estimator of cross-sectional ordering signal:
- A monotone signal across deciles gives long/short spread ≈ 5–10× the bulk IC
  (depending on signal-to-noise concentration).
- A signal concentrated in middle deciles gives flat decile spread.
- A signal concentrated in extreme tails (v3's top-30 case) is captured but
  averaged with adjacent deciles, dampening regime-flip damage.

For a +0.0260 bulk Spearman rank-IC on n_asof≈1000 with an assumed monotone
relationship, the expected long/short decile annualized return spread is
roughly: `2 × IC × σ_returns × sqrt(252/holding) × scaling ≈ 5-12%` (rough
back-of-envelope using `Grinold-Kahn IC-IR-breadth heuristic` adapted to
cross-sectional decile spreads). This is ENOUGH to clear `α_ann ≥ 3%` after
60bps cost drag, IF the signal is monotone across deciles.

If middle deciles drive the +0.0260 (high ranks of moderate-strength), decile
spread shrinks proportionally and may even invert. This is the falsifiable
prediction.

### Why this might NOT capture the bulk signal

Three failure modes preserve v3's FAIL:

1. **Middle-decile concentration.** +0.0260 rank-IC could mean middle-quintile
   stocks (decile 4-7) drive the correlation. Top/bottom decile spread = 0.
2. **Asymmetric tail behavior preserved at decile size.** The 2024-2026 meme/AI
   regime may have caused both top AND bottom deciles to underperform the
   middle (tail wags both directions). Long/short = 0 or negative.
3. **Cost overshoots signal.** 60bps round-trip on doubled turnover (~140% per
   rebalance per leg from prior ranking volatility) ≈ 84bps drag per rebalance,
   ~10% drag annualized. Net signal may go negative even if gross is +5–12%.

The IR gate (in-CV mean fold rank-IC / std fold rank-IC ≥ 0.5 MID) is RETAINED
unchanged from v3. v3's IR was 0.338 < 0.5 — fold instability concern is real
and orthogonal to the selection-rule change. **The v4 verdict is honest about
needing IR ≥ 0.5 to clear MID, regardless of how the L/S spread looks.**

### Cost model (doubled vs v3)

Per-rebalance drag bps:
```
drag_per_leg_bps = primary_period_drag_bps(half_spread=10, turnover=t_leg)
total_drag_per_rebal_bps = 2 * drag_per_leg_bps  # both legs pay round-trip
```

Turnover per leg measured using the same `_turnover_per_rebal` helper as v3,
applied separately to (long_top_lists, short_bottom_lists). Total per-rebal
drag is 2× the per-leg drag.

This is conservative — true L/S costs may be slightly higher (short borrow fees
not modeled). Borrow fees are typically 0.3–3% annualized for Russell 2000
names; assume worst-case mid-range 1.5% annualized in honest reporting (NOT
deducted from headline drag, but flagged in verdict notes).

## Pass / fail criteria (in-class n=4 → |t|≥2.50)

| Gate | Pass-rule | Source |
|---|---|---|
| Carhart-4F α t-stat (HAC=5) on L/S returns | ≥ 2.50 | Bonferroni n=4 |
| Lo-2002 Sharpe (net) on L/S returns | ≥ 0.5 | same as v3 |
| α annualized (gross) | ≥ 3% | same as v3 |
| MaxDD (net) on L/S cum return | ≥ −35% | same as v3 |
| ≥1 nonzero coef | ≥ 1 / 10 | retained (v3's 2 satisfies; v4 reuses fit) |
| In-CV IR (mean fold IC / std fold IC) ≥ 1.0 | NEW retained from v3 | catches noise-overfit λ |
| Holdout mean per-asof rank-IC > 0 | NEW retained from v3 | already +0.0260 in v3 |
| **NEW: market neutrality** | abs(L/S Mkt-RF beta from Carhart-4F regression) ≤ 0.20 | sanity for L/S construct |
| MID rule | α t ≥ 1.5 AND Sharpe ≥ 0.3 AND ≥1 coef AND IR ≥ 0.5 AND holdout IC > 0 AND \|β_mkt\| ≤ 0.20 | refine + re-pre-register |
| FAIL rule | anything else | log to ledger, document |
| Phase C (multi-phase) | only on PASS or MID | per pre-reg `phase_robustness_followup` |

The **market-neutrality gate** is a sanity check for the L/S construct: if
realised L/S Mkt-RF beta is materially nonzero, the spread is loaded on market
risk and the `alpha` is not pure cross-sectional skill. Threshold |β| ≤ 0.20
permits residual noise from imperfect cross-section coverage but rejects
constructs that accidentally trade beta.

## Adversarial review summary

**Stage 1 (zen / gemini-3-pro-preview, thinking_mode=high) — COMPLETED 2026-05-01.**
Findings (most severe first):
1. **Rank-to-return asymmetry trap (FATAL flagged).** Rank-target Lasso is
   blind to magnitudes; a Spearman IC of +0.0260 can survive 1 meme +300%
   short-squeeze in the bottom decile because 99/100 ordinals stay correct.
   The L/S decile portfolio TRADES the un-transformed return tails the model
   was designed to ignore. Short leg has unbounded downside.
   **Response:** ACKNOWLEDGED as inherent risk of rank-IC → spread translation.
   Mitigated by SI > 15% short filter (removes the worst HTB squeezers).
   FAIL of v4 under tail asymmetry is a CLEAN diagnostic outcome, not a
   design flaw — the verdict prose will name this mechanism explicitly if
   bottom-leg blows up.
2. **Hard-to-borrow short leg (FATAL flagged).** ADOPTED via SI > 15% filter.
3. **Holdout leakage / HARKing (severe).** ACKNOWLEDGED in pre-reg as
   "diagnostic only — capital deploy off-table regardless of outcome."
   Cannot be fully cleansed without fresh OOS data.
4. **Ex-post market neutrality gate (moderate).** ACKNOWLEDGED. Gate retained
   as descriptive sanity, NOT as deploy-determining. If realised |β| > 0.20
   the verdict prose will note structural beta loading; doesn't gate FAIL.

**Stage 2 (perplexity / sonar-reasoning-pro, search_context_size=high) — COMPLETED 2026-05-01.**
Findings (numbered):
1. **HARKing despite pre-reg (MODERATE-HIGH).** Same as zen #3. Reviewers
   would expect: timestamped registration BEFORE v3 holdout run (impossible —
   v4 follows v3); explicit regime-shift documentation pre-dating v4
   (impossible same reason). MITIGATION: explicit "diagnostic only" framing
   + capital deploy off-table.
2. **Bonferroni n=4 inadequate if experiments share regime (MODERATE).**
   v1-v4 all train through 2024-04-29 in this class. Hierarchical adjustment
   not adopted — class-conditional Bonferroni is project framework canon
   (per `feedback_keep_searching_screeners.md` + ADR 0007).
3. **IC-to-alpha mapping not pre-specified (HIGH).** Grinold-Kahn heuristic
   was implicit in v3's selection rule (top-30 long); v4 ALTERS the IC-to-
   alpha translator (top-30 → decile L/S) which IS pre-specified in this
   memo + JSON. ACCEPTED — the pre-reg makes the mapping explicit before run.
4. **Modest rank-IC may not support decile economics (HIGH).** Cited [2]
   feature crosstalk inflating IC; [7] tail alpha fragility; [10] short-leg
   borrow asymmetry. ACKNOWLEDGED as failure mode. v4's success requires
   that +0.0260 bulk IC is NOT entirely a feature-crosstalk artifact (e.g.
   short-interest + filing-density both proxying for "investor attention").
   FAIL outcome under crosstalk is the same diagnostic value as outright
   bulk-signal-too-thin.
5. **Borrow cost under-estimated (MODERATE).** PARTIALLY addressed by SI > 15%
   filter; remaining borrow fee on filtered short leg estimated 1.5% mid-
   range Russell-2000.
6. **Bootstrap decile-spread significance recommended.** DEFERRED to Phase D
   if Phase B clears. Adds compute without changing Phase B verdict logic.

**Pre-registration honest scope (locked):** v4 is a DIAGNOSTIC test of "does
v3's bulk +0.0260 rank-IC translate to L/S decile spread alpha after
ex-ante HTB filtering and 60bps cost drag." PASS verdict triggers a v5
re-pre-registration on TRULY fresh data, NOT capital allocation. FAIL or MID
verdict informs whether next class should pivot to LightGBM (option B) or
fundamentally different feature space (option E). The methodological value
of v4 is the same regardless of outcome.

## Implementation plan

### Code changes (small)

1. **`scripts/experiment_alt_data_lasso_longshort_20d.py`** (new driver):
   - Clone of `scripts/experiment_alt_data_lasso_rankic_20d.py`.
   - Replace `_holdout_portfolio_returns_20d` with new function
     `_holdout_longshort_decile_returns_20d`:
     - Long-eligible universe = full post-ADV cross-section.
     - **Short-eligible universe = post-ADV ∩ {SI % float ≤ 15%} at asof**
       (per-asof check using the `short_interest_pct_float` raw column on
       the holdout feature frame).
     - decile_long_size = floor(n_long / 10).
     - decile_short_size = floor(n_short / 10).
     - Long top-decile EW; short bottom-decile EW.
     - Per-asof return = long_mean - short_mean.
     - Per-asof turnover_long, turnover_short tracked separately.
     - Returns also (long_lists, short_lists, long_returns, short_returns)
       for regime-stratified post-hoc analysis.
   - Update `_assess` to:
     - Apply `2 × per-leg drag` cost from the doubled turnover.
     - Compute Mkt-RF beta from Carhart 4F regression (sanity, |β| ≤ 0.20
       descriptive gate).
     - Add separate borrow-fee drag of 1.5% annualized on short notional
       (reported, NOT subtracted from headline α to avoid double-counting if
       prime broker rebates exceed expectations).
   - Update verdict thresholds: PASS_T = 2.50 (was 2.39).
   - Output path: `docs/research/alt_data_screener_v4_phase_b.md`.
   - Audit JSON: `docs/research/alt_data_screener_v4_audit.json`.
2. **No model.py changes:** v4 calls `fit_global(target_transform='rank')`
   identically to v3.
3. **Tests:** new `tests/test_longshort_decile.py` covering:
   - Decile size computation (floor / 10) for asymmetric long/short universes.
   - SI > 15% short-leg exclusion (per-asof boundary check).
   - Per-asof return = long_mean − short_mean.
   - Edge cases: short-eligible universe < 30 (skip asof), all-NaN forward
     returns, score ties on the decile boundary, all-SI-above-15% asof
     (short leg empty → skip).

### Phase A smoke

Same approach as v3: `--smoke` with 5 mega-cap names, but for v4 the smoke is
PURELY plumbing — 5 tickers cannot form a meaningful decile spread. Smoke
verifies:
- fit succeeds with `target_transform='rank'`
- L/S decile builder runs without exception (with n_asof=5, decile_size = 0
  → graceful skip)
- Cost model and Carhart regression code paths execute
- Verdict logic emits a defensible string (not crash)

Phase A does NOT pre-judge holdout. Phase B is the real test.

### Phase B production

Reuse cached PIT data (Polygon SI, companyfacts, prices). Just rerun with new
script. No additional bulk fetches needed. Cross-section ~1033 tickers/asof
expected → decile size ~103 names per leg.

### Phase C (multi-phase) — only if Phase B PASS or MID

5 phase offsets per pre-reg. Same multi-phase machinery as v3. Each phase
re-runs train/holdout split, re-fits Lasso, re-builds L/S spread, re-computes
Carhart-4F. Mean alpha t ≥ 2.50 across 5 phases for PASS.

### Phase D (permutation null) — DEFERRED to Phase C if MID/PASS

Optional permutation test: shuffle ranks within each asof in the train pool,
refit Lasso 100 times, compute holdout L/S alpha for each. Verify chosen-fit
holdout alpha > 95% percentile of permuted distribution. Adds ~2500 fits;
implement only if Phase C clears.

## Honest expectation (revised post-adversarial-review)

Burnt-holdout HARKing concern means **v4 is diagnostic only**. PASS does NOT
trigger capital allocation regardless of t-stat magnitude; it triggers a v5
re-pre-registration on truly fresh data. The expected-outcome distribution
also shifts: short-leg HTB filter dampens upside, asymmetric tail behavior
flagged by zen as a real failure mode increases FAIL probability.

**Scenario 1 (P ≈ 25%): MID — bulk signal partially captured.** Decile spread
α t-stat in 1.5–2.5 range. Sharpe net 0.3–0.5. IR ≥ 0.5 if v3's IR estimate
generalizes. Outcome: v5 candidate with inverse-vol short-leg weighting OR
fresh-data continuation post-2026-04-30.

**Scenario 2 (P ≈ 35%): FAIL — middle-decile signal.** Decile spread α t-stat
near 0. Bulk +0.0260 driven by middle deciles, not extremes. OR feature
crosstalk inflated the rank-IC. Diagnostic: linear-Lasso + 10-feature
alt-data ceiling reached. Next class: LightGBM (option B) OR different
feature space (option E).

**Scenario 3 (P ≈ 25%): FAIL — short-leg tail blowup.** Top decile gains
modest alpha, bottom decile (post-SI-filter) gets hit by 2024-2026 idiosyncratic
squeezes the rank-target Lasso couldn't see (zen Objection 1: rank-blindness
to magnitudes). v4 verdict prose names this mechanism explicitly.
Diagnostic: rank-IC → spread translation requires magnitude-aware loss
(asymmetric loss, robust regression, or quantile target). Next class: option
B (LightGBM with quantile loss).

**Scenario 4 (P ≈ 12%): FAIL — symmetric tail decay.** Top-decile AND
bottom-decile both underperform middle. 2024-2026 meme/AI regime had unusual
cross-sectional structure where any decile signal struggled. Diagnostic:
2024-2026 specifically not a stationary cross-section for this feature class.

**Scenario 5 (P ≈ 3%): PASS — bulk signal captured cleanly.** Decile spread
α t-stat ≥ 2.50, Sharpe ≥ 0.5, |β| ≤ 0.20, IR ≥ 1.0. v4 advances to
multi-phase audit Phase C; if it survives, v5 with truly fresh data (post-
2026-04-30 continuation as it accrues) is the deploy gate.

The honest weighting is FAIL >> MID > PASS. The reason to run v4 anyway is
that the diagnostic information value is high under EVERY scenario, and the
incremental compute cost is minor (reuses v3 cached fits + driver
modifications < 1 day to implement).

## Architectural variable status (post-v4 outcomes)

- Architecture: 4-regime vs single-global SETTLED (prior class v1+v2 same)
- Horizon: 5d vs 20d SETTLED (prior class v2+v3 same)
- Feature content: 21 v1-v3 vs 10 v4-v3 SETTLED (both fail in cap-weighted)
- Target shape: raw-return vs rank-percentile SETTLED (rank correct architecture per v3)
- **Selection rule: top-N long vs decile L/S** — v4 settles this
- Model class: linear-Lasso vs nonlinear (LightGBM/XGBoost) UNTESTED
- Universe: ADV≥$5M vs sub-universes UNTESTED
