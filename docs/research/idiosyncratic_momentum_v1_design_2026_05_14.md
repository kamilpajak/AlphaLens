# Idiosyncratic Momentum (Residual Momentum) — Design Memo

**Date:** 2026-05-14
**Status:** LOCKED 2026-05-14 (post-adversarial-review by zen gemini-3-pro-preview continuation_id `24c057f6-d077-4d3c-96d3-fcef84bed3bb`)
**Pre-reg ID:** `idiosyncratic_momentum_2026_05_14_v1`
**Class:** `price_factor_search_2026_04_29` (existing; n=4→5 after registration; strict |t|=2.57 at α=0.05)
**Project doctrine:** |t|≥3.5 binds (k=14→15)

---

## §0. Pre-registration context

### Class identity (honest accounting)

Idiosyncratic momentum (IM) registers under existing `price_factor_search_2026_04_29` class, which currently contains 4 completed paradigm tests, all FAILed:

- `pure_momentum_2026_04_29` (raw 12-1 momentum)
- `pure_contrarian_2026_04_29` (raw reversal)
- `mom_lowvol_combo_2026_04_29` (momentum × low-vol combination)
- `quality_momentum_combo_2026_04_29` (quality × momentum combination)

After IM registration, n=5; strict Bonferroni critical |t| at α=0.05 = 2.57. Project doctrine 3.5 binds regardless.

**Why same class (not fresh):** signal uses identical data input (US equity price history). The transformation (residualize via FF3 rolling regression then cumulate) is a model-class swap on identical features/holdout/selection. Per CLAUDE.md "Burnt-holdout multiplicity compounds — pure model-class swap NIE cleansuje multiplicity." Honest registration in existing class.

**Distinction claim (NOT class-fresh argument):** literature (Blitz et al 2011) treats residual momentum as construct of interest because the residualization isolates firm-specific persistence from time-varying factor exposures. This is a methodological refinement of the raw-momentum signal, not a new mechanism class.

### Project k accounting

k=14 paradigm tests pre-registration. After IM registration, k=15. Project-wide strict Bonferroni at α=0.05/15 = critical |t| = 2.94. Project doctrine 3.5 unchanged.

---

## §1. Hypothesis

Within US large-cap equities (S&P 1500 PIT union), ranking stocks cross-sectionally by **idiosyncratic momentum** — cumulative residual return over months t-12 to t-2 from a 36-month rolling FF3 regression — and going long top-decile equal-weight, rebalanced monthly with 1-month hold, generates **net-of-cost Carhart-4F α with t-stat ≥ 3.5** (project doctrine) on the 2010-2024 sample, with positive α t-stat in each of IS / OOS / FL phases individually, with Newey-West HAC SE at lag=12 (monthly-cadence regression).

---

## §2. Mechanism + literature

**Construction (Blitz-Huij-Martens 2011 "Residual Momentum", *J. Banking & Finance*):**

For each stock and each month, run rolling FF3 regression on past 36 months:
```
r_i,t = α_i + β_M·Mkt-RF + β_S·SMB + β_H·HML + ε_i,t
```

Residual ε_i,t is the firm-specific component. **Idiosyncratic momentum signal** at month t:
```
IM_i,t = (1/σ_36) · Σ_{k=t-12}^{t-2} ε_i,k
```
where σ_36 is the standard deviation of residuals over the same 36-month window (standardisation per Blitz et al).

**Why this might survive when raw momentum doesn't:**

1. Time-varying factor exposures: raw momentum mechanically loads on Mkt/SMB/HML when those factors had positive past returns; if signs reverse in the holding period, momentum portfolios lose. Residualisation removes this mechanical risk source.
2. Volatility reduction: Blitz et al document gross Sharpe 0.48 (residual) vs 0.25 (raw) in 1926-2009 sample — most of the improvement comes from σ reduction, not α increase.
3. Drawdown resilience: Blitz et al show ~40% MaxDD for residual vs >80% for raw in both 1930s and 2000-2009 crashes.

**Why this might NOT survive (anti-thesis from literature):**

