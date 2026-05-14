# Post-Earnings Announcement Drift (PEAD) v1 — Design Memo

**Status:** REJECTED (post-adversarial-review 2026-05-13 — superseded by `paradigm14_pead_v2_design_2026_05_13.md`)
**Date:** 2026-05-13

**Rejection summary (2026-05-13):**
- Adversarial review by `mcp__zen__chat` (gemini-3-pro-preview, continuation_id `a232f8da-fecf-4699-9b3b-9a3cf0126c16`) returned verdict `REJECT-AND-RESPEC`.
- Adversarial review by `mcp__perplexity__perplexity_research` (reasoning_effort=high) returned verdict `PROCEED-WITH-SUBSTANTIAL-CHANGES`.
- Convergent FATAL flaws:
  1. Timing mechanic (enter E[i+1]−2, exit E[i+1]+1) inverts Bernard-Thomas causal logic; BT documented drift AFTER announcement, not before. Strategy requires perfect prediction of E[i+1] date 90 days in advance → look-ahead bias.
  2. `surprisePercentage` denominator `|estimatedEPS|` creates structural junk-bias toward low-absolute-EPS names; not a clean information-processing signal.
  3. Cost-stress G1 knockout at 10bps half-spread underestimates earnings-window spread widening + concentrated execution market impact.
  4. Empty-day zero-return convention breaks Carhart-4F regression math (zero variance days corrupt β and σ_ε estimates).
  5. AV `estimatedEPS` PIT integrity not validated against I-B-E-S — back-revision risk = look-ahead bias.
  6. PEAD has been materially attenuated in S&P 500 large-cap post-2010 per Chordia-Subrahmanyam-Tong 2014, Kettell-McInnis-Zhao 2022, "Rest in Peace" Emerald CFR 2024.

**No paradigm-#14 abandonment.** Respec to canonical post-announcement timing window + price-scaled surprise + Newey-West regression. See v2 memo.

---

<details><summary>Original v1 spec (REJECTED — kept for audit trail)</summary>


**Paradigm test #:** 14
**Layer:** 2 (screener — event-driven, cross-sectional ranking of announcement surprises)
**Author:** Kamil Pająk
**Vendor dependency:** Alpha-Vantage `EARNINGS` (free tier, 25 req/day; bulk historical pull cached locally)
**Module target:** `alphalens/screeners/pead/`

---

## 1. Hypothesis (single sentence, falsifiable)

> Within the S&P 500 universe, ranking firms cross-sectionally by `surprisePercentage` at quarterly earnings announcement E[i] and going long-only the top decile during the 3-trading-day window around the NEXT quarterly announcement E[i+1] (entry at close E[i+1]−2, exit at close E[i+1]+1) produces net-of-cost Carhart-4F α t-stat ≥ **3.5** (Bonferroni-doctrine bar at k=14) jointly, with positive α t-stat in each of IS / OOS / FL phases individually.

**Mechanism class:** information-processing anomaly (delayed cross-sectional reaction to earnings surprises). Bernard-Thomas (1989, 1990) documented that ~25-30% of total PEAD concentrates in the 3-day windows around subsequent earnings dates despite those windows being ~5% of trading days. Refined by Kettell-McInnis-Zhao (2022): predictive power compresses to the FIRST subsequent earnings, not 2nd or 3rd.

**Distinct from 13 prior failures.** Not factor-based (no momentum/value/quality), not insider/clustering, not overlay. First test of an information-processing class in this project.

## 2. Universe

- **Source**: S&P 500 PIT membership snapshots at `data/sp500_pit/{2018,2020,2022,2024}.yaml` (510 tickers each — current-membership proxy with documented "SP500_FALLBACK" survivorship caveat).
- **Effective membership rule**: at any rebalance date `t`, the eligible set is the snapshot file whose `as_of` is the latest ≤ `t`.
- **Survivorship-bias caveat**: snapshots are NOT true PIT — they were generated from current S&P 500 membership at the time of file creation. Effect: false-positive bias from delisted names being absent. Acknowledged in §11; quantified post-audit if PASS.
- **Intersection filter**: ticker must have non-empty AV `EARNINGS.quarterlyEarnings` history covering the rebalance window, AND Lean OHLCV cache coverage.
- **Eligibility floor (divide-by-near-zero)**: drop any event where `|estimatedEPS| < $0.10` to avoid pathological `surprisePercentage` blow-ups when consensus is close to zero.

