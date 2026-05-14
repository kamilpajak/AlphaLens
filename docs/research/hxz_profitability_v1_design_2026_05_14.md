# HXZ Profitability (Cash-Based Operating Profitability) — Design Memo

**Date:** 2026-05-14
**Status:** **DRAFT** (pre-lock, pre-adversarial-review)
**Pre-reg ID:** `hxz_cash_op_2026_05_14_v1`
**Class:** `hxz_profitability_2026_05_14` (NEW; n=1 first test in class; class-internal critical |t|=1.96 at α=0.05)
**Project doctrine:** |t|≥3.5 binds (k=15→16)

---

## §0. Pre-registration context

### Class identity (honest accounting)

This paradigm registers under a NEW class `hxz_profitability_2026_05_14`. No prior tests in the class. Class-internal Bonferroni at α=0.05 with n=1 → critical |t|=1.96. Project doctrine 3.5 binds (k=15→16 with this registration; strict project Bonferroni 0.05/16 → critical |t|=2.95). Doctrine 3.5 self-imposed conservative buffer.

**Why fresh class:**
- Different signal-data axis from `price_factor_search_2026_04_29` (n=5, all price-derived: pure mom, contrarian, mom×lowvol, qual×mom, idio mom) — HXZ profitability uses fundamentals (operating cash flow), not price history.
- Different signal-data axis from `fundamental_value_dcf_2026_05_12` (n=1, FCFF yield) — HXZ measures **profitability** as cash flow / assets (returns-on-capital framing), not **valuation** as cash flow / EV (price framing). Different mechanism; literature treats them as orthogonal.
- Different from event-driven (`event_drift_search`, `insider_form4_opportunistic`) — pure cross-sectional ranking on stable fundamentals.

**Honest registration**: class is NEW because the signal is a distinct mechanism class (profitability/efficiency-of-capital), not a fresh-class-by-evasion. Per CLAUDE.md "Burnt-holdout multiplicity compounds" — model-class swap on identical data inputs would NOT cleanse multiplicity, but profitability ratios use DIFFERENT input variables (operating cash flow, total assets) than ev_fcff_yield's FCFF + EV. Honest fresh class.

### Project k accounting

k=15 pre-registration. After HXZ registration, k=16. Project-wide strict Bonferroni at α=0.05/16 → critical |t|=2.95. Project doctrine 3.5 unchanged.

### True PIT mandate (post-CLAUDE.md doctrine 2026-05-14)