1. **Arnott-Beck 2023 (AEA WP)** — residual momentum confounds firm-specific momentum with (a) betting-against-beta exposure and (b) factor momentum. Pure firm-specific component is ~63 bps/month with t=9.20; mechanical FF3 residualisation captures all three. **For our test: Carhart-4F regression EXPLICITLY includes UMD/Mom factor, which absorbs the factor-momentum confound. Mom-factor β likely +0.5 to +1.0 → much of apparent α absorbed.**
2. **CXO Advisory (per Blitz update through 2015)**: "idiosyncratic momentum weakens after the early 2000s."
3. **Talebzadeh-Norouzi 2009-2018 OOS replication**: "no significant difference between residual and conventional momentum strategies."
4. **Research Affiliates live mutual fund analysis**: "the momentum factor has provided no benefit whatever to the end-investor" after costs.
5. **2023-2025 momentum crisis**: worst 8-quarter rolling alpha (-11.3%) in 20+ years. If 2024 is in FL phase, this depresses recent αt.

---

## §3. Universe + data

**Universe:** S&P 1500 PIT union via `load_sp1500_pit_union()` — ~2000 unique tickers across 2009-2026 snapshot vintages. Survivorship-biased fallback (per known CLAUDE.md limitation), documented; expected effect on αt is +0.1 to +0.3.

**Data sources:**
- **Prices**: yfinance daily OHLCV cache at `~/.alphalens/prices/` ✓
- **FF3 factors** (Mkt-RF, SMB, HML, RF): `data.factors.load_carhart_daily` ✓ (also gives Mom for Carhart-4F regression)

No new data acquisition required.

**Eligibility filters (locked):**
- Price floor: close ≥ $5 at signal date (drop penny-stock noise)
- ≥36 months of return history (residualisation window requirement)
- ≥10 non-NaN months in formation window t-12 to t-2

---

## §4. Methodology

**Formation & ranking:**

1. For each month t and each ticker in universe at t:
   - Compute monthly returns for past 36 months t-36 to t-1
   - Run OLS regression on FF3 (Mkt-RF, SMB, HML)
   - Save residuals ε_i,τ for τ ∈ [t-36, t-1]
2. Compute IM_i,t = cumulative residual over [t-12, t-2] / σ_36 (Blitz standardisation)
3. Cross-sectionally rank IM across eligible universe at month t; pick **top-decile**

**Portfolio construction:**
- Equal-weight (no value-weighting; consistent with most academic spec)
- ~200 tickers in top-decile from ~2000 eligible
- Long-only (NO short leg)
- No sector neutralization (vanilla spec; sector neutralization deferred to v2 if v1 PASS_MARGINAL)

**Rebalance cadence:**
- Monthly: refresh signal + portfolio at month-end close
- `--rebalance-stride 21` (21 trading days ≈ 1 month)
- Hold = 21 trading days

**Cost model:**
- Standard 5-cost grid: {0, 5, 10, 15, 25} bps half-spread per project pattern
- Per-rebalance turnover proxy; G4 gate at 15bps net αt ≥ 2.0
- **Turnover hardening (zen 2026-05-14)**: residual momentum has notoriously brutal turnover because residuals are noisy → rank correlation decays instantly. **MANDATORY**: log absolute monthly turnover; cost drag MUST scale with realized turnover, not flat penalty. Audit output includes `mean_monthly_turnover` field for each cost arm. Material-finding flag if monthly turnover > 80% — high cost drag will dominate strategy at 15bps stress arm.

---

## §5. Statistical methodology

**Primary regression spec (Carhart-4F, project-standard, doctrine-binding):**
```
(R_port,t − RF_t) = α + β_M·(Mkt-RF)_t + β_S·SMB_t + β_H·HML_t + β_Mom·Mom_t + ε_t
```

**Cadence:** daily portfolio returns (via existing `BacktestEngine` + daily continuous returns path; standard for project)

**HAC SE:** Newey-West with `maxlags=21` (1 month, matches monthly rebalance cycle)

**Sample:** 2010-01-01 → 2024-12-31

### §5.1 Mandatory BAB-confound diagnostics (per zen adversarial review 2026-05-14)