## 3. Data (PIT discipline)

| Field | Source | PIT key |
|---|---|---|
| `reportedDate` | AV `EARNINGS.quarterlyEarnings[*].reportedDate` | event timestamp |
| `reportTime` | same (`pre-market` / `post-market`) | entry-timing adjuster |
| `reportedEPS` | same | reported at announcement |
| `estimatedEPS` | same — consensus snapshot at reporting time | known at announcement |
| `surprisePercentage` | same — `(reported − estimated) / |estimated| × 100` | computed at announcement |
| Close price | Lean cache OHLCV (`~/.alphalens/lean/data/`) | as-of close on event day |

**PIT guarantee:** `surprisePercentage` is computed by AV at announcement time and is fixed in the historical record. Signal at E[i] is observable from E[i]+1 onwards. Execution window `[E[i+1]−2, E[i+1]+1]` lags signal by ~90 calendar days — no look-ahead risk.

**reportTime adjustment:** for `pre-market` releases, t+0 entry is feasible; for `post-market`, the signal is reflected in t+1 close. The execution window is anchored to `reportedDate` of E[i+1] regardless — the 90-day lag absorbs any intra-day timing ambiguity.

## 4. Signal definition

**Surprise ranking at announcement E[i] for firm `f`:**

```
σ_f(E[i]) = surprisePercentage of f's earnings report at E[i]    # provided by AV
```

**Cross-sectional rank** within the trailing-45-calendar-day window of announcements:

```
rank_f(E[i]) = percentile_rank(σ_f, {σ_g : g ∈ universe, reportedDate(g) ∈ [E[i]−45d, E[i]−1d]})
```

The 45-day window matches one earnings-season cluster (US S&P 500 earnings typically span ~6 weeks for a fiscal quarter). Stable cross-sectional context without manual cluster boundaries.

**Long candidate flag** (long-only top decile):

```
long_f(E[i]) = (rank_f(E[i]) ≥ 90th percentile) AND (|estimatedEPS_f| ≥ $0.10)
```

Flag is persistent from E[i]+1 until E[i+1]+1.

## 5. Portfolio construction

| Parameter | Value | Rationale |
|---|---|---|
| Direction | Long-only | Project doctrine; S&P 500 large-cap = shortable but first-test simplicity favors long-only |
| Selection | Top decile by `surprisePercentage` percentile-rank | Bernard-Thomas quintile/decile cross-sectional standard |
| Eligibility floor | `\|estimatedEPS\| ≥ $0.10` | Divide-by-near-zero protection on `surprisePercentage` |
| Entry | Close of trading day E[i+1] − 2 | Pre-announcement positioning per Bernard-Thomas 3-day window |
| Exit | Close of trading day E[i+1] + 1 | Captures post-announcement drift day |
| Holding | 3 trading days per event | Constant; no early-stop, no extension |
| Weighting | Equal-weight across all live positions on the date | Avoids cap-tilt; standard for event-driven cross-sectional |
| Aggregation | Daily portfolio = average return across all events whose execution window includes day `t` | Compounds multiple overlapping events |
| Empty-day handling | If no event-active position on date `t`, daily return = 0 (cash) | Realistic for sparse event days outside earnings season |
| Position cap | None (decile naturally caps ~50 active flags per cluster) | |

## 6. Cost model & stress-test

- **Baseline**: AlphaLens `alphalens/attribution/cost_model.py` defaults. S&P 500 large-cap tight half-spreads (target ~3-8 bps) — substantially below R2000 (~25-40 bps).
- **Critical risk**: high event count → high turnover. Each event = 2 round-trips (enter + exit) in 3 trading days. With ~16,000 events over 8 years → ~4,000 events/year ≈ 16 events/day average → portfolio fully turned over multiple times per quarter.
- **Mandatory stress grid (pre-audit)**: re-run final-locked phase result at half-spread = `{0, 3, 5, 10, 15}` bps. Inclusion of zero-cost arm exposes the cost-mirage risk demonstrated 2026-05-12 on `insider_form4_opportunistic` (gross αt = +2.71 OOS → net +1.27 OOS at 50bps).
- **G1 knockout gate**: if net αt at baseline cost is < 2.0 in IS or OOS phase, the test FAILs without proceeding to FL phase (early-stop pattern from `feedback_slippage_stress_diagnostic_pattern_2026_05_12.md`).

