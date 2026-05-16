# Insider Form-4 Opportunistic × Quality Gate — Design Memo (paradigm #16)

**Date:** 2026-05-14
**Status:** **REJECTED 2026-05-15** post zen adversarial review (continuation `9a1655fd`) + Perplexity sonar deep research. Both reviewers converged: math is structurally broken (paradigm #11 gross αt 2.71 + universe-shrink-by-70% → needed gross αt ~4.0+ on filtered universe, HIGHER than literature Cohen-Malloy ceiling). Project pivoted entirely away from factor-paradigm-search to thematic event-driven decision-support tool — see `docs/research/thematic_event_tool_v1_design_2026_05_15.md`. Memo retained as research record per Plan C precedent.

## Rejection rationale (2026-05-15)

Zen + Perplexity convergent findings:

1. **Doctrine amendment was loophole engineering** — same-day amendment to enable this experiment was flagged as motivated reasoning. Outside reviewer would reject.
2. **Bonferroni 1-cost claim indefensible** — testing 3 new variables simultaneously (delisted universe, F-score, cap percentile). At minimum 2-cost.
3. **Structural alpha amputation** — Cohen-Malloy concentrates in smallest quintile (αt≈3.5); cap_percentile≥25 + F-score≥6 cleanly REMOVES exactly that subset.
4. **Mathematically delusional G1=3.5 target** — paradigm #11 gross was 2.71; shrinking universe by ~70% raises SE by ~1.82×, requiring gross αt to more-than-double original Cohen-Malloy ceiling. Impossible.
5. **Bernard-Thomas trap** — "quality-screened insider buys" is ATTRIBUTION not strategy. Healthy mid-caps efficiently priced → residual signal too small to clear remaining 50bps spread.
6. **Joint probability decomposition** (Perplexity): ~1.5-2% posterior, not 5-10% prior. Below project doctrine 3.5 by structural ceiling.

**Decision:** ABORTED. Project pivoted away from paradigm-search entirely (see thematic tool memo).

---

**Original draft (DRAFT pre-adversarial-review):**

---
**Pre-reg ID:** `insider_form4_quality_gated_2026_05_14_v1`
**Class:** `insider_event_quality_compound_2026_05_14` (NEW; n=1 first test in class; **doctrine-amendment paradigm** per CLAUDE.md `## Research methodology` block "Signal × gate compounds allowed pre-single-PASS" amendment 2026-05-14)
**Project doctrine:** |t|≥3.5 binds (k=16→17)

---

## §0. Pre-registration context

### Why this paradigm exists (the trigger)

Paradigm #11 `insider_form4_opportunistic_2026_05_08_v2` (Cohen-Malloy-Pomorski opportunistic insider net-buy) hit **SLIPPAGE-FAIL 2026-05-12** despite gross signal validation:

- **OOS gross αt = +2.71** (class-internal PASS_MARGINAL at 2.86 threshold — αt-positive in every phase, dispersion within gate)
- **OOS net αt @ H=50bps half-spread = +1.27** (G1 knockout violated)
- **FL gross αt = +2.69**, net @ H=50bps = +1.95 (also knockout)

The signal mechanism is real and validated. The failure mode is **micro-cap distressed bid-ask spread** — Cohen-Malloy effect concentrates in small-cap quintile per the original 2012 JFE Table 2, but small-cap insider-targets are exactly where realistic trading costs (50bps median half-spread per `insider_form4_opportunistic_slippage_stress_postmortem_2026_05_12.md`) dominate signal.

**Hypothesis:** a Layer-2 selection-gate that restricts the eligible universe to **financially-healthy and liquid firms** (Piotroski F-score ≥ 6 + market-cap percentile ≥ 25th in R2000) preserves the Cohen-Malloy informational signal while removing the cost-trap names. The signal-on-quality-subset should retain a material fraction of gross αt while cutting net-αt-degradation at H=50bps dramatically.

### Class identity (honest accounting)

NEW class `insider_event_quality_compound_2026_05_14`. n=0 → n=1. Class-internal Bonferroni at α=0.05 → critical |t|=1.96.

**Why fresh class is honest here** (cf. zen adversarial review crushing HXZ "fresh class" claim on 2026-05-14):
- Compound test = SIGNAL (insider opportunistic, paradigm #11 class `informed_trader_signals_2026_05_05`) × GATE (Piotroski quality, NOT in any prior class).
- Per CLAUDE.md amendment 2026-05-14: screener × Layer-2-selection-gate compound pays **1 Bonferroni cost** (universe-restricted test) rather than 2 (independent-signal compound). Class-internal n=1 reflects this 1-cost.
- Mechanism justification per amendment: gate addresses documented failure mode (slippage on micro-cap distressed names). Not an evasion — it's the mechanistically-correct response to the failure.

### Project k accounting

k=16 pre-registration. After this paradigm, k=17. Project-wide strict Bonferroni at α=0.05/17 → critical |t| = 2.97. Project doctrine 3.5 unchanged.

### True PIT mandate

This is the **second** paradigm to register under the 2026-05-14 true-PIT-mandatory doctrine (after paradigm-16-HXZ-DRAFT which was REJECTED post-zen-review). Universe construction MUST use R2000 PIT-augmented (snapshot rosters intersected with delisted-ticker augmentation). The original paradigm #11 used R2000 PIT but without explicit delisted augmentation — a potential bias source we now correct.

---

## §1. Hypothesis

Within R2000 PIT-augmented universe (Russell 2000 small-cap PIT membership augmented with delisted tickers from `~/.alphalens/survivorship/`), filtering to firms passing the **quality gate** (Piotroski F-score ≥ 6 AND market-cap percentile ≥ 25th within R2000 at rebalance date) and applying the **Cohen-Malloy-Pomorski opportunistic-insider net-buy magnitude** signal residualized cross-sectionally against (`reversal_1m`, `momentum_6m`, `rv_30d`), with long-only top-decile equal-weight monthly rebalance (stride=21d, holding=21d), generates **net-of-cost Carhart-4F α with t-stat ≥ 3.5** (project doctrine) at H=50bps median half-spread on the 2018-2023 OOS sample, with positive net αt in each of OOS / FL phases individually, and Newey-West HAC SE at maxlags=126.

---

## §2. Mechanism + literature

**Signal mechanism (unchanged from paradigm #11):**
- Cohen, Malloy, Pomorski 2012 JFE "Decoding Inside Information" — opportunistic insiders (history of non-routine trades, 5y filter) outperform routine traders. Original paper αt~3.5 on size-stratified universe; effect concentrated in small-cap quintile.

**Gate mechanism (Piotroski F-score + cap percentile):**
- Piotroski 2000 JAR "Value Investing: The Use of Historical Financial Statement Information to Separate Winners from Losers from Future Stock Returns" — 9-criterion fundamental quality score (profitability, leverage, operating efficiency). F-score ≥ 6 separates "high-quality" from "low-quality" within value universe with documented post-publication αt persistence.
- Cap percentile threshold addresses bid-ask-spread mechanics: R2000's bottom 25% by market cap typically has 2-3× the half-spread of the top 75% (per Polygon retail-realistic spread surveys).

**Why compound might work where insider_form4 single failed:**

1. **Cohen-Malloy effect persists across cap range, but slippage concentration is in bottom 25%.** Restricting to top 75% by cap reduces transaction-cost impact by ~50-60% (estimated, validate in audit) while preserving most of the signal — Cohen-Malloy Table 2 shows αt declining from t≈3.5 (smallest) to t≈2.0 (mid) — mid quintile still has real signal.
2. **F-score ≥ 6 filter removes "distressed-rebound" mechanic.** Cohen-Malloy's original concern was that opportunistic insiders trade on real information. But in 2018-2023, some opportunistic buys came from distressed-CEO-buying-own-bankruptcy-trajectory — F-score filter excludes these (operating cash flow < 0, leverage rising = F-score < 4).
3. **Higher-quality firms have tighter spreads.** Piotroski F-score ≥ 6 firms are profitable, have positive OCF, declining leverage — they trade on more liquid order books even within R2000. Compounding effect with cap percentile.

**Why this might NOT work:**

1. **Quality filter might destroy the signal.** Cohen-Malloy mechanism is about insider INFORMATION asymmetry. Healthy firms may have less asymmetric information (analyst coverage is higher, insider edge is smaller). Filtering to quality could remove the names where insider trades are most informative.
2. **Universe attrition reduces statistical power.** F-score ≥ 6 typically retains ~40-50% of R2000; cap percentile filter additional. Combined filter could leave ~30-35% of original universe → fewer per-decile names → noisier portfolio.
3. **Quality factor (RMW absorption per Perplexity research 2026-05-14)** — if filtered universe shows quality-tilt absorbing the αt, Carhart-4F regression should catch it (since 4F doesn't include RMW). FF5+UMD attenuation diagnostic mandatory.
4. **Post-publication arbitrage of Cohen-Malloy after 2012.** Effect may have decayed further beyond what paradigm #11 picked up. Quality filter cannot rescue a fully-arbitraged signal.

---

## §3. Universe + data

### Universe (true PIT, doctrine 2026-05-14)

**Construction** — at each rebalance date t:

1. Load R2000 PIT snapshot rosters from `alphalens/data/alt_data/pit_universe.py` ($300M-$3B band, per paradigm #11 spec) at latest snapshot date ≤ t
2. Augment with delisted tickers from `~/.alphalens/survivorship/{delisted_2007_2018,delisted_2021_2026}.parquet` whose `delisted_date > t`
3. Apply paradigm-#11 fire-sale exclusion: `survivorship_pit.DelistingEvent` 180-day pre-delisting exclusion (insiders selling into a known-bad outcome generate spurious signals)
4. Apply quality gate (§4)
5. Apply Cohen-Malloy ticker-history filter (≥5y of past Form-4 filings to classify routine vs opportunistic per paradigm #11)

**Expected universe size before gate:** ~1800-2000 tickers (R2000 PIT-augmented). After Piotroski F-score ≥ 6: ~700-900 (40-45% retention). After cap percentile ≥ 25th: ~525-675 (~30% of original R2000). Top-decile → ~50-70 names per rebalance.

### Data sources

**Form-4 insider transactions:**
- VPS-backfilled parquet store at `~/.alphalens/form4_parquet/` (37 MB, 2.66M rows, 1.19M accessions, hive-partitioned by transaction_year per paradigm #11 backfill 2026-05-08). Provides routine/opportunistic classification per Cohen-Malloy 2012 §3.

**Fundamentals (for Piotroski F-score components):**
- `~/.alphalens/companyfacts_parquet/` (263 MB, 2784 tickers, XBRL-derived from SEC EDGAR per paradigm #4 backfill). Provides annual + quarterly: net income, OCF, total assets, current ratio components, gross margin, total debt, shares outstanding. All 9 F-score components computable.

**Prices:**
- `~/.alphalens/prices/` yfinance daily OHLCV (~2800 tickers). Used for: (a) market cap computation at rebalance date, (b) holding-period forward returns.
- Delisted-ticker backfill: per paradigm-#11 `survivorship/` infrastructure, target ~80% coverage on R2000 delisted-active subset.

**Factors:**
- `alphalens.data.factors.load_carhart_daily` (FF3 + UMD + RF) ✓
- `alphalens.data.factors.{load_ff5_daily, load_umd_daily}` for §5.1 attenuation ✓

**Polygon median-half-spread (slippage stress data):**
- Polygon Starter tier ($29/mo, in cache from paradigm #11) provides bid-ask spread history. Slippage diagnostic uses H=50bps median half-spread per paradigm #11 SLIPPAGE-FAIL postmortem (`insider_form4_opportunistic_slippage_stress_postmortem_2026_05_12.md`).

No new vendor cost. Zero new PIT validation gates.

---

## §4. Methodology

### Signal (UNCHANGED from paradigm #11)

For each (ticker, date t) in eligible universe:

1. **Cohen-Malloy classification**: classify each Form-4 filer as Routine or Opportunistic based on 5-year history (paradigm #11 implementation: `alphalens/screeners/insider_activity/cohen_malloy_classifier.py`).
2. **Net-buy aggregation**: sum dollar-value of opportunistic-insider net buys over trailing 6-month window ending at t (paradigm #11: `opportunistic_form4.py`).
3. **Signal**: `signal_raw_t = net_oppor_usd_t / equity_mcap_t` (size-normalized).
4. **Cross-sectional residualization**: regress `signal_raw` on (`reversal_1m`, `momentum_6m`, `rv_30d`) within rebalance universe; use residual as `score`.

### Gate (NEW — paradigm #16 contribution)

**Piotroski F-score (Piotroski 2000 JAR §III) — 9 binary criteria summed:**

Profitability (4): (1) ROA > 0, (2) OCF > 0, (3) ΔROA > 0 YoY, (4) OCF > Net Income (accruals quality).
Leverage/liquidity/source (3): (5) ΔLong-term debt / Total assets ≤ 0 YoY, (6) ΔCurrent ratio ≥ 0 YoY, (7) No new equity issuance YoY.
Operating efficiency (2): (8) ΔGross margin > 0 YoY, (9) ΔAsset turnover > 0 YoY.

**Gate filter v1 (LOCKED):** `F_score ≥ 6` AND `market_cap_percentile ≥ 25` (within current R2000 PIT-augmented at rebalance date).

**Rationale for v1 choice (and rejection of v1b/v1c alternatives):**
- v1a (F-score + cap percentile) is the SIMPLEST defensible compound; minimizes free-parameter surface
- v1b (CBOP + leverage from SimFin) was rejected because zen review crushed CBOP-as-signal for HXZ on "RMW absorption" — but as a GATE (binary, not gradient) the absorption argument is weaker. Still preferred F-score because it's a 9-criterion composite that's less data-dependent than single-ratio CBOP
- v1c (composite of v1a + v1b) has highest p-hacking surface — rejected

### Portfolio construction

- **Long-only top-decile** of `score` rank within gated universe (~50-70 names)
- **Equal-weight** within decile (paradigm #11 convention)
- **Rebalance**: monthly, stride=21 trading days, holding=21 (paradigm #11 lock)

### Cost model (REINFORCED from paradigm #11)

- **Standard 5-cost grid**: {0, 5, 10, 15, 25} bps half-spread
- **Mandatory slippage stress arm**: H=50bps median half-spread (THIS is the gate that killed paradigm #11; if v1 fails this, COMPOUND PARADIGM IS REJECTED)
- Slippage uses Polygon-derived per-rebalance per-ticker spreads; falls back to median 50bps for tickers without Polygon coverage
- G4 reads `t_net_4f` from net-cost regression (H1 fix pattern from ev_fcff_yield)

---

## §5. Statistical methodology

**Primary regression spec (Carhart-4F, project-standard, doctrine-binding):**
```
(R_port,t − RF_t) = α + β_M·(Mkt-RF)_t + β_S·SMB_t + β_H·HML_t + β_Mom·Mom_t + ε_t
```

**Cadence:** daily portfolio returns (via existing `BacktestEngine` + `daily_continuous_returns`; standard)

**HAC SE:** Newey-West with `maxlags=126` (6 months; matches Cohen-Malloy 6-month signal window — paradigm #11 lock Z1)

**Block bootstrap:** block_size=126 trading days (paradigm #11 lock Z1, 6-month rolling signal mandates this)

**Sample:** OOS 2018-01-01 → 2023-12-31 + FL 2024-01-01 → 2026-04-30 (apples-to-apples with paradigm #11 windows for Δαt comparability)

### §5.1 Mandatory diagnostics

1. **Δαt vs paradigm #11 single (apples-to-apples)**: report (αt gated − αt ungated) for OOS + FL windows. **Material finding if Δαt > +0.5** (gate adds material lift) OR **Δαt < -0.5** (gate destroys signal).
2. **Realized β_market**: CAPM single-factor regression. **Material finding if β_market < 0.7** (quality-tilt absorbing alpha into low-beta exposure) OR **> 1.3** (filtered universe became high-beta).
3. **FF5+UMD attenuation**: refit with RMW + CMA. **Material finding if attenuation > 50%** (RMW absorbs quality-gated alpha → gate signal redundant with public factor).
4. **Slippage net-αt at H=50bps**: PRIMARY GATE-EFFICACY METRIC. Paradigm #11 was 1.27 OOS / 1.95 FL. v1 must demonstrate substantial uplift (target net αt @ 50bps ≥ 2.0) — otherwise gate failed mechanistically and verdict is REJECTED.
5. **Universe-attrition diagnostic**: log mean per-rebalance count of eligible tickers, top-decile count, F-score-pass count, cap-percentile-pass count. Required for postmortem.
6. **Cohen-Malloy density preservation**: verify ≥30% of asof-quarters retain ≥30 opportunistic-classified tickers in gated universe. If <30%, BREADTH-FAIL (gate destroyed signal density).

---

## §6. Phase design (locked)

Two windows (matches paradigm #11 for apples-to-apples Δαt diagnostic):

| Phase | Window | Purpose |
|---|---|---|
| OOS | 2018-01-01 → 2023-12-31 | Out-of-sample replication (paradigm #11 primary verdict window) |
| FL | 2024-01-01 → 2026-04-30 | Final lock (paradigm #11 secondary window; covers 2024 post-AI rally + 2025 quant unwind) |

**Joint-PASS rule:** Both OOS AND FL windows must individually clear §8 gates.

**Phase-offset stride sweep:** standard 5 offsets per multi-phase orchestrator pattern.

**No IS window** for v1 because:
- Cohen-Malloy 5-year history filter requires Form-4 backfill data 2013+; we have it via VPS backfill ✓
- Paradigm #11 had TRAIN 2009-2017 + OOS 2018-2023 + FL 2024-2026; we use only OOS + FL because train design is INHERITED from paradigm #11 (same signal mechanism; no re-tuning)
- Risk: zen may flag this as "free parameter inheritance"; address in §16

---

## §7. Pre-reg parameters (locked)

| Parameter | Locked value | Notes |
|---|---|---|
| Universe | R2000 PIT-augmented + 180d delisting fire-sale exclusion | Per paradigm-#11 universe + 2026-05-14 true-PIT doctrine |
| Signal | Cohen-Malloy opportunistic net-buy magnitude, residualized | UNCHANGED from paradigm #11 |
| Gate | Piotroski F-score ≥ 6 AND market_cap_percentile ≥ 25 | NEW v1 lock |
| Portfolio | top-decile, equal-weight, long-only | matches paradigm #11 |
| Rebalance | monthly, stride=21 trading days, holding=21 | matches paradigm #11 |
| Cost grid | {0, 5, 10, 15, 25, **50**} bps half-spread | 50bps is mandatory paradigm #11 anti-pattern lesson |
| Regression | Carhart-4F + NW HAC maxlags=126 | matches paradigm #11 |
| Block bootstrap | block_size=126 trading days | matches paradigm #11 Z1 |
| Phases | OOS 2018-2023, FL 2024-2026 | matches paradigm #11 for Δαt diagnostic |
| n_phases | 5 phase-offsets per window | project standard |

---

## §8. Success criteria (Bonferroni-doctrine, pre-registered)

PASS requires **all five**:

1. **G1 Net Carhart-4F α t-stat @ H=50bps ≥ 3.5** on full sample (THE PARADIGM-#11-KILLER-METRIC; doctrine-binding)
2. G2 Mean αt @ H=50bps across OOS+FL phases ≥ 2.5
3. G3 Net αt @ H=50bps > 0 in each phase individually
4. G4 Cost stress: net αt at H=15bps remains ≥ 3.0 (matches project standard G4 + extra buffer because 15bps is below the 50bps median that killed paradigm #11)
5. **G5 Cohen-Malloy density preserved**: ≥30% of asof-quarters have ≥30 opportunistic-classified tickers in gated top-decile candidates (per §5.1.6)

**PASS_MARGINAL**: passes G2-G5 but G1 net @ 50bps is in [2.5, 3.5]. Triggers paper-trade observation, NOT capital deploy.

**FAIL**: any of G1-G5 violated.

**REJECTED COMPOUND PARADIGM**: if net αt @ H=50bps < paradigm #11's 1.27 (OOS) — the gate FAILED MECHANISTICALLY (made it worse than no gate). This is a stronger fail than ordinary FAIL — implies the gate was the wrong tool.

---

## §9. Honest priors with literature evidence

**Probability achieving doctrine 3.5 t-stat: 5-10%** (post-adversarial-review estimate; significantly higher than HXZ single 1-2% per zen because):

1. **Signal is already validated (paradigm #11 gross αt 2.71)** — not literature, EMPIRICAL on our universe.
2. **Failure mode is identified and mechanistically addressed** — gate directly attacks cost-on-distressed root cause.
3. **F-score is mechanistically distinct from RMW** — RMW is profitability-as-gradient-signal; F-score is profitability-as-binary-gate. Carhart-4F attenuation concern weaker.
4. **No fresh-class evasion** — paying full Bonferroni cost per amendment.

Composition stack:

| Component | Contribution |
|---|---|
| paradigm #11 gross αt (OOS) | +2.71 baseline |
| Slippage @ 50bps without gate | −1.44 (= 1.27 net) — paradigm #11 actual |
| Quality gate slippage relief (estimated 50-70% of 1.44 recovered) | +0.7 to +1.0 |
| Signal degradation from universe shrink (Cohen-Malloy Table 2: smallest t=3.5 → mid t=2.0 implies ~40% degradation in mid-cap subset) | −0.7 to −1.0 |
| True-PIT correction (subtract per 2026-05-14 doctrine) | −0.3 |
| Bonferroni stress for gate-test (1 added test) | minor (doctrine already at 3.5) |
| **Posterior net αt @ 50bps** | **~1.4-2.7 (median ~2.0)** |

**Net posterior P(αt ≥ 3.5)**: ~5-10%.
**Net posterior P(αt ≥ 2.5 PASS_MARGINAL)**: ~30-40%.

**Most likely outcome**: net αt @ 50bps in [1.5, 2.5] range — gate provides material slippage relief but doesn't push to full PASS. Paper-trade activation plausible. Pattern **WOULD BE FUNDAMENTALLY DIFFERENT** from paradigms #13/#15 (which were modest-signal-below-bar); this would be **strong-signal-cost-recovered-still-below-bar**.

**Material findings expected regardless of verdict:**
- Empirical Δαt of quality gate on a known signal — first such measurement in the project
- Cohen-Malloy effect cap-size decomposition (smallest vs mid quintile retention through gate)
- F-score vs RMW orthogonality empirical (§5.1.3 attenuation)
- Slippage-relief efficacy of binary-gate vs gradient-signal approach

---

## §10. Risk register

| Risk | P | Severity | Mitigation |
|---|---|---|---|
| Quality gate destroys Cohen-Malloy signal (filtered universe has low information asymmetry) | 30% | HIGH | §5.1.1 Δαt diagnostic; if Δαt < -0.5, REJECTED COMPOUND |
| Universe attrition reduces statistical power below threshold | 25% | MID | §5.1.5 attrition diagnostic; G5 density gate enforces ≥30 opp-classified tickers per quarter |
| F-score is FF5+RMW redundant (gate adds no orthogonal information beyond public factor) | 40% | MID | §5.1.3 FF5+UMD attenuation diagnostic; if >50%, document as confound finding |
| Slippage gate insufficient (50bps half-spread underestimates true micro-cap costs even after filter) | 35% | MID | §8 G1 uses 50bps; PASS requires t-stat ≥ 3.5 at this stress level (not just baseline) |
| 2024-2025 quant unwind regime ("junk rally" Q3 2025) hurts quality-tilt signal | 50% | MID | Per Perplexity 2026-05-14 — profitability factors in active drawdown 2025 H2; phase FL captures this regime, no special mitigation |
| Doctrine-amendment scrutiny — zen may flag "signal × gate" framing as Bonferroni evasion | 30% | LOW | Honest registration: 1 Bonferroni cost paid; mechanism justification in §0 |
| True-PIT engineering complexity adds 2-3 days to timeline | 60% | LOW | Same engineering effort as HXZ doctrine-loader (deferred to engineering phase); already planned |
| insider_form4 module changes since paradigm #11 (signal computation drift) | 15% | LOW | Re-run paradigm #11 verification on OOS subset; assert αt 2.71 reproduces within 0.1 t-stat |

---

## §11. Bonferroni accounting

**Project-level:**
- k=16 (pre-this-paradigm: 15 paradigm failures + PEAD-14 in-flight + paradigm-15 IM completed)
- k=17 after this paradigm registration
- Strict Bonferroni at α=0.05/17 → critical |t| = 2.97
- **Project doctrine 3.5 binds** (self-imposed buffer above strict)

**Class-internal (`insider_event_quality_compound_2026_05_14`):**
- n=0 → n=1 with this registration
- Strict Bonferroni at α=0.05/1 → critical |t| = 1.96
- **Class-internal threshold << project doctrine** → project doctrine binds

**Compound-test cost (per CLAUDE.md amendment 2026-05-14):**
- Signal (insider_form4) inherits paradigm #11 ledger entry; no double-count
- Gate (quality) adds 1 test cost (universe-restricted); reflected in n=1 in new compound class

---

## §12. Capital deployment clause

If PASS (net αt @ H=50bps ≥ 3.5):
- **First standing PASS in project after 14 failures + 1 SLIPPAGE-FAIL** — capital deploy unlocked PER PRE-REG
- Mandatory diagnostics before deployment (per `feedback_slippage_stress_diagnostic_pattern_2026_05_12.md`):
  - Regime-conditional β-amplification ×  per-phase turnover × Carhart re-regression on net daily at H=50bps + buffer 75bps stress
  - Excess-cyclicality screen vs IWM benchmark
- Paper-trade observation: 6-12 months (matches v9D + pc_abnormal precedent)
- Capital deploy only if all above PASS

If PASS_MARGINAL: paper-trade observation only.

---

## §13. Module placement

- **Reuse paradigm #11 screener:** `alphalens/screeners/insider_activity/` (already CLOSED but reusable as reference implementation; do NOT modify CLOSED module per ADR 0005)
- **NEW gate module:** `alphalens/gates/quality.py` — new occupant in existing `gates/` layer (currently RESEARCH_ONLY single-occupant per CLAUDE.md `## Layer status`)
- **Adapter glue:** `alphalens/screeners/insider_form4_quality_gated/` (new dir; `__status__ = "ACTIVE"` during audit)
  - `adapter.py` — composes `insider_activity.cohen_malloy_classifier` + `gates.quality.f_score_gate` + universe loader
- **Experiment script:** `scripts/experiment_insider_form4_quality_gated.py` (modeled on `scripts/experiment_insider_form4_opportunistic.py`)
- **Audit orchestrator:** `scripts/run_insider_form4_quality_gated_audit.py` (modeled on paradigm #15 orchestrator)
- **Runpod launcher:** `scripts/launch_insider_form4_quality_gated_audit.sh`

---

## §14. Pre-audit gates

1. **R2000 PIT-augmented loader** in `alphalens/data/universes/` — implementation deferred to engineering phase; smoke test required
2. **Piotroski F-score module** with unit tests + smoke on 10 known firms (AAPL, JPM, etc) — F-score values cross-checked against published Piotroski 2000 or recent academic replication
3. **Smoke profile** in `alphalens/preaudit/profiles.py` — 6-month 2020-Q1-Q2 smoke window, cap=300, --skip-precheck
4. **Coverage check**: companyfacts_parquet, form4_parquet, prices (incl. delisted), factors EXISTS_NONEMPTY
5. **`alphalens preaudit insider_form4_quality_gated_2026_05_14_v1`** PASS → audit launch
6. **Paradigm #11 reproducibility check**: regenerate paradigm #11's OOS αt 2.71 on current code (pre-gate) before launching gated version. Tolerance ±0.1.
7. **NO new vendor PIT validation needed** — Form-4, SimFin, companyfacts, Polygon all validated in prior paradigms

---

## §15. Sources / citations

1. Cohen, L., Malloy, C., Pomorski, L. (2012). "Decoding Inside Information." *Journal of Finance* 67(3): 1009-1043.
2. Piotroski, J. (2000). "Value Investing: The Use of Historical Financial Statement Information to Separate Winners from Losers from Future Stock Returns." *Journal of Accounting Research* 38: 1-41.
3. AlphaLens precedent: `docs/research/insider_form4_opportunistic_design_2026_05_05.md` (paradigm #11 v2 design)
4. AlphaLens postmortem: `docs/research/insider_form4_opportunistic_slippage_stress_postmortem_2026_05_12.md` (slippage failure mode characterization)
5. AlphaLens doctrine: CLAUDE.md `## Research methodology` amendment 2026-05-14 "Signal × gate compounds allowed pre-single-PASS"
6. AlphaLens doctrine: CLAUDE.md `## Research methodology` 2026-05-14 "True PIT universe mandatory dla paradigmów >100 tickers"
7. Adversarial review precedent: HXZ profitability v1 design (`hxz_profitability_v1_design_2026_05_14.md`, REJECTED post zen review, continuation `e97ba21c`) — lessons informing this memo's framing
8. Perplexity research 2026-05-14: profitability factor decay + quality-filter regime conditionality (informs §10 risk register)

---

## §16. Post-lock amendments (audit trail)

*To be populated after adversarial review (zen + Perplexity) per CLAUDE.md mandatory pre-compute gate.*

### A0. Pre-lock checklist

- [ ] Adversarial review by zen (gemini-3-pro-preview, high thinking) on this memo § 0-15
- [ ] Adversarial review by Perplexity (sonar deep research) on F-score post-publication evidence + quality-filter mechanism literature
- [ ] Both reviewers' verdicts incorporated as §16 A1/A2 amendments before status → LOCKED
- [ ] Ledger registration in `docs/research/preregistration/ledger.json` AFTER lock
- [ ] Engineering work (gate module + adapter + experiment + orchestrator + launcher) AFTER ledger entry committed

---

**Honest pre-review flags from author (Claude, 2026-05-14):**

1. **No IS window** — reviewer may flag this as parameter inheritance from paradigm #11 without re-tuning gate threshold. F-score ≥ 6 is academic Piotroski default but in our specific R2000 ex-fin context the optimal threshold might differ. Should we test F ≥ 5 / ≥ 6 / ≥ 7 with strict Bonferroni? Or lock ≥6 per literature default?
2. **G1 net αt @ 50bps ≥ 3.5** is structurally harder than paradigm-#11-style "gross αt ≥ 2.86 class-internal" target. Realistically may need to relax to 2.5 (PASS_MARGINAL) to be physically achievable.
3. **Universe attrition risk** — if F-score ≥ 6 retains only 30% of R2000, top-decile becomes ~50-70 tickers per rebalance — small sample for cross-sectional ranking.
4. **Bonferroni accounting honesty** — am I really paying 1 cost not 2? Zen will scrutinize this. The signal scorer is paradigm-#11-identical; the gate is a new layer. Per ADR 0007 this IS one new test cost. But zen may push back.
5. **Slippage diagnostic interpretation** — what if v1 produces net αt @ 50bps = 2.1? That's better than paradigm #11's 1.27 but below G1 3.5. Verdict = PASS_MARGINAL = paper-trade. Is paper-trade a useful outcome given 2 prior INCONCLUSIVE paper-trades (v9D, pc_abnormal) already in observation?