Blitz canonical standardisation `IM = cumsum(ε) / σ_36` mechanically overweights low-volatility stocks (low σ → high IM after scaling) → injects implicit BAB tilt. Carhart-4F does NOT include a BAB factor. Apparent α could be low-vol premium in disguise.

**Required post-hoc diagnostics, logged in audit output regardless of verdict:**

1. **Portfolio realized β_market**: regress daily portfolio returns on Mkt-RF only (no other factors); log `β_market` value.
   - **Material finding if β_market < 0.8**: BAB confound material; primary Carhart-4F α suspect.
   - **β_market ≈ 1.0**: no low-vol tilt; primary α genuine.

2. **Secondary FF5+UMD attenuation check**:
   ```
   (R_port − RF) = α + β_M·(Mkt-RF) + β_S·SMB + β_H·HML + β_RMW·RMW + β_CMA·CMA + β_Mom·Mom + ε
   ```
   RMW (profitability) and CMA (investment) are documented BAB-correlated. Compare `α_Carhart` vs `α_FF5+UMD`.
   - **Material finding if attenuation > 30%**: BAB / quality confound material; primary Carhart-4F α suspect.
   - **Attenuation < 15%**: residual signal robust to BAB/quality controls.

3. **Sharpe-improvement vs raw momentum (secondary metric)**: also log Sharpe of equivalent raw-momentum portfolio (top-decile by 12-1 cumulative return) over same sample for direct comparison per Blitz primary claim. Not a PASS gate, but anti-pattern documentation.

These diagnostics are NOT authoritative gates (project doctrine binds on Carhart-4F per §8). They are mandatory artifact-logging to enable honest postmortem interpretation.

---

## §6. Phase design (locked)

Three windows per project standard:

| Phase | Window | Purpose |
|---|---|---|
| IS | 2010-01-01 → 2017-12-31 | In-sample design validation |
| OOS | 2018-01-01 → 2021-12-31 | Out-of-sample replication (pre-COVID through partial recovery) |
| FL | 2022-01-01 → 2024-12-31 | Final lock (includes 2023-2024 momentum crisis) |

**Rebalance phase-offset stride sweep:** standard {0, 5, 10, 15} per multi-phase orchestrator pattern.

---

## §7. Pre-reg parameters (locked)

| Parameter | Locked value | Notes |
|---|---|---|
| Universe | S&P 1500 PIT union | survivorship-biased, documented |
| Eligibility floor | close ≥ $5 at signal date | standard |
| Residualisation | FF3, 36-month rolling OLS | Blitz-Huij-Martens canonical |
| Formation window | t-12 to t-2 months | skip-1 standard |
| Standardisation | residual cumsum / σ_36 | Blitz canonical |
| Portfolio | top-decile, equal-weight, long-only | ~200 tickers from ~2000 eligible |
| Rebalance | monthly, stride=21 trading days | matches signal cadence |
| Hold | 21 trading days | 1 month |
| Cost grid | {0, 5, 10, 15, 25} bps half-spread | project standard |
| Regression | Carhart-4F + NW HAC maxlags=21 | daily cadence |
| Phases | IS 2010-2017, OOS 2018-2021, FL 2022-2024 | locked |

---

## §8. Success criteria (Bonferroni-doctrine, pre-registered)

PASS requires **all four**:

1. Net Carhart-4F α t-stat ≥ **3.5** on full sample, Newey-West HAC SE, daily-cadence regression (project doctrine bar at k=15; strict Bonferroni 2.94; doctrine 3.5 self-imposed buffer)
2. Net Carhart-4F α t-stat ≥ **2.5** mean across IS / OOS / FL phases (phase-stability)
3. Net Carhart-4F α t-stat **> 0** in each of IS, OOS, FL individually (no negative-phase rescue)
4. Cost stress: net αt at half-spread = **15bps** remains ≥ **2.0** (cost-mirage prevention)

**PASS_MARGINAL**: passes (3) and (4) but joint αt is in [2.5, 3.5]. Triggers paper-trade observation, NOT capital deploy.

**FAIL**: any of (1)-(4) violated.

---

## §9. Honest priors with literature evidence