## 7. Phase split (3 × ~3y per project methodology)

| Phase | Start | End | Size |
|---|---|---|---|
| IS | 2018-01-01 | 2020-12-31 | 36 months |
| OOS | 2021-01-01 | 2023-12-31 | 36 months |
| FL | 2024-01-01 | 2026-04-30 | 28 months |
| post-FL holdout (observational only) | 2026-05-01 | T+12mo | observational |

AV `EARNINGS` historical depth (verified 2026-05-13 on 5 sector-diverse names): coverage back to 1996-04-17 for AAPL/JPM/UNH/CAT and 1998-10-29 for RSG — sufficient for 2018-2026 IS/OOS/FL. All 32 post-2018 quarters per name have non-null `estimatedEPS`.

## 8. Success criteria (Bonferroni-doctrine, pre-registered)

PASS requires **all four**:

1. Net Carhart-4F α t-stat ≥ **3.5** on full sample (project Bonferroni-doctrine bar at k=14; strict Bonferroni for k=14 α=0.05 = 2.91 per `phase_robust_backtesting.ledger.bonferroni_critical_tstat`; 3.5 is the project-doctrine self-imposed buffer)
2. Net Carhart-4F α t-stat ≥ **2.5** mean across IS / OOS / FL phases (phase-stability)
3. Net Carhart-4F α t-stat **> 0** in each of IS, OOS, FL individually (no negative-phase rescue)
4. Cost stress: net αt at half-spread = 10bps remains ≥ **2.0** (cost-mirage prevention; lower stress threshold than #13's 15bps reflects S&P 500's tighter spread regime)

**PASS_MARGINAL**: passes (3) and (4) but α t-stat is in [2.5, 3.5] joint window. Triggers paper-trade observation, NOT capital deploy.

**FAIL**: any of (1-4) violated.

## 9. Capital deployment clause

Off-table for #14. PASS → re-evaluate per project capital-deploy doctrine (Layer 4 vol-target overlay assessment required first; cyclicality pre-screen per `feedback_signal_overlay_cyclicality_screen.md` before any overlay design). PASS_MARGINAL → paper-trade only.

## 10. Pre-screening checklist (mandatory before audit launch)

Per `CLAUDE.md` "Pre-audit smoke before any audit > 1h compute":

1. **Add `SmokeProfile` to `alphalens/preaudit/profiles.py::SMOKE_PROFILES`** — name `pead_v1_2026_05_13`, cap=100 (S&P 500 large-cap; coverage uniform), 1-quarter window.
2. **`alphalens preaudit pead_v1_2026_05_13`** — confirms (i) per-DataDep coverage check against `~/.alphalens/`, (ii) tiny end-to-end smoke subprocess passes.
3. **Excess-cyclicality screen** (`signal_vol_regime.classify_cyclicality_excess`) against SPY benchmark on a backfilled toy run of IS phase. Quote verdict verbatim into §4 of LOCKED memo before audit launch — enforced by `tests/test_overlay_design_compliance.py` (applies if any Layer-4 overlay is considered post-PASS).
4. **Two-method input cross-check** per `feedback_bug_correction_mid_flight_discipline.md`: verify event-count and average daily-position-count by (a) AV-only parsing and (b) end-to-end engine-side count from a 1-quarter smoke.

## 11. Anti-patterns explicitly avoided

| Anti-pattern from prior failures | Defense in v1 spec |
|---|---|
| Monthly turnover cost-mirage (`insider_form4_opportunistic_2026_05_12`) | Tight S&P 500 spreads + mandatory cost-stress grid + G1 knockout at 2.0 net αt |
| Compound w/o single-layer PASS (paradigm #12 insider_pc) | Pure single-layer screener; no compounds, no overlays in v1 |
| Over-engineering (paradigm #11 drawdown overlay) | Single feature (surprisePercentage), no composite, no sector-neutralization |
| ML kitchen-sink (Lasso paradigm 6/7 anti-pattern) | No ML; literature-anchored cross-sectional rank |
| Survivor bias (S&P 500 current snapshot) | Acknowledged explicitly; quantification deferred to post-PASS — false-positive risk only |
| Counter-cyclical × pro-cyclical overlay mismatch (`insider_form4` Layer 4 reject) | Excess-cyclicality screen mandatory before any overlay consideration (§10.3) |
| Divide-by-near-zero `surprisePercentage` artifacts | Eligibility floor `\|estimatedEPS\| ≥ $0.10` (§4) |
| Time-varying-β hazard from regime-conditioned exposures | Layer-2 only test; Layer-4 deferred to post-PASS |

## 12. Engineering risks (non-blocking for memo lock, must be resolved before audit)

| Risk | Status | Mitigation path |
|---|---|---|
| Engine event-driven scorer adapter | UNVERIFIED | `alphalens/backtest/engine.py` is cross-sectional-rebalance-shaped (assumes fixed snapshot dates). Event-driven entry/exit needs either (a) a daily-rebalance with sparse weights or (b) a new "PEADScorer" mode. Investigate before audit launch. |
| AV rate limit (25/day free tier) | KNOWN | Bulk historical pull → ~500 names × 1 req each → 20 days of throttled fetches OR upgrade to paid tier ($50/mo). Cache locally to `~/.alphalens/av_cache/`. |
| Lean OHLCV coverage for full S&P 500 historic | UNVERIFIED | Spot-check 5 names confirmed AV coverage; Lean cache coverage parity not yet verified. |
| `reportTime` (pre/post-market) edge cases | LOW | Documented in spec (§3); 90-day lag absorbs ambiguity. |

## 13. Pre-registration

- Ledger entry name: `pead_2026_05_13_v1`
- Ledger location: `phase-robust-backtesting` external dep (per ADR 0006)
- Bonferroni denominator: 14 (this is paradigm test #14)
- Strict Bonferroni at k=14 α=0.05: 2.91 (informational; doctrine bar 3.5 binds)
- Pre-reg timestamp: locked at memo lock time, prior to any phase data observation

## 14. Outcome interpretation matrix

| Result | Action |
|---|---|
| PASS (all 4 criteria) | Proceed to Layer 4 overlay eligibility evaluation (vol-target test). Capital deploy off-table until cyclicality pre-screen + overlay test pass per project doctrine. |
| PASS_MARGINAL (αt in 2.5-3.5 joint) | Paper-trade observation for 12 months; document in ledger. Add to "INCONCLUSIVE" catalog like `pc_abnormal` and `v9D`. |
| FAIL via gate (1) — joint αt < 3.5 but per-phase positive | Document as paradigm #14 sub-fail. Consider Frame-2 candidate #15: price-scaled surprise `(reportedEPS − estimatedEPS) / close_price` as alternative SUE proxy. |
| FAIL via gate (3) — negative-phase | PEAD does NOT survive on S&P 500 in our cost model. Class CLOSED with anti-pattern documentation. |
| FAIL via gate (4) — cost-mirage | Documentation of cost-sensitivity; explore execution alternatives (limit-order entry, ETF wrapper consideration). |

## 15. Out of scope for v1 (deferred)

- Forecast-dispersion-standardised SUE (true SUE per Bernard-Thomas) — AV only provides single consensus, not analyst dispersion. Use `surprisePercentage` proxy.
- Price-scaled surprise `(reportedEPS − estimatedEPS) / close_price` — reserved for Frame 2 / paradigm #15 candidate if v1 fails.
- Mid-cap / R2000 universe — first test on S&P 500 large-cap (tightest spreads, cleanest microstructure). R2000 extension reserved for paradigm #15 conditional on #14 PASS.
- Sector neutralization within S&P 500
- Long-short cross-sectional spread (for statistical hurdle only, not tradable)
- Multi-quarter drift extension (E[i+2], E[i+3]) — Kettell-McInnis-Zhao 2022 shows compression to first subsequent earnings; we follow that finding

---

## Memo lock confirmation

**Pending adversarial review** via `mcp__zen__chat` (gemini-3-pro-preview) + `mcp__perplexity__perplexity_research`. Upon review, material critiques will be applied, status flipped to LOCKED, and the locked memo committed. Audit launch is a subsequent session (runpod CPU pod).

</details>

**Final status:** REJECTED 2026-05-13. Successor: `paradigm14_pead_v2_design_2026_05_13.md`.
