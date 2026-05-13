# Post-Earnings Announcement Drift (PEAD) v2 — Design Memo

**Status:** LOCKED (post-adversarial-review, supersedes v1)
**Date:** 2026-05-13
**Paradigm test #:** 14
**Layer:** 2 (screener — event-driven, cross-sectional ranking of announcement surprises)
**Author:** Kamil Pająk
**Adversarial review:** `mcp__zen__chat` (gemini-3-pro-preview, continuation_id `a232f8da-fecf-4699-9b3b-9a3cf0126c16`) + `mcp__perplexity__perplexity_research` (reasoning_effort=high). Both reviewers returned material-change verdicts on v1. All convergent FATAL flaws addressed below.
**Vendor dependency:** Alpha-Vantage `EARNINGS` (free tier, 25 req/day; locally cached) + I-B-E-S cross-validation on 5 anchor events.
**Module target:** `alphalens/screeners/pead/`

---

## 0. v1 → v2 changes (audit trail)

| v1 spec | Fatal flaw | v2 fix |
|---|---|---|
| Entry E[i+1]−2, exit E[i+1]+1 (3-day window around NEXT earnings) | Inverts BT causal logic; requires perfect E[i+1] date forecast 90 days ahead → look-ahead | Entry on first feasible close after E[i] (t+1 for pre-market reports, t+2 for post-market); exit on close t+20 (≈1 calendar month) |
| `surprisePercentage` = (rEPS−eEPS)/\|eEPS\| | Denominator scaling → junk-bias toward low-absolute-EPS firms | Price-scaled surprise `PSS = (rEPS−eEPS) / close_price_at_announcement` |
| Cost-stress baseline 10bps, G1 at 10 | Earnings-window spread widening unaccounted | Stress grid `{0, 5, 10, 15, 25}` bps; G1 knockout at 15bps |
| Empty-day return = 0 (cash) | Zero-variance days corrupt Carhart-4F regression (β collapses, σ_ε warped) | Regression on INVESTED DAYS ONLY; cash return computed separately and reported but excluded from α t-stat |
| OLS t-stat | Overlapping 20-day holds violate IID | Newey-West HAC SE with `lag=20` (matches holding period) |
| AV `estimatedEPS` PIT not validated | Back-revision risk = look-ahead | Mandatory spot-check vs I-B-E-S (or news-archive snapshot) on 5 anchor events 2018-Q1 before audit launch |

## 1. Hypothesis (single sentence, falsifiable)

> Within the S&P 500 universe, ranking firms cross-sectionally by **price-scaled earnings surprise** `PSS = (reportedEPS − estimatedEPS) / close_price_at_announcement` at quarterly announcement E[i], going long-only the top quintile entered at first-feasible close post-announcement and exited at close t+20, produces net-of-cost Carhart-4F α t-stat ≥ **3.5** (Bonferroni-doctrine bar at k=14) jointly, with positive α t-stat in each of IS / OOS / FL phases individually, when regression is computed on invested-days-only with Newey-West HAC standard errors at `lag=20`.

**Mechanism class:** information-processing anomaly (delayed cross-sectional reaction to earnings surprises). Bernard-Thomas (1989) 60-day post-announcement drift. We acknowledge the literature on PEAD attenuation in large-cap post-2010 (Chordia-Subrahmanyam-Tong 2014 ~50% decline post-decimalization; "Rest in Peace" Emerald CFR 2024) — v2 tests whether residual edge survives doctrine-conservative net-cost gates. **Honest prior:** perplexity research adversarial review puts pre-cost survival probability at ~25-40%, net-cost survival at ~5-15%. We test anyway because the literature decline measurements pre-date the 2020-2025 retail-trading regime shift, and direct phase-robust measurement on AlphaLens cost model is the cleanest available evidence.

**Distinct from 13 prior failures.** Not factor-based (no momentum/value/quality), not insider/clustering, not overlay. First test of information-processing class in this project.

## 2. Universe

