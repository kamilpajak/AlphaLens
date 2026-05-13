# EV/FCFF-Yield Value Screener v1 — Design Memo

**Status:** LOCKED (post-adversarial-review)
**Date:** 2026-05-12
**Paradigm test #:** 13
**Layer:** 2 (screener — single-stage, cross-sectional ranking)
**Author:** Kamil Pająk (with adversarial review by zen/gemini-3-pro-preview + perplexity sonar-reasoning-pro on 2026-05-12)
**Vendor dependency:** SimFin Start tier ($25/mo, paid since 2026-05-12)
**Module target:** `alphalens/screeners/ev_fcff_yield/`

---

## 1. Hypothesis (single sentence, falsifiable)

> Ranking R2000 active ex-financials companies by **EV/FCFF yield** (Enterprise Value / Unlevered Free Cash Flow, TTM, point-in-time at filing date) and going long top-decile equal-weight rebalanced quarterly produces net-of-cost Carhart-4F α t-stat ≥ 3.5 (Bonferroni-adjusted) across IS / OOS / FL phases, with positive α t-stat in each phase individually.

The honest framing: this is **a value factor of the FCF-yield family**, tested on the R2000-ex-financials universe with the AlphaLens cost model and phase-robust audit framework. It is **not** a "growth mispricing discovery" mechanism — Perplexity adversarial review (2026-05-12) demonstrated that single-stage reverse Gordon with fixed WACC is mathematically a monotonic transformation of FCF/EV at constant `r`, so ranking by `g_implied` = ranking by FCF/EV. We frame openly and reference published FCF-yield literature.

## 2. Universe

- **Source**: intersection of IWM current snapshot (1,930 active R2000 tickers, `alphalens/data/alt_data/data/iwm_current.yaml`) with SimFin `us-companies` and `us-income-annual` (6,580 + 31,637 rows respectively).
- **Exclusions**:
  - All tickers in SimFin `us-income-banks-annual` (1,908 rows)
  - All tickers in SimFin `us-income-insurance-annual` (595 rows)
  - Sector-coded "Financial Services" in SimFin industries metadata that lack standard income data (the residual ~735 financials beyond banks+insurance)
- **Rationale for financials exclusion**: per Option D decision documented earlier 2026-05-12, banks (NIM, Provision for Loan Losses) and insurance (Premium Income, Loss Reserves) report on fundamentally different P&L templates; standard DCF/FCFF formulas are economically invalid. Fama-French and Asness QMJ base specs exclude financials for the same reason; complexity has *increased* since 1992 (LCR, regulatory capital). Banks reserved for paradigm #14 conditional on #13 PASS.
- **Effective coverage**: ~70-75% of R2000 raw (loss of ~15-20% to financials exclusion, ~10% to missing SimFin data).
- **Stale fundamentals filter**: drop ticker at rebalance if latest available 10-Q `PUBLISH_DATE` > 100 days before `asof_t`.

## 3. Data (PIT discipline)

| Field | Source | PIT key |
|---|---|---|
| OCF | SimFin `us-cashflow-quarterly`, TTM-summed | `PUBLISH_DATE ≤ asof_t − 1 day` |
| Capex | same; `Change in Fixed Assets & Intangibles` (signed) | same |
| Interest Expense | SimFin `us-income-quarterly`, TTM-summed | same |
| Effective tax rate (τ) | SimFin `us-income-annual` latest available, `Income Tax / Pretax Income`, clamped to [0%, 35%] | same |
| Net Debt | SimFin `us-balance-quarterly` latest, `Long Term Debt + Short Term Debt − Cash, Cash Equivalents & Short Term Investments` | same |
| Shares Outstanding | SimFin `us-shareprices-daily` joined, latest as-of `asof_t` | as-of-t |
| Close Price | SimFin `us-shareprices-daily`, last close on or before `asof_t` | as-of-t |
| Sales (TTM, for imputation) | SimFin `us-income-quarterly`, TTM `Revenue` | `PUBLISH_DATE ≤ asof_t − 1 day` |
| 5y FCF margin (median, for imputation) | rolling 20-quarter window from `us-cashflow-quarterly`, `(OCF + Capex)/Revenue` | `PUBLISH_DATE ≤ asof_t − 1 day` |

The 1-day buffer (`PUBLISH_DATE ≤ asof_t − 1 day`) ensures the same trading day as the publication is not used (8-K preliminaries may precede the 10-Q publication; this is the SimFin documented caveat — see `reference_pit_fundamentals_vendors_2026_05_12.md`).

## 4. Signal definition

**Unlevered Free Cash Flow (FCFF) TTM**, per Damodaran convention:

```
FCFF_TTM = OCF_TTM + InterestExpense_TTM × (1 − τ) − Capex_TTM
```

where `Capex_TTM = −Change_in_Fixed_Assets_and_Intangibles_TTM` (SimFin's `Change in Fixed Assets & Intangibles` is signed negative when capex is positive; we flip the sign).

**Enterprise Value (EV)**:

```
EV = MarketCap + NetDebt
   = Price × SharesOutstanding + (LongTermDebt + ShortTermDebt − Cash)
```

**FCFF imputation** if `FCFF_TTM ≤ 0`:

```
FCFF_imputed = Sales_TTM × median(FCF_margin_quarterly, 5y rolling window)
```

If `FCFF_imputed` is also ≤ 0, **drop the ticker** at this rebalance (Gemini-recommended imputation cuts the hard 30% R2000 attrition; only firms with both negative current FCF and negative 5y median FCF margin are excluded — true zombies, not temporarily distressed).

**Yield**:

```
FCFF_yield = FCFF_effective / EV
```

where `FCFF_effective` is `FCFF_TTM` if positive, else `FCFF_imputed` if positive, else ticker is dropped.

**Ranking**: cross-sectional Z-score of `FCFF_yield` per rebalance date. Winsorize at 1% / 99% percentiles before Z-scoring.

## 5. Portfolio construction

| Parameter | Value | Rationale |
|---|---|---|
| Direction | Long-only | R2000 short borrow + squeeze risk uncapped in cost model; Israel-Moskowitz 2014 retail-realism precedent |
| Decile | Top 10% (highest FCFF_yield) | Standard quintile/decile cross-sectional construction |
| Weighting | Equal-weight within decile | Avoids cap-tilt bias, standard for academic-style factor tests |
| Sector neutralization | **None** | Adds free parameters; first-test simplicity > sophistication |
| Rebalance cadence | **Quarterly** — last trading day of February, May, August, November | Filing-aligned (10-Q deadline ~45 days post-quarter); cost-drag concern from both adversarial reviewers; monthly = noise chase per Gemini |
| Position cap | None (decile naturally caps ~150 positions) | |

## 6. Cost model & stress-test

- **Baseline**: AlphaLens `alphalens/attribution/cost_model.py` defaults, R2000 conservative half-spread + impact.
- **Mandatory stress grid (pre-audit)**: re-run final-locked phase result at half-spread = `{0, 5, 10, 15, 25}` bps. Inclusion of zero-cost arm exposes the cost-mirage risk demonstrated 2026-05-12 on `insider_form4_opportunistic` (gross αt = +2.71, net αt = +1.27 at 50bps → cost-mirage).
- **G1 knockout gate**: if net αt at baseline cost is < 2.0 in IS or OOS phase, the test FAILs without proceeding to FL phase (early-stop pattern from `feedback_slippage_stress_diagnostic_pattern_2026_05_12.md`).

## 7. Phase split (3 × 3y per project methodology)

| Phase | Start | End | Size |
|---|---|---|---|
| IS | 2016-08-31 | 2019-08-31 | 36 months |
| OOS | 2019-08-31 | 2022-08-31 | 36 months |
| FL | 2022-08-31 | 2025-08-31 | 36 months |
| post-FL holdout (observational only) | 2025-08-31 | 2026-04-30 | ~8 months |

Earliest SimFin REPORT_DATE = 2016-07-31 per coverage check 2026-05-12. The 2016-08-31 IS start gives a one-month buffer for first quarterly rebalance.

## 8. Success criteria (Bonferroni-adjusted, pre-registered)

PASS requires **all four**:

1. Net Carhart-4F α t-stat ≥ **3.5** on full sample (Bonferroni-adjusted, k = 13 paradigm tests)
2. Net Carhart-4F α t-stat ≥ **2.5** mean across IS / OOS / FL phases (phase-stability)
3. Net Carhart-4F α t-stat **> 0** in each of IS, OOS, FL individually (no negative-phase rescue)
4. Cost stress: net αt at half-spread = 15bps must remain ≥ **2.0** (cost-mirage prevention)

PASS_MARGINAL: passes (3) and (4) but α t-stat is in [2.5, 3.5] joint window. Triggers paper-trade observation, NOT capital deploy.

FAIL: any of (1-4) violated.

## 9. Capital deployment clause

Off-table for #13. PASS → re-evaluate per project capital-deploy doctrine (Layer 4 vol-target overlay assessment required first; cyclicality pre-screen per `feedback_signal_overlay_cyclicality_screen.md` before any overlay design). PASS_MARGINAL → paper-trade only.

## 10. Adversarial review summary (2026-05-12)

| Reviewer | Critical finding | Spec change |
|---|---|---|
| Perplexity (sonar-reasoning-pro) | `g_implied` with fixed WACC = monotonic transformation of FCF/EV → no novelty over FCF-yield | Spec reframed openly as FCF-yield value factor (§1); pure `g_implied` ranking replaced with `EV/FCFF yield` direct |
| Gemini (3-pro-preview) | Fixed `r = rf + 6%` applied to P/FCF (equity) creates leverage bias toward levered firms | Spec switched to **EV/FCFF** (unlevered FCF / enterprise value), neutralizes capital structure without per-firm beta |
| Gemini | Hard drop FCF<0 introduces ~30% selection bias toward mature profitable firms | Imputation via Sales × 5y median FCF margin added (§4); only true zombies excluded |
| Gemini + Perplexity | Monthly rebalance with 30bps R2000 spreads → cost-mirage paradigm #13 risk | Rebalance changed to **quarterly** (filing-aligned); mandatory cost stress grid added (§6) |
| Perplexity | Long-only on R2000 carries 40-60% alpha drag vs L/S equivalent | Acknowledged; α t-stat hurdle (3.5) is achievable per published FCF-yield results (top decile ~16.6%/y US 40y per Perplexity citation [5]) but not generous |

## 11. Anti-patterns explicitly avoided

| Anti-pattern from prior failures | Defense in v1 spec |
|---|---|
| Over-engineering (paradigm #11 drawdown overlay) | Single feature, no composite, no overlay, no sector-neutralization |
| Compound w/o single-layer PASS (paradigm #12 insider_pc) | Pure single-layer screener; compounds (banks separate, growth composite Frame 2) reserved for #14 conditional |
| Monthly turnover cost-mirage (`insider_form4_opportunistic_2026_05_12`) | Quarterly rebalance + mandatory pre-audit cost stress grid |
| Hard zombie drop = quality bias | Imputation retains temporarily-distressed |
| Fixed-WACC equity-DCF = leverage bias | EV/FCFF unlevered |
| Free-parameter sprawl (multi-stage growth) | Single-stage; literature-anchored to FCF-yield |
| Per-firm β estimation noise | Avoided; fixed `r` neutralized via unlevered FCF |

## 12. Pre-registration

- Ledger entry name: `ev_fcff_yield_2026_05_12_v1`
- Ledger location: `phase-robust-backtesting` external dep (per ADR 0006), to be added before any audit run
- Bonferroni denominator: 13 (this is paradigm test #13)
- Pre-reg timestamp: locked at memo lock time, prior to any phase data observation

## 13. Outcome interpretation matrix

| Result | Action |
|---|---|
| PASS (all 4 criteria) | Proceed to Layer 4 overlay eligibility evaluation (vol-target test). Capital deploy off-table until cyclicality pre-screen + overlay test pass per project doctrine. |
| PASS_MARGINAL (αt in 2.5-3.5) | Paper-trade observation for 12 months; document in ledger. Add to "INCONCLUSIVE" catalog like `pc_abnormal` and `v9D`. |
| FAIL via gate (1) — joint αt < 3.5 but per-phase positive | Document as paradigm #13 sub-fail. Frame 2 (composite g_realized − g_implied) becomes candidate #14 with explicit motivation. |
| FAIL via gate (3) — negative-phase | FCF-yield doesn't survive on R2000 ex-fin in our cost model. Class CLOSED with anti-pattern documentation; reverse-DCF/value class as a whole flagged for retirement. |
| FAIL via gate (4) — cost-mirage | Documentation of cost-sensitivity; explore execution alternatives (less aggressive entry, ETF wrapper consideration). |

## 14. Out of scope for v1 (deferred)

- Multi-stage DCF / IBES analyst forecast integration → would unlock Frankel-Lee mispricing methodology, but requires data source not in SimFin Start tier
- Sector neutralization within non-financial universe
- Long-short cross-sectional spread (for statistical hurdle only, not tradable)
- Composite signal (Frame 2: `g_realized_3y − g_implied`) → candidate for #14 conditional on #13 PASS
- Banks via residual income, insurance via embedded value → candidates for #14 / #15 conditional on #13 PASS

---

## Memo lock confirmation

Adversarial review complete on 2026-05-12 (zen continuation_id `73321b6c-b702-40a7-b63d-f4e33221cb08` for Option D/E adjudication; new chat continuation_id `2be4227c-ac8a-4b50-9cab-a5d276669580` for spec v1 → v2 review). Both reviewers' material critiques applied. **Status: LOCKED**. No further spec modification permitted prior to audit run; any post-lock changes require new memo version + Bonferroni count increment.