**Probability achieving doctrine 3.5 t-stat: 10-15%** (post-Perplexity-research + zen-review estimate, 2026-05-14)

Composition:
- **Pre-cost survival** (Carhart-4F α₃F ignored, look only at gross αt): 40-55% per Blitz et al base evidence
- **Post-cost survival** (gross αt with 5-10bps cost drag, NW HAC): 25-40%
- **Carhart-4F absorption of factor-momentum component**: drops to 15-25%
- **Hyper-turnover (60-80% monthly) cost drag at 15bps stress** (zen flag): subtracts ~5-10pp
- **2023-2024 momentum crisis hit on FL phase**: subtracts ~5pp from above
- **Net post-corrections**: 10-15%

**Most likely outcome**: αt mean across phases ~1.5-2.5, vindicated mechanism but below project doctrine 3.5. **Pattern matches ev_fcff_yield #13 FAIL** (mechanism vindicated by signs, below Bonferroni bar).

**Material findings expected regardless of verdict:**
- Empirical measurement of Mom-factor β in residual-momentum portfolio on S&P 1500 (validates/rejects Arnott BAB-confound hypothesis on our universe)
- 2023-2024 crisis effect on FL phase
- Sharpe improvement vs raw momentum (secondary metric, not authoritative gate)

---

## §10. Risk register