- **Source**: S&P 500 PIT membership snapshots at `data/sp500_pit/{2018,2020,2022,2024}.yaml` (510 tickers each — current-membership proxy with documented `SP500_FALLBACK` survivorship caveat).
- **Effective membership rule**: at any rebalance date `t`, the eligible set is the snapshot file whose `as_of` is the latest ≤ `t`.
- **Survivorship-bias quantification (v2 addition)**: the snapshots are NOT true PIT — they were generated from current S&P 500 membership at the time of file creation. **Effect on PEAD**: large negative surprises correlate with delisting risk; missing delisted names means missing the worst negative-PEAD-attribution names. For a long-only top-quintile strategy this creates a **null-direction tailwind** (we don't long delisted names anyway), but it inflates the cross-sectional rank cutoff by removing low-quality firms from the comparison set. Net: modest IS αt inflation, estimated ≤ +0.3 t-stat (literature anchors). Acknowledged in §11; success criteria already include sufficient buffer.
- **Intersection filter**: ticker must have non-empty AV `EARNINGS.quarterlyEarnings` history covering the rebalance window AND Lean OHLCV cache coverage for close prices.
- **Eligibility floor (denominator stability)**: drop event if `close_price_at_announcement < $5` (penny-stock filter) or if `|PSS| > 0.20` (20% surprise of share price = data error / corporate action; outside-distribution sanity gate).

## 3. Data (PIT discipline)

| Field | Source | PIT key |
|---|---|---|
| `reportedDate` | AV `EARNINGS.quarterlyEarnings[*].reportedDate` | event timestamp |
| `reportTime` | same (`pre-market` / `post-market`) | entry-timing branch |
| `reportedEPS` | same | reported at announcement |
| `estimatedEPS` | same — consensus snapshot at reporting time | **validated** (see §3.1) |
| Close price t-1 | Lean cache OHLCV (`~/.alphalens/lean/data/`) | as-of t-1 (for denominator) |
| Daily returns t+1..t+20 | Lean cache OHLCV | t-relative to event |

### 3.1 AV PIT validation gate (mandatory pre-audit)

Before audit launch, run `scripts/validate_av_estimated_eps_pit.py` (to be written) on **5 anchor events from 2018-Q1**: AAPL 2018-02-01, JPM 2018-01-12, UNH 2018-01-16, CAT 2018-01-25, RSG 2018-02-08. For each, compare AV `estimatedEPS` against the consensus snapshot from one of:
- I-B-E-S detail (if academic access obtainable)
- WSJ / Bloomberg / Reuters news archive (announcement-day article quoting "analysts expected $X")
- SEC 8-K filing context (occasionally quotes prior guidance)

**Acceptance criterion**: ≥ 4 of 5 anchor events agree to within ±2¢ (or ±1% of consensus). If FAIL → escalate to v3 (alternative data source: SimFin Earnings premium add-on or Polygon).

This is a discrete pass/fail gate **logged in pre-reg ledger entry** as `data_pit_validation_status: {passed,failed,skipped}`. Skipping is not permitted.

## 4. Signal definition

**Price-scaled surprise (PSS) at announcement E[i] for firm `f`:**

```
PSS_f(E[i]) = (reportedEPS_f − estimatedEPS_f) / close_price_f(E[i] − 1 trading day)
```

The denominator is the prior-trading-day close (PIT — known before announcement open). This normalizes surprise to firm price scale, eliminating the v1 junk-bias toward low-absolute-EPS names. PSS is the academic-standard alternative to forecast-dispersion-SUE when analyst dispersion is unavailable (per Livnat-Mendenhall 2006 SUE-vs-PSS comparison; perplexity citation [17] price-scaled surprise validated in modern samples).

**Cross-sectional rank** within the trailing 45-calendar-day window of S&P 500 announcements:

```
rank_f(E[i]) = percentile_rank(PSS_f, {PSS_g : g ∈ universe, reportedDate(g) ∈ [E[i] − 45d, E[i] − 1d]})
```

Excludes future events (strict `<` boundary on the right). 45-day window matches one earnings-season cluster.

**Long candidate flag** (long-only top quintile):

```
long_f(E[i]) = (rank_f(E[i]) ≥ 80th percentile)
            AND (close_price_f(E[i] − 1d) ≥ $5)
            AND (|PSS_f| < 0.20)
```

Flag activates at the first feasible entry post-announcement (§5) and remains active through exit at t+20.

## 5. Portfolio construction

| Parameter | Value | Rationale |
|---|---|---|
| Direction | Long-only | Project doctrine; first-test simplicity |
| Selection | Top quintile (rank ≥ 80) by PSS percentile | Bernard-Thomas quintile-cross-sectional standard; quintile (not decile) → ~100 names per cluster → more diversification, lower per-position concentration |
| Entry timing | `pre-market` report → close of same trading day E[i]; `post-market` report → close of E[i]+1 | Avoids overnight gap; signal observable before close |
| Holding period | 20 trading days from entry (≈1 calendar month) | Captures dominant post-announcement drift per modern literature (Chordia-Subrahmanyam-Tong 2014); shorter than BT-60d to reduce cost burden and overlap-induced autocorrelation |
| Exit timing | Close of entry_day + 20 | Constant; no early-stop, no extension |
| Weighting | Equal-weight across all live positions per day | Standard event-driven cross-sectional |
| Daily portfolio return | `mean({return_f(t) : f ∈ live_positions(t)})`; **NaN if no live positions** | NaN is excluded from regression — see §6 |
| Position cap | None (quintile naturally caps ~100 active flags per cluster) | |

## 6. Cost model & statistical infrastructure

### 6.1 Cost model

- **Baseline**: AlphaLens `alphalens/attribution/cost_model.py` defaults. S&P 500 large-cap half-spreads ~5-8 bps quoted, but earnings-window widening adds +3-7 bps per zen review.
- **Mandatory stress grid (pre-audit)**: re-run final-locked phase at half-spread = `{0, 5, 10, 15, 25}` bps.
- **G1 knockout gate**: if net αt at **15bps** baseline-conservative half-spread is < 2.0 in IS or OOS phase, the test FAILs without proceeding to FL phase. (v1 had G1 at 10bps; v2 raises to 15bps per zen review — accounts for earnings-window spread widening.)

### 6.2 Regression infrastructure (Carhart-4F)

- **Invested-days-only regression**: daily portfolio return is `NaN` (not zero) when no live positions exist. Carhart-4F regression on `return_t ~ α + β_MKT × MKT_t + β_SMB × SMB_t + β_HML × HML_t + β_MOM × MOM_t + ε_t` is fit ONLY over the subset where `return_t` is non-NaN. Reported αt is annualised post-fit.
- **Newey-West HAC**: standard errors via `statsmodels.api.OLS().fit(cov_type='HAC', cov_kwds={'maxlags': 20})`. The `maxlags=20` matches the 20-day holding period (per `feedback_hac_maxlags_units_silent_inflation.md` — express HAC lag in obs-units of the regression input, which here is invested daily observations).
- **Pre-audit smoke check**: confirm `maxlags=20 ≤ n_obs(invested) / 4` per Andrews 1991 rule of thumb; for IS phase ~750 trading days × ~70% invested fraction ≈ 525 obs → maxlags 20 well below 525/4 = 131.

### 6.3 Diagnostic: invested-days fraction

Report `n_invested_days / n_total_days` per phase. If invested fraction < 40% in any phase, flag for spec review (likely indicates cluster definition or eligibility floor issue).

## 7. Phase split (3 × ~3y per project methodology)

| Phase | Start | End | Size |
|---|---|---|---|
| IS | 2018-01-01 | 2020-12-31 | 36 months |
| OOS | 2021-01-01 | 2023-12-31 | 36 months |
| FL | 2024-01-01 | 2026-04-30 | 28 months |
| post-FL holdout (observational only) | 2026-05-01 | T+12mo | observational |

AV `EARNINGS` historical depth verified 2026-05-13 on 5 sector-diverse names: coverage 1996-1998+ (AAPL/JPM/UNH/CAT 1996; RSG 1998), 0 missing `estimatedEPS` post-2018, 32 quarters each.

**COVID phase note (acknowledged in §11):** IS phase (2018-2020) includes Q1-Q2 2020 pandemic shock — extreme earnings surprises and forecast misses. Acceptable test of regime-robustness, but interpret IS αt with awareness that 2020-vintage surprises may be outliers.

## 8. Success criteria (Bonferroni-doctrine, pre-registered)

PASS requires **all five**:

1. Net Carhart-4F α t-stat ≥ **3.5** on full sample, Newey-West HAC SE, invested-days regression (project doctrine bar at k=14; strict Bonferroni for k=14 α=0.05 = 2.91 per `phase_robust_backtesting.ledger.bonferroni_critical_tstat`; 3.5 is project-doctrine self-imposed buffer)
2. Net Carhart-4F α t-stat ≥ **2.5** mean across IS / OOS / FL phases (phase-stability)
3. Net Carhart-4F α t-stat **> 0** in each of IS, OOS, FL individually (no negative-phase rescue)
4. Cost stress: net αt at half-spread = **15bps** remains ≥ **2.0** (cost-mirage prevention; tighter than v1's 10bps per zen review)
5. **AV PIT validation gate** (§3.1) — pass status logged in ledger pre-audit

**PASS_MARGINAL**: passes (3), (4), and (5) but joint αt is in [2.5, 3.5]. Triggers paper-trade observation, NOT capital deploy.

**FAIL**: any of (1)-(5) violated.

## 9. Capital deployment clause

Off-table for #14. PASS → re-evaluate per project capital-deploy doctrine (Layer 4 vol-target overlay assessment required first; cyclicality pre-screen per `feedback_signal_overlay_cyclicality_screen.md` before any overlay design). PASS_MARGINAL → paper-trade only.

## 10. Pre-screening checklist (mandatory before audit launch)

Per `CLAUDE.md` "Pre-audit smoke before any audit > 1h compute":

1. **AV PIT validation script** runs cleanly on 5 anchor events 2018-Q1 (§3.1). Status logged in ledger.
2. **Add `SmokeProfile` to `alphalens/preaudit/profiles.py::SMOKE_PROFILES`** — name `pead_v2_2026_05_13`, cap=200 (S&P 500 large-cap uniform coverage), 1-quarter window.
3. **`alphalens preaudit pead_v2_2026_05_13`** — confirms (i) per-DataDep coverage check against `~/.alphalens/` and (ii) tiny end-to-end smoke subprocess passes.
4. **Excess-cyclicality screen** (`signal_vol_regime.classify_cyclicality_excess`) against SPY benchmark on the IS-phase backfill output. Verdict quoted verbatim into §4 of this LOCKED memo before audit launch — enforced by `tests/test_overlay_design_compliance.py` IF Layer-4 overlay is considered post-PASS.
5. **Two-method input cross-check** per `feedback_bug_correction_mid_flight_discipline.md`: verify event-count and average-daily-position-count by (a) AV-only parsing and (b) end-to-end engine-side count from 1-quarter smoke.
6. **Invested-days-fraction sanity check**: smoke output reports `n_invested / n_total ≥ 0.40` in target window.

## 11. Anti-patterns explicitly avoided

| Anti-pattern from prior failures | Defense in v2 spec |
|---|---|
| Monthly turnover cost-mirage (`insider_form4_opportunistic_2026_05_12`) | Mandatory cost-stress grid + G1 knockout at 15bps net αt ≥ 2.0 + event-driven (no calendar-rebalance churn) |
| Compound w/o single-layer PASS (paradigm #12 insider_pc) | Pure single-layer screener; no compounds, no overlays in v2 |
| Over-engineering (paradigm #11 drawdown overlay) | Single feature (PSS), no composite, no sector-neutralization |
| ML kitchen-sink (Lasso paradigm 6/7) | No ML; literature-anchored cross-sectional rank |
| `surprisePercentage` junk-bias (v1) | Price-scaled surprise; price-floor + |PSS| sanity gate |
| Counter-cyclical × pro-cyclical overlay mismatch | Excess-cyclicality screen mandatory before any overlay consideration (§10.4) |
| Empty-day zero-return regression corruption (v1) | NaN-on-empty + invested-days-only regression |
| OLS t-stat on overlapping holds | Newey-West HAC SE at lag=20 |
| Look-ahead bias from forward-known E[i+1] date (v1) | Canonical post-announcement timing; E[i] is the only event referenced |
| Look-ahead bias from back-revised consensus | AV PIT validation gate vs anchor events |
| Survivorship bias inflating IS αt | Acknowledged + quantified (≤ +0.3 t-stat); success criteria already conservative |
| Time-varying-β hazard from regime-conditioned exposures | Layer-2 only test; Layer-4 deferred to post-PASS |

## 12. Engineering risks (non-blocking for memo lock, must be resolved before audit)

| Risk | Status | Mitigation path |
|---|---|---|
| Engine event-driven scorer adapter (`PEADScorer`) | UNVERIFIED | `alphalens/backtest/engine.py` is cross-sectional-rebalance-shaped. Two paths: (a) per-day "active positions" weights (sparse, daily rebalance); (b) new `PEADScorer` class. Unit-test against 3-stock dummy matrix before runpod launch. |
| AV rate limit (25/day free tier) | KNOWN | Bulk historical pull → ~500 names × 1 req each = 20 days throttled OR upgrade to paid tier ($50/mo). Cache to `~/.alphalens/av_cache/`. |
| Lean OHLCV coverage for full S&P 500 historic | UNVERIFIED | Pre-audit smoke (§10.3) confirms. |
| `reportTime` edge cases | LOW | Documented in spec (§3, §5); pre/post-market timing branch in §5. |
| Newey-West HAC implementation choice | LOW | Use `statsmodels.api.OLS().fit(cov_type='HAC', cov_kwds={'maxlags': 20})`; verify on synthetic AR(1) series in smoke run. |

## 13. Pre-registration

- Ledger entry name: `pead_2026_05_13_v2`
- Ledger location: `phase-robust-backtesting` external dep (per ADR 0006)
- Bonferroni denominator: 14 (this is paradigm test #14)
- Strict Bonferroni at k=14 α=0.05: 2.91 (informational; doctrine bar 3.5 binds)
- Honest prior survival probability per adversarial review: 5-15% net-cost
- Pre-reg timestamp: locked at memo lock time, prior to any phase data observation

## 14. Outcome interpretation matrix

| Result | Action |
|---|---|
| PASS (all 5 criteria) | Proceed to Layer 4 overlay eligibility evaluation (vol-target test). Capital deploy off-table until cyclicality pre-screen + overlay test pass per project doctrine. |
| PASS_MARGINAL (αt in 2.5-3.5 joint) | Paper-trade observation for 12 months; document in ledger. Add to "INCONCLUSIVE" catalog like `pc_abnormal` and `v9D`. |
| FAIL via gate (1) — joint αt < 3.5 but per-phase positive | Document as paradigm #14 sub-fail. Consider Frame-2 candidate #15: forecast-dispersion-standardised SUE (if I-B-E-S access obtainable) or mid-cap (R1000 ex-S&P500) extension. |
| FAIL via gate (3) — negative-phase | PEAD does NOT survive on S&P 500 in our cost model. Class CLOSED with anti-pattern documentation. Consistent with "Rest in Peace" literature. |
| FAIL via gate (4) — cost-mirage | Documentation of cost-sensitivity. Explore execution alternatives (limit-order entry, VWAP execution, ETF wrapper) — but new spec, new Bonferroni count. |
| FAIL via gate (5) — AV PIT validation | Halt audit. Escalate to alternative data source (SimFin / Polygon) and respec as v3. No Bonferroni cost (the validation gate fails before audit run). |

## 15. Out of scope for v2 (deferred)

- Forecast-dispersion-standardised SUE (true Bernard-Thomas SUE) — AV provides single consensus only. Reserved for #15 conditional.
- Mid-cap / R1000 universe extension — first test on S&P 500 (data availability + cost-floor cleanest). R1000 reserved for #15 conditional.
- Sector neutralization within S&P 500 — perplexity flagged risk; v2 spec accepts factor-loading attribution via Carhart-4F. If PASS, sector-attribution diagnostic added post-hoc.
- Long-short cross-sectional spread — for statistical hurdle only, not tradable; deferred.
- Bernard-Thomas 3-day-window-around-next-earnings strategy (v1 hypothesis) — REJECTED per adversarial review; no respec planned (cost-dominated by canonical 20-day hold per §0 reasoning).

---

## Memo lock confirmation

Adversarial review complete on 2026-05-13 (zen continuation_id `a232f8da-fecf-4699-9b3b-9a3cf0126c16` + perplexity reasoning_effort=high). Both reviewers' material critiques applied in v2 (see §0 audit trail). **Status: LOCKED**. No further spec modification permitted prior to audit run; any post-lock changes require new memo version + Bonferroni count increment.

Audit launch is a subsequent session (runpod CPU pod), gated on:
1. AV PIT validation script passes on 5 anchor events (§3.1) — method revised per §16.A2
2. `SmokeProfile` registered and `alphalens preaudit pead_pss_v2_2026_05_13` passes (§10.2-3) — name corrected per §16.A4
3. `PEADScorer` adapter unit-tested against 3-stock dummy matrix (§12) — assertions extended per §16.A3
4. Cost-model audit determines α1 (gross=1) vs α2 (1/n_active) weighting choice (NEW per §16.A5)

---

## 16. Post-lock amendments (audit trail, original spec unchanged)

Amendments below were applied 2026-05-13 (same session as memo lock) following reconnaissance + plan-level adversarial review by `mcp__zen__chat` (gemini-3-pro-preview, continuation_id `a232f8da-fecf-4699-9b3b-9a3cf0126c16`). They correct **ledger metadata and operational details only**; the locked spec (signal, universe, phase split, success criteria) is unchanged. Per project doctrine, only spec changes require new memo version + Bonferroni increment; these are audit-trail corrections that improve operational soundness without re-defining the hypothesis.

### §16.A1 — Pre-reg ledger ID + class corrected
- ORIGINAL §13: `Ledger entry name: pead_2026_05_13_v2; Bonferroni denominator: 14 (this is paradigm test #14)`
- AMENDED: Ledger entry abandoned and re-registered as **`pead_v5_pss_2026_05_13`** under class **`event_drift_search_2026_05_03`**, joining two prior abandoned entries (`event_drift_v3_pead_quality_clean`, `event_drift_v4_pead_quality_sp1500`) for strict n=3 class-internal Bonferroni accounting (critical |t| = 2.39 at α=0.05). Project paradigm-test count k=14 strict threshold = 2.91 unchanged. Project doctrine 3.5 still binds. See `params_frozen.class_bonferroni_caveat` in new ledger entry for full rationale; precedent in `alt_data_screener_v4_2026_05_01.params_frozen.class_bonferroni_caveat`.
- RATIONALE: reconnaissance found two prior PEAD-quality entries in the same mechanism class. The original `pead_information_processing_2026_05_13` class label was a Bonferroni-evasion anti-pattern; v2 differs only in signal construction (PSS vs Foster SUE × accruals) and hold length (20d vs 58d), not in mechanism class.

### §16.A2 — AV PIT validation method revised
- ORIGINAL §3.1: "compare AV `estimatedEPS` against the consensus snapshot from one of: I-B-E-S detail / WSJ-Bloomberg-Reuters news archive / SEC 8-K context"
- AMENDED: Two-step method. (1) Perplexity surfaces contemporaneous-source URLs only — no numeric extraction (zen 2026-05-13: Perplexity hallucinates / regurgitates back-revised aggregator content). (2) Human reads ≥1 URL per event from the surfaced list, extracts consensus, records delta vs AV. Acceptance unchanged at ≥4/5 within ±2¢ or ±1%.
- RATIONALE: LLM numeric extraction unreliable for back-dated consensus values where SEO-indexed modern aggregators dominate search results.

### §16.A3 — Engine adapter unit-test assertions extended
- ORIGINAL §12 (PEADScorer adapter): "unit-test against 3-stock dummy matrix"
- AMENDED: Test suite must include explicit assertions:
  - pre-market event at t → portfolio captures `close(t) → close(t+1)` return on day t+1
  - **post-market event at t → portfolio has ZERO exposure to `close(t) → close(t+1)`; first captured return is `close(t+1) → close(t+2)` on day t+2**
  - index-alignment: no off-by-one between scorer-output DataFrame indices and engine-side date keys
- RATIONALE: zen 2026-05-13 flagged that cross-sectional-rebalance engines commonly assume t-close weights capture t→t+1 returns, which would silently corrupt the post-market-event timing rule.

### §16.A4 — Smoke profile name
- ORIGINAL §10.2: `pead_v2_2026_05_13`
- AMENDED: `pead_pss_v2_2026_05_13` (matches scorer module name `score_pead_pss.py`)
- RATIONALE: naming consistency with corrected module path (§16.A5).

### §16.A5 — Module path + cost-model audit prerequisite
- ORIGINAL §12: `Module target: alphalens/screeners/pead/`
- AMENDED: `Module target: alphalens/screeners/event_drift/` — compose existing primitives (`announcement_dates`, `t0_timing`, `event_window`, `sector_filter`) + add `av_earnings_ingestion.py` + `score_pead_pss.py`. Skip `day1_filter`, `accruals` (v2 spec drops both).
- NEW PREREQ: before implementing the daily portfolio adapter (Option α: gross=1 daily-rebalance with sparse weights), audit `alphalens/attribution/cost_model.py` to confirm forced-deleveraging churn (sell-down of existing positions to fund new entries during peak earnings week) is charged correctly. If yes → keep α1 (memo-literal). If no → α2 (1/n_active sub-leveraged) is required, which IS a material spec change → triggers v3 memo + Bonferroni increment (class n=3→4). Decision logged in ledger entry `outcome.weighting_choice_resolution`.
- RATIONALE: zen 2026-05-13 identified forced-deleveraging churn as a hidden cost hazard (Day-1 50/50 positions sold down to Day-2 10% as 8 new events trigger). Existing `event_drift/` module has reusable primitives — duplicating to `pead/` would have been an anti-pattern.