This is the FIRST paradigm to register under the new "True PIT universe mandatory dla paradigmów >100 tickers" doctrine (PR #127). Universe construction MUST intersect snapshot rosters with delisted-ticker augmentation from `~/.alphalens/survivorship/`. Section §3 specifies the implementation.

---

## §1. Hypothesis

Within US large-cap equities (S&P 1500 PIT-augmented union, ex-financials), ranking firms cross-sectionally by **Cash-Based Operating Profitability** — defined as ``CBOP = OCF_TTM / Average_Total_Assets`` with PIT availability lag of 1 quarter — and going long top-decile equal-weight, rebalanced quarterly with 1-quarter hold, generates **net-of-cost Carhart-4F α with t-stat ≥ 3.5** (project doctrine) on the 2016-2024 sample. Joint-PASS requires positive αt in each of IS / OOS / FL phases individually, with Newey-West HAC SE at maxlags=126 (daily-cadence regression).

---

## §2. Mechanism + literature

**Profitability anomaly canonical evidence:**

- **Novy-Marx 2013 JFE "The Other Side of Value"** — gross profitability (Revenue − COGS) / Total Assets predicts cross-sectional returns with αt ~4 on US 1962-2010. Original spec.
- **Hou-Xue-Zhang 2015 JF "Digesting Anomalies: An Investment Approach"** — q-factor model with ROE (Net Income / Book Equity) as profitability factor, annual rebalance.
- **Ball-Gerakos-Linnainmaa-Nikolaev 2016 JFE "Accruals, Cash Flows, and Operating Profitability in the Cross Section of Stock Returns"** — Cash-based Operating Profitability (CBOP = OCF / Total Assets) dominates Novy-Marx GP/A and HXZ ROE in post-2002 OOS tests. CBOP avoids accruals manipulation that distorts net-income measures.
- **Asness-Frazzini-Pedersen 2019 JoF (QMJ paper)** — composite quality (profitability + growth + safety + payout) Sharpe 0.8-1.1.
- **Hou-Mo-Xue-Zhang 2020 RAS "An Augmented q-Factor Model"** — q5 extension adds expected growth factor; profitability factor retains αt 1.8-2.3 t-stat in updated 2015-2019 OOS audits.

**Why CBOP v1 (not gross profitability or ROE):**

1. **Accruals-clean signal.** Cash flow is harder to manipulate than reported net income (Sloan 1996 documented; CBOP literature builds on it). On post-2002 data (post-Sarbanes-Oxley earnings-quality cleanup), CBOP retains αt where ROE has decayed.
2. **Aligns with data availability.** SimFin Start tier (already in cache from paradigm #13) reports OCF directly; no need for derived measure with accruals adjustments.
3. **Less correlated with value tilt.** Novy-Marx GP uses revenue-side measure, which correlates with value premium. CBOP separates better from Carhart-4F controls (especially HML).

**Why this might survive when prior paradigms haven't:**

1. **Fundamental fact (not behavioral or arbitrage-prone).** CBOP measures actual cash-generation efficiency. Less subject to time-varying sentiment than momentum or event-driven signals.
2. **Slow signal → low turnover.** Quarterly rebalance + persistent ranking (firms maintain profitability rankings across quarters) → expect 10-15%/quarter turnover (~40%/year), giving G4 cost stress significant safety margin.
3. **Post-publication evidence stronger than QMJ.** Perplexity research 2026-05-14: HXZ profitability factor exhibits 25-35% LESS decay than QMJ in 2019-2024 OOS replications.

**Why this might NOT survive (anti-thesis from literature):**

1. **Post-publication erosion.** Even CBOP shows decay from 2002-2010 (αt ~3-4) to 2015-2024 (αt ~1.8-2.3 long-short academic). Long-only retail variant degrades further.
2. **Mega-cap tech anomaly.** Modern mega-cap growth stocks (AAPL, GOOG, MSFT) have HIGH profitability AND positive returns — so profitability factor may have become a "mega-cap growth tilt" rather than a cross-sectional discriminator within mid-cap segment.
3. **Survivorship bias inflation (per Perplexity).** Literature alphas based on snapshot universes are 20-40 bps/y inflated; subtract from prior.
4. **Long-only top-decile retains only 40-55% of L/S alpha** per Perplexity research.
5. **Industry concentration.** Top-decile by CBOP tends to be tech/healthcare-heavy; may have sector tilt that absorbs cross-sectional alpha into industry factor.

---

## §3. Universe + data

### Universe (true PIT, per 2026-05-14 doctrine)

**Construction** — at each rebalance date t:

1. Load S&P 1500 PIT snapshot rosters for the latest snapshot date ≤ t from `data/universes/sp{500,400,600}_pit/{YYYY-MM-DD}.yaml`
2. Augment with delisted tickers from `~/.alphalens/survivorship/{delisted_2007_2018,delisted_2021_2026}.parquet` whose `delisted_date > t` (still active at rebalance time)
3. Intersect with SimFin `us-income-annual` coverage (implicit financials exclusion; SimFin separates `us-income-banks-annual` and `us-income-insurance-annual` which we do NOT include — same Option-D decision as paradigm #13)
4. Apply price floor: close ≥ $5 at signal date (drop penny-stock noise)
5. Apply CBOP-computability filter: need both OCF_TTM and Total_Assets non-null, PUBLISH_DATE ≤ t-1 day

**Expected universe size:** ~1200-1500 tickers per rebalance (S&P 1500 ex-fin ∩ SimFin coverage ∩ price floor). Delisted augmentation adds ~50-150 tickers per rebalance for early quarters.

**Implementation requirement** (deferred to engineering phase):
- New function `load_sp1500_pit_for_date_augmented(asof, include_delisted=True) → list[str]` in `alphalens/data/universes/sp1500_pit.py`
- Augmentation logic: scan both delisted parquets, filter by `delisted_date > asof`, union with snapshot roster, dedupe
- Delisted-ticker OHLCV: existing `~/.alphalens/survivorship/lean_data/` (biotech subset) + on-demand yfinance refetch path for non-biotech; written to `~/.alphalens/prices/<ticker>.parquet` for engine consumption
- CI test: assert any new `scripts/experiment_*.py` importing `sp1500_pit` uses the `_augmented` variant per doctrine

### Data sources

**Fundamentals:**
- **SimFin Start tier** ($25/mo, paid 2026-05-12 paradigm #13 acquisition) — 10y depth from 2016, PIT via PUBLISH_DATE column ✓
- Required SimFin fields: `Cash from Operating Activities` (OCF_TTM, trailing 4 quarters), `Total Assets` (current quarter + prior quarter for averaging), `PUBLISH_DATE` (PIT key)

**Prices:**
- **yfinance daily OHLCV cache** at `~/.alphalens/prices/` ✓ (~2800 tickers; will need ~200 delisted backfill for full augmented universe)

**Factors:**
- **FF3 + UMD + RF** from `alphalens.data.factors.load_carhart_daily` ✓
- **FF5 + UMD** for §5.1.2 attenuation diagnostic (if §10 risk M3 fires): `alphalens.data.factors.{load_ff5_daily,load_umd_daily}` ✓

No new vendor cost. PIT validation gate (CLAUDE.md mandatory for new vendors) NOT triggered — SimFin already validated for paradigm #13.

---

## §4. Methodology

### Signal computation

For each ticker on each rebalance date t:

1. **OCF_TTM**: sum of 4 most recent quarterly Cash-from-Operating-Activities values with PUBLISH_DATE ≤ t-1 day. If 4 quarters not available, drop ticker.
2. **Total_Assets_avg**: average of current-quarter and prior-quarter Total Assets, both with PUBLISH_DATE ≤ t-1 day. Avoids period-end-balance artifact.
3. **CBOP = OCF_TTM / Total_Assets_avg**
4. **Drop if Total_Assets_avg ≤ 0** (rare; degenerate / data error)
5. **No imputation** for missing CBOP — drop ticker at this rebalance (different from ev_fcff_yield which imputed FCFF; here cash flow is direct, less subject to corner cases).

### Cross-sectional ranking

1. **Winsorize** CBOP at [1%, 99%] percentile within rebalance universe
2. **Cross-sectional z-score** (population std-dev, `ddof=0`) — reuse `alphalens.screeners._common.{winsorize, rank_zscore}` from paradigm #15 refactor
3. **Top-decile selection**: pick ~10% of eligible universe by z-score (target N ≈ 120-150 tickers)
4. **Equal-weight** within top-decile (no value-weighting; standard academic spec)

### Portfolio construction

- **Long-only** (NO short leg)
- **Top-decile equal-weight**, ~120-150 names (10% of ~1200-1500 eligible)
- **No sector neutralization** in v1 (vanilla spec; sector-neutral v2 reserved if v1 PASS_MARGINAL)

### Rebalance cadence

- **Quarterly**, stride locked at 63 trading days ≈ Feb/May/Aug/Nov filing cadence (matches ev_fcff_yield #13 pattern)
- **Hold = 63 trading days** (1 quarter)
- Filing-aligned to PUBLISH_DATE schedule reduces stale-data risk

### Cost model

- **Standard 5-cost grid**: {0, 5, 10, 15, 25} bps half-spread per project pattern
- Per-rebalance turnover proxy; G4 gate at 15bps net αt ≥ 2.0
- **Turnover prediction**: 10-15% per rebalance (quarterly), 40-60% annualized — well below memo §10 hyper-turnover threshold for paradigm #15 (80%). Cost drag should NOT dominate.

---

## §5. Statistical methodology

**Primary regression spec (Carhart-4F, project-standard, doctrine-binding):**
```
(R_port,t − RF_t) = α + β_M·(Mkt-RF)_t + β_S·SMB_t + β_H·HML_t + β_Mom·Mom_t + ε_t
```

**Cadence:** daily portfolio returns (via existing `BacktestEngine` + `daily_continuous_returns`; standard for project)

**HAC SE:** Newey-West with `maxlags=126` (≈ 6 months; matches quarterly rebalance cycle; same as ev_fcff_yield #13)

**Sample:** 2016-08-31 → 2025-08-31 (SimFin earliest REPORT_DATE 2016-07-31 per `simfin_coverage_check_2026_05_12.md` Step 6; 1-month buffer for first quarterly rebalance)

### §5.1 Mandatory diagnostics

These are NOT authoritative gates (project doctrine binds on Carhart-4F per §8) — they are mandatory artifact-logging for honest postmortem.

1. **β_market (CAPM single-factor regression)**: log realised market beta of CBOP top-decile portfolio.
   - **Material finding if β_market > 1.3**: portfolio is high-beta growth-tilted; CBOP factor may be acting as growth-tilt proxy.
   - **β_market in [0.85, 1.15]**: portfolio is market-neutral on beta; signal is genuinely cross-sectional.

2. **FF5+UMD attenuation**: refit with RMW (profitability) + CMA (investment) included.
   - **Material finding if attenuation > 50%**: HXZ CBOP signal is largely absorbed by FF5's RMW (Fama-French already captures this); not additive to standard factor model.
   - **Attenuation < 30%**: CBOP carries incremental information beyond FF5.

3. **Industry concentration check**: log Herfindahl-Hirschman Index (HHI) of top-decile portfolio across 12 Fama-French industries.
   - **Material finding if HHI > 0.30** (top 3 industries hold >55% weight): industry-tilted signal; may need v2 sector-neutral variant.
   - **HHI < 0.20** (diversified): signal is broad cross-sectional, not concentrated.

4. **Turnover logging**: per pre-reg `turnover_logging_mandate` from paradigm #15. Log mean per-rebalance turnover; flag if > 20%/quarter (well below 80%/mo IM threshold given quarterly cadence).

These four diagnostics are mandatory artifact-logging — failure of any to be logged is a pre-reg violation but does NOT change verdict.

---

## §6. Phase design (locked)

Three non-overlapping windows per project standard:

| Phase | Window | Purpose |
|---|---|---|
| IS | 2016-08-31 → 2019-08-31 | In-sample design validation |
| OOS | 2019-08-31 → 2022-08-31 | Out-of-sample replication (COVID + value rally 2021-2022) |
| FL | 2022-08-31 → 2025-08-31 | Final lock (rate-rise regime + AI mega-cap rally) |

**Identical to ev_fcff_yield #13 windows** — SimFin data coverage constraint. 3 non-overlapping 3-year segments.

**Joint-PASS rule:** every window in {IS, OOS, FL} must individually clear all §8 gates.

**Phase-offset stride sweep:** standard 5 offsets per multi-phase orchestrator pattern (matches ev_fcff_yield + idiosyncratic_momentum precedent).

---

## §7. Pre-reg parameters (locked)

| Parameter | Locked value | Notes |
|---|---|---|
| Universe | S&P 1500 PIT-augmented, ex-financials | True PIT mandatory per 2026-05-14 doctrine |
| Eligibility floor | close ≥ $5 at signal date | standard |
| Signal | CBOP = OCF_TTM / avg(Total Assets) | Cash-based per Ball et al 2016 |
| PIT lag | PUBLISH_DATE ≤ asof − 1 day | conservative |
| Winsorize | [1%, 99%] | standard |
| Standardisation | cross-sectional z-score, ddof=0 | reuse `_common.rank_zscore` |
| Portfolio | top-decile, equal-weight, long-only | ~120-150 tickers from ~1200-1500 eligible |
| Rebalance | quarterly, stride=63 trading days | filing-aligned Feb/May/Aug/Nov |
| Hold | 63 trading days | 1 quarter |
| Cost grid | {0, 5, 10, 15, 25} bps half-spread | project standard |
| Regression | Carhart-4F + NW HAC maxlags=126 | daily cadence, matches ev_fcff_yield |
| Phases | IS 2016-2019, OOS 2019-2022, FL 2022-2025 | locked |
| n_phases | 5 phase-offsets per window | project standard |

---

## §8. Success criteria (Bonferroni-doctrine, pre-registered)

PASS requires **all four**:

1. Net Carhart-4F α t-stat ≥ **3.5** on full sample, Newey-West HAC SE, daily-cadence regression (project doctrine bar at k=16; strict Bonferroni 2.95; doctrine 3.5 self-imposed buffer)
2. Net Carhart-4F α t-stat ≥ **2.5** mean across IS / OOS / FL phases (phase-stability)
3. Net Carhart-4F α t-stat **> 0** in each of IS, OOS, FL individually (no negative-phase rescue)
4. Cost stress: net αt at half-spread = **15bps** remains ≥ **2.0** (cost-mirage prevention; G4 reads `t_net_4f` per H1 fix)

**PASS_MARGINAL**: passes (3) and (4) but joint αt is in [2.5, 3.5]. Triggers paper-trade observation, NOT capital deploy.

**FAIL**: any of (1)-(4) violated.

---

## §9. Honest priors with literature evidence

**Probability achieving doctrine 3.5 t-stat: 2-4%** (post-Perplexity-research + true-PIT correction estimate, 2026-05-14)

Composition:

- **Literature OOS replication evidence (2020-2024 post-publication audits)**: HXZ profitability factor long-short αt 1.8-2.3 in academic samples (Perplexity research 2026-05-14)
- **Long-only top-decile degradation**: literature shows 40-55% L/S alpha retention → pre-cost long-only αt ≈ 0.8-1.3 t-stat
- **True PIT adjustment** (subtract ~0.3 t-stat from literature priors that used snapshot universes per 2026-05-14 doctrine): pre-cost long-only αt ≈ 0.5-1.0 t-stat
- **Implementation cost @ 5bps half-spread** (~20-25 bps/y annual drag for quarterly rebalance at ~12%/quarter turnover): subtract ~0.2-0.3 t-stat → post-cost αt ≈ 0.3-0.8 t-stat
- **15bps cost stress (G4)**: additional ~0.3-0.4 t-stat compression → G4 αt ≈ 0.0-0.5 t-stat
- **Net posterior P(αt ≥ 2.5)**: ~7-11%
- **Net posterior P(αt ≥ 3.5)**: ~2-4%

**Most likely outcome**: αt mean across phases ~0.8-1.5, vindicated mechanism but below project doctrine 3.5. **Pattern likely to match paradigms #13 ev_fcff_yield and #15 idiosyncratic_momentum** (real signal vindicated by signs, modest magnitude, below conservative Bonferroni bar).

**Material findings expected regardless of verdict:**
- True PIT vs snapshot comparison (first paradigm under new doctrine) — empirical anchor for Δαt magnitude
- Industry concentration distribution of CBOP top-decile (tech/healthcare tilt?)
- FF5+RMW attenuation (HXZ vs Fama-French profitability redundancy check)
- Quarterly turnover empirical (test §10 M1 prediction)

---

## §10. Risk register

| Risk | P | Severity | Mitigation |
|---|---|---|---|
| RMW absorbs CBOP signal (FF5 already captures profitability via RMW) | 60% | EXPECTED | §5.1.2 attenuation diagnostic; if >50% attenuation, signal redundant with public factor; v1 still gates on Carhart-4F per pre-reg |
| Long-only top-decile retains only 40-55% L/S alpha (Perplexity-confirmed) | 90% | HIGH | Baked into §9 honest prior (0.4 t-stat reduction applied) |
| Industry concentration in tech / healthcare | 50% | MID | §5.1.3 HHI diagnostic; if HHI > 0.30, document as material finding; v2 sector-neutral candidate if PASS_MARGINAL |
| True PIT augmentation breaks engine consumption (delisted-ticker OHLCV gaps) | 30% | MID | Phase A pre-engineering: test universe loader + delisted-ticker price coverage; backfill via yfinance if <80% coverage |
| Post-2022 mega-cap rally absorbs profitability premium (CBOP becomes growth-tilt proxy) | 50% | MID | §5.1.1 β_market diagnostic; if β > 1.3, signal is growth-tilted (anti-pattern) |
| Industry-mix shift across windows (tech-heavy 2016-2019, energy 2019-2022, AI 2022-2025) breaks phase stability | 40% | MID | §5.1.3 HHI logged per window; cross-window comparison in postmortem |
| SimFin Total Assets quarterly availability gaps for delisted tickers | 25% | LOW | If material gap, restrict augmented universe to delisted-with-full-SimFin-coverage subset; document attrition |
| Cost-mirage at 15bps stress arm (matches insider_form4 anti-pattern) | 25% | LOW | Quarterly rebalance ≈ 40-60% annual turnover; cost drag is bounded; G4 gate should not knockout |
| Class-internal Bonferroni concerns (fresh-class evasion) | 15% | LOW | Honest registration: CBOP is distinct mechanism (cash efficiency) from prior classes (value, momentum, options). Project doctrine 3.5 binds regardless of class-internal threshold. |

---

## §11. Bonferroni accounting

**Project-level:**
- k=15 (pre-HXZ-registration: 15 paradigm failures + PEAD-14 in-flight)
- k=16 after HXZ registration
- Strict Bonferroni at α=0.05/16 → critical |t| = 2.95
- **Project doctrine 3.5 binds** (self-imposed buffer above strict)

**Class-internal (`hxz_profitability_2026_05_14`):**
- n=0 → n=1 with this registration
- Strict Bonferroni at α=0.05/1 → critical |t| = 1.96
- **Class-internal threshold << project doctrine** → project doctrine binds (3.5)

**No retroactive impact** on other paradigms (this class has no in-flight tests).

---

## §12. Capital deployment clause

Per project precedent: **no paradigm has standing PASS** after insider_form4 SLIPPAGE-FAIL (#11). HXZ PASS would be the first.

If HXZ PASS:
1. **Mandatory slippage stress diagnostic** (per `feedback_slippage_stress_diagnostic_pattern_2026_05_12.md`):
   - Regime-conditional β-amplification × per-phase turnover × Carhart re-regression on NET daily @ H=50bps median half-spread
   - PASS_MARGINAL → SLIPPAGE-FAIL conversion is real risk (paradigm #11 anti-pattern)
2. **Excess-cyclicality screen** vs IWM benchmark (CLAUDE.md mandatory pre-screen)
3. **Industry concentration audit** beyond §5.1.3 HHI — confirm not just tech mega-cap tilt
4. **Paper-trade observation 6-12 months** (matches v9D + pc_abnormal precedent)
5. Capital deploy only if all above PASS

If HXZ PASS_MARGINAL: paper-trade observation, NO capital deploy.

---

## §13. Module placement

- Scorer: `alphalens/screeners/hxz_profitability/` (new dir)
- Pure scorer: `alphalens/screeners/hxz_profitability/scorer.py` — uses `_common.rank_zscore + winsorize`
- Adapter: `alphalens/screeners/hxz_profitability/adapter.py` — composes SimFin store + pure scorer
- Experiment script: `scripts/experiment_hxz_profitability.py` (modeled on `scripts/experiment_ev_fcff_yield.py`)
- Audit orchestrator: `scripts/run_hxz_profitability_audit.py` (modeled on `scripts/run_ev_fcff_yield_audit.py`)
- Runpod launcher: `scripts/launch_hxz_audit.sh`
- Status: `__status__ = "ACTIVE"` (during audit), → CLOSED or PAPER_TRADE post-verdict

Distinct from `alphalens/screeners/ev_fcff_yield/` (different signal: profitability vs valuation) and `alphalens/screeners/idiosyncratic_momentum/` (different data: fundamentals vs price).

---

## §14. Pre-audit gates

1. **PIT augmentation loader** in `alphalens/data/universes/sp1500_pit.py::load_sp1500_pit_for_date_augmented` — unit tests + smoke
2. **Delisted-ticker OHLCV coverage check**: target ≥80% coverage on delisted-active subset at IS-start date 2016-08-31; backfill if gap
3. **Smoke profile** in `alphalens/preaudit/profiles.py` with 6-month 2018-Q1-Q2 smoke window, cap=200, --skip-precheck
4. **Coverage check**: SimFin cache + prices (incl. delisted backfill) + factors EXISTS_NONEMPTY
5. **`alphalens preaudit hxz_cash_op_2026_05_14_v1`** PASS → audit launch
6. **NO new vendor PIT validation needed** (SimFin already validated paradigm #13)

---

## §15. Sources / citations

1. Novy-Marx, R. (2013). "The Other Side of Value: The Gross Profitability Premium." *Journal of Financial Economics* 108(1): 1-28.
2. Hou, K., Xue, C., Zhang, L. (2015). "Digesting Anomalies: An Investment Approach." *Journal of Finance* 70(1): 1-72.
3. Ball, R., Gerakos, J., Linnainmaa, J. T., Nikolaev, V. (2016). "Accruals, Cash Flows, and Operating Profitability in the Cross Section of Stock Returns." *Journal of Financial Economics* 121(1): 28-45.
4. Asness, C., Frazzini, A., Pedersen, L. H. (2019). "Quality Minus Junk." *Review of Accounting Studies* 24(1): 34-112.
5. Hou, K., Mo, H., Xue, C., Zhang, L. (2020). "An Augmented q-Factor Model with Expected Growth." *Review of Asset Studies* 11(3).
6. AlphaLens precedent: `docs/research/ev_fcff_yield_v1_design_2026_05_12.md` (FCF-yield paradigm #13, FAIL but template for quarterly-rebalance SimFin pipeline)
7. AlphaLens precedent: `docs/research/idiosyncratic_momentum_v1_design_2026_05_14.md` (paradigm #15, scorer refactor lineage)
8. AlphaLens doctrine: `docs/research/plan_C_survivorship_retrospective_2026_05_14.md` (true-PIT mandate origin)
9. Perplexity research 2026-05-14: HXZ profitability post-publication OOS evidence in 2015-2024

---

## §16. Post-lock amendments (audit trail)

*To be populated after adversarial review (zen + Perplexity) per CLAUDE.md mandatory pre-compute gate.*

### A0. Pre-lock checklist (BEFORE engineering)

- [ ] Adversarial review by zen (gemini-3-pro-preview) on this memo § 1-15
- [ ] Adversarial review by Perplexity (sonar-reasoning-pro) on hypothesis + literature claims
- [ ] Both reviewers' verdicts incorporated as §16 A1/A2 amendments before status → LOCKED
- [ ] Ledger registration in `docs/research/preregistration/ledger.json` AFTER lock (not before)
- [ ] Engineering work (scorer + adapter + experiment + orchestrator + launcher) AFTER ledger entry committed

---

**Honest note from author (Claude, 2026-05-14):** this memo is structured for adversarial review. Key concerns I expect reviewers to flag:
1. Whether CBOP is sufficiently distinct from RMW (FF5 already includes profitability) — if attenuation is high, project doctrine 3.5 is far out of reach.
2. Whether my "honest prior" of 2-4% PASS doctrine is brave enough (could realistically be 1-2%).
3. Whether the true-PIT mandate adds engineering complexity that displaces paradigm-#16 timeline (the universe loader + delisted backfill is non-trivial, may take 2-3 days alone).
4. Whether industry concentration risk (50% probability in risk register) warrants v1 sector-neutralization rather than v2-deferral.