| Risk | P | Severity | Mitigation |
|---|---|---|---|
| Carhart-4F Mom factor absorbs residual signal — but this is CORRECT behavior of the test, not a flaw (zen 2026-05-14): if α survives UMD control, signal is genuine; if absorbed, signal was noisy momentum proxy | 60-70% | EXPECTED | Carhart-4F is the project-doctrine gate by design |
| BAB confound from 1/σ standardisation (Blitz canonical → low-vol tilt; Carhart-4F doesn't control for BAB) | 40-60% | HIGH | §5.1 mandatory diagnostics: realized β_market + FF5+UMD attenuation check |
| Hyper-turnover (60-80% monthly) cost drag dominates at 15bps stress arm | 70-80% | HIGH | §4 mandatory turnover logging; G4 gate catches |
| Class-internal Bonferroni concerns from adversarial review | 30% | MID | Honest registration in existing class n=4→5; project doctrine 3.5 binds anyway |
| Survivorship in S&P 1500 PIT union inflates IS αt | 50% | MID | Documented; consistent with PEAD-14 + ev_fcff_yield posture |
| 2023-2024 momentum crisis crushes FL phase | 70% | MID | Expected — FL phase is the stress test |
| Cost-mirage at 15bps stress arm | 50% | MID | G4 gate catches this directly |
| FF3 residualisation parameter choice (36mo) data-snoops | 20% | MID | Locked per Blitz canonical; no exploration |
| Top-decile vs top-quintile choice data-snoops | 15% | LOW | Locked at top-decile per literature standard |
| Sector concentration in tech / growth | 30% | LOW | No sector neutralization in v1 (documented); v2 candidate if PASS_MARGINAL |

---

## §11. Bonferroni accounting

**Project-level:**
- k=14 (pre-IM-registration: 13 paradigm failures + PEAD-14 in-flight)
- k=15 after IM registration
- Strict Bonferroni at α=0.05/15 = critical |t| = 2.94
- **Project doctrine 3.5 binds** (self-imposed buffer above strict)

**Class-internal (`price_factor_search_2026_04_29`):**
- n=4 (pure_momentum, pure_contrarian, mom_lowvol_combo, quality_momentum_combo — all completed FAIL)
- n=5 after IM registration
- Strict Bonferroni at α=0.05/5 = critical |t| = 2.57
- **Class-internal threshold ≤ project doctrine** → project doctrine binds

**No retroactive impact** on other paradigms (this class has no in-flight tests).

---

## §12. Capital deployment clause

Per project precedent: **no paradigm has standing PASS** after insider_form4 SLIPPAGE-FAIL. IM PASS would be the first.

If IM PASS:
1. **Mandatory slippage stress diagnostic** (per `feedback_slippage_stress_diagnostic_pattern_2026_05_12.md`):
   - Regime-conditional β-amplification × per-phase turnover × Carhart re-regression on NET daily @ H=50bps median half-spread
   - PASS_MARGINAL → SLIPPAGE-FAIL conversion is real risk
2. **Excess-cyclicality screen** vs SPY/IWM benchmark (CLAUDE.md mandatory pre-screen)
3. **Paper-trade observation 6-12 months** (matches v9D + pc_abnormal precedent)
4. Capital deploy only if all above PASS

If IM PASS_MARGINAL: paper-trade observation, NO capital deploy.

---

## §13. Module placement

- Scorer: `alphalens/screeners/idiosyncratic_momentum/` (new dir)
- Adapter: `alphalens/screeners/idiosyncratic_momentum/scorer.py`
- Experiment script: `scripts/experiment_idiosyncratic_momentum.py`
- Status: `__status__ = "ACTIVE"` (during audit), → CLOSED or PAPER_TRADE post-verdict

Distinct from `alphalens/screeners/event_drift/` (PEAD class) and `alphalens/screeners/ev_fcff_yield/` (DCF class).

---

## §14. Pre-audit gates

1. **Smoke profile registration** in `alphalens/preaudit/profiles.py` with 6-month 2018-Q1 smoke window, cap=200, --skip-precheck
2. **Coverage check**: prices + factors EXISTS_NONEMPTY (no new data acquisition)
3. **`alphalens preaudit idiosyncratic_momentum_2026_05_14_v1`** PASS → audit launch
4. **NO AV PIT validation needed** (no new data vendor — uses existing yfinance + FF3 factors)

---

## §15. Sources / citations

1. Blitz, D., Huij, J., Martens, M. (2011). "Residual Momentum." *Journal of Banking & Finance* 35(8): 1949-1956.
2. Blitz, Hanauer, van der Grient (2020). Update via Robeco research papers.
3. Arnott, R., Beck, N., Kalesnik, V. (2023). "What's in the residual momentum strategy?" AEA Conference WP.
4. CXO Advisory: "Idiosyncratic, Pure, or Residual Momentum as a Stock Return Predictor."
5. Talebzadeh, H., Norouzi, M. (2018). Iranian Journal of Finance & Investment Studies — OOS replication 2009-2018.
6. Israel, R., Moskowitz, T., Ross, A., Serban, L. (2017). "Costs Implementing Momentum Strategies." AQR Capital.
7. Arnott et al. Research Affiliates: "The Incredible Shrinking Factor Return."
8. CFA Institute 2026 review: "Momentum Investing: A Stronger, More Resilient Framework."
9. Project precedent: `docs/research/paradigm14_pead_v2_design_2026_05_13.md` (PEAD memo template)
10. Project precedent: `docs/research/ev_fcff_yield_v1_design_2026_05_12.md` (recent failed paradigm)

---

## §16. Post-lock amendments (audit trail)

### A1. Adversarial review applied 2026-05-14

Zen `mcp__zen__chat` (gemini-3-pro-preview, continuation_id `24c057f6-d077-4d3c-96d3-fcef84bed3bb`) verdict: **APPROVE-WITH-CHANGES**. Applied changes:

- **§5.1 new section**: mandatory BAB-confound diagnostics (realized β_market + FF5+UMD attenuation + Sharpe-vs-raw-momentum). Carhart-4F doesn't control for BAB; Blitz 1/σ standardisation injects low-vol tilt.
- **§4 cost model**: explicit turnover logging mandate (residual momentum hyper-turnover 60-80% monthly will dominate at 15bps stress arm).
- **§9 honest prior**: recalibrated 15-25% → 10-15% per zen's analysis (matches ev_fcff_yield #13 pattern).
- **§10 risk register**: reclassified Mom-absorption from HIGH to EXPECTED (correct test behavior, not flaw); added BAB confound HIGH + hyper-turnover HIGH.

Zen also confirmed:
- Class identity registration in `price_factor_search_2026_04_29` (n=4→5) is honest, no Bonferroni evasion.
- Locking Blitz canonical parameters is responsible choice (clean OOS replication, not data snooping).
- §12 capital deployment clause sufficient given insider_form4 SLIPPAGE-FAIL precedent.

Memo is LOCKED for ledger registration.
