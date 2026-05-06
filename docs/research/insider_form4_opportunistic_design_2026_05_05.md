# Design memo — Form-4 Opportunistic Insider Screener (insider_form4_opportunistic_2026_05_05)

**Status:** LOCKED 2026-05-05 (pending pre-reg ledger registration)
**Class:** `insider_form4_opportunistic_2026_05_05` (FRESH single-class — distinct from archived `alphalens/archive/screeners/insider/` cluster scorer CLOSED 2026-04-24)
**Owner:** Kamil Pająk (solo)
**Compute envelope:** ~3-5d wall on runpod CPU pod for backfill (~12-15M Form-4 filings 2004-2026), then ~6-12h for Phase A check + 5-phase audit on R2000 PIT.
**Plan reference:** `/Users/jacoren/.claude/plans/sunny-brewing-biscuit.md`

## 1. Hypothesis

Long-only top-decile equal-weight monthly-rebalance Form-4 **opportunistic-insider** net-buy magnitude (Cohen-Malloy-Pomorski JFE 2012) residualized cross-sectionally vs (`reversal_1m`, `momentum_6m`, `rv_30d`) generates Carhart 4F mean alpha t-stat ≥ 2.86 across a 5-phase OOS audit on Russell 2000 PIT, 2018-01-01 → 2023-12-31.

**Mechanism.** Per Cohen-Malloy-Pomorski (2012, Section III.A, p. 1786), insiders fall into two behavioral classes:
- **Routine** = an insider who placed a trade in the same calendar month for **at least three consecutive prior years** (e.g., always January in t-3, t-2, t-1). Their trades are uninformative — predictable timing, no info content.
- **Opportunistic** = everyone else with sufficient history (≥1 trade in each of the 3 preceding years). Their irregular trading patterns carry information.
- **Unclassified** = insiders without ≥1 trade in each of the 3 preceding years. Excluded from signal.

The paper documents +82bps/month value-weighted abnormal returns following opportunistic buys, concentrated in small-cap firms where information asymmetry is highest.

**Why opportunistic, not Lakonishok-Lee net-buy (any insider) or cluster (multiple insiders).** The earlier `alphalens/archive/screeners/insider/` (Layer 2d, CLOSED 2026-04-24) used cluster detection — multiple insiders within 30d. Carhart αt=+2.14 in-sample → +0.68 out-of-sample = classic overfit. Cohen-Malloy is mechanistically and statistically different: per-insider 5-year history filter pre-classifies signal quality before aggregation, so we are not aggregating noise from routine insiders. Per `feedback_burnt_holdout_multiplicity.md`, different feature spec + different OOS window + new class label = honest fresh-class refresh.

## 2. Adversarial review summary (BEFORE compute burn)

### 2.1 Perplexity (sonar-reasoning-pro, 2026-05-05)

Asked: "Three paths after v9D + v11 INCONCLUSIVE: A compound ensemble, B variant v11, C fresh class. Rank by defensibility."

**Verdict:** C ≫ B ≫ A (mocno). Key findings:

- **Path A (v9D+v11 ensemble) HARKing-adjacent.** Both signals are options-class informed-trader-flow proxies. Expected mechanism correlation ρ ≈ 0.60-0.70. Ensemble ceiling under ρ=0.65: ≈ 2.55-2.65, **misses +2.86 by 0.21-0.31 t-units.** Combining two near-misses post-hoc to clear the bar = textbook HARKing.
- **Path B (variant v11) does NOT escape multiplicity.** Same data window 2008-2018 already burnt for v11; OI-based vs volume-based variants on cooked period.
- **Path C (fresh class) defensible.** Form-4 > analyst revisions because:
  - Analyst revisions: HIGH decay risk (Arnott 1985+, retail arbitraged via Yahoo/Refinitiv APIs)
  - Form-4: LOWER decay risk (less retail penetration, mechanism orthogonal to options-flow, fewer competitors monitoring live)

Asked again: "Universe + bootstrap + dispersion gate decisions, plain language."

**Verdict:** R2000 primary + R3000 secondary, accept 126d block bootstrap, pre-register 70pp dispersion for R2000 with literature citation.

### 2.2 Perplexity classifier-definition verification (sonar-pro, 2026-05-05)

Asked: "What is the EXACT Cohen-Malloy 2012 routine vs opportunistic definition?"

**Verdict caught a FATAL spec error in initial draft.** First draft had ">=3 of 5 years" with 5-year lookback and "<2 trades in any non-anchor month" addendum. Paper p. 1786 actually defines:
- Routine = trade in same calendar month for **3 consecutive prior years**
- Opportunistic = everyone else with sufficient history
- Eligibility = ≥1 trade in EACH of the 3 preceding calendar years
- Lookback = 3 years (not 5)

**Amendment locked:** classifier definition rewritten to faithfully match paper p. 1786 (§4.1). History lookback shortened from 2004 → 2006 (saves ~2 years backfill compute, ~1M filings). This is a HONEST replication now, not "Cohen-Malloy-inspired variant."

### 2.3 Gemini (gemini-3-pro-preview thinkdeep, 2026-05-05)

Asked: scrutinize the locked design for FATAL flaws.

Three load-bearing concerns surfaced (NOT FATAL but require amendments):

- **Z1 — Block-bootstrap size mismatch (METHODOLOGY HAZARD, fixable).** A 6-month rolling signal at 21d stride means consecutive observations share ~85% of underlying data. The project-default 4-week block bootstrap would inflate t-stats by under-estimating serial correlation. **Fix locked: block_size = 126 trading days** (matches signal window).
- **Z2 — Stealth multiplicity beyond program n=27.** The act of choosing Form-4 PO obserwacji 11 fails + 2 INCONCLUSIVE is itself an unregistered search. **Mitigation: prose-only acknowledgment in postmortem, NOT numerical n bump.** Justification: bumping n every time class choice follows prior failures leads to infinite regress; current naive |t| ≥ 2.86 is sufficiently strict.
- **Z3 — Size-stratification critical for Cohen-Malloy.** Effect concentrated in small-cap quintile (paper Table 2: smallest t≈3.5, mid t≈2.0, largest t≈1.0 ns). Equally-weighted R3000 dilutes signal in arbitraged large-cap zone. **Fix locked: primary R2000, secondary diagnostic R3000.**

### 2.4 Resulting amendments to plan (all locked before run)

1. Primary universe = R2000 PIT (was R3000); secondary diagnostic R3000 reported in same audit JSON without separate Bonferroni unit
2. Romano-Wolf bootstrap block_size = 126 trading days (was project-default ~4w)
3. R2000 dispersion gate = 70pp (was 50pp); R3000 secondary diagnostic gate stays 50pp
4. Phase A0 added: density check ≥2 transactions/ticker/quarter on R2000 TRAIN
5. Stealth-multiplicity acknowledgment in postmortem (no n bump)
6. Cost-drag risk for R2000 small-caps documented (report cost-gross alongside cost-net if cost-net Sharpe negative)
7. **Cohen-Malloy classifier rewritten to faithful paper definition (3y consecutive same-month, not 5y "≥3 of 5"). History lookback 2006-2008 (was 2004-2008). Saves ~2y backfill compute.**

## 3. Periods (frozen)

- **Insider history lookback (not eval)**: 2006-01-01 → 2008-12-31 — 3yr pre-IS for Cohen-Malloy classification of 2009 trades (per paper p. 1786 — requires ≥1 trade in each of 3 preceding years). NOT used for evaluation.
- **TRAIN (Phase A breadth + density + direction)**: 2009-01-01 → 2017-12-31 (9yr post-GFC)
- **OOS retrospective (5-phase audit, primary verdict source)**: 2018-01-01 → 2023-12-31 (6yr, never seen by any insider screener)
- **Final lock (single-phase, prose-discount-flagged)**: 2024-01-01 → 2026-04-30 (2.3yr; old archived cluster screener tested here but different feature spec → discount in postmortem prose, no gate change)

## 4. Signal specification (frozen)

### 4.1 Cohen-Malloy classifier (faithful replication of paper p. 1786)

Classification is performed at the **start of each calendar year** based on the 3 preceding years' history. The classification then applies to all trades by that insider during that year.

For each insider person-CIK at the start of calendar year Y:

1. Pull all Form-4 filings filed by this person-CIK in window [Y−3, Y) — i.e. years Y−3, Y−2, Y−1.
2. **Eligibility check**: insider must have ≥1 trade in EACH of the 3 preceding calendar years. If any of the 3 years has zero trades, classify as **UNCLASSIFIED** and exclude from signal.
3. **Routine check**: identify whether there exists any calendar month M (1..12) such that the insider placed at least one trade in month M in each of the 3 preceding years. If yes → **ROUTINE**.
4. **Opportunistic**: if eligible (passes step 2) but not routine (fails step 3) → **OPPORTUNISTIC**.

Classification is locked at start of year Y and reused for all trades in year Y. Re-evaluated annually using the rolling [Y−3, Y) window. Per paper Footnote 9 (p. 1786) and Internet Appendix p. 12.

### 4.2 Per-ticker signal

For each (ticker, asof t) on the rebalance calendar:

1. Pull all Form-4 records via `Form4PITStore.records_as_of(ticker, asof, lookback_days=180)` — filing_date ≤ asof, exclude tickers with `delisted_date ≤ asof + 180 days` (180d fire-sale exclusion per PIT audit F4 finding).
2. Filter to: code ∈ {`P`} (open-market purchase only) ∪ {`S`} (open-market sale only); officer or director (exclude isOther; exclude isTenPercentOwner unless also officer/director).
3. Classify each record's reporting_owner via the Cohen-Malloy classifier above. Drop routine + insufficient_history records.
4. Compute `usd_value = transaction_shares × transaction_price_per_share`. For records with missing price (~5% historically), impute via close price on transaction_date (fallback to filed_date if missing).
5. `net_oppor_usd_t = Σ (buy_usd − sell_usd)` over remaining opportunistic records.
6. `signal_raw_t = net_oppor_usd_t / equity_mcap_t` where mcap_t is contemporaneous market cap from the universe builder.

### 4.3 Cross-sectional residualization

Per asof t, fit OLS within asof:

```
signal_raw ~ reversal_1m + momentum_6m + rv_30d + intercept
```

Score = +residual (high score = bullish, NET BUY direction; no sign-flip because direction is mechanically aligned with hypothesis).

Module: `alphalens/screeners/insider_activity/opportunistic_form4.py:score_opportunistic_form4`. Mirrors `score_cross_sectional_residual` in v9D. NaN propagates: any missing input → NaN score → row excluded from cross-section.

## 5. Architecture (single-layer Layer 2 per ADR 0007)

| Element | Value |
|---------|-------|
| Layer | 2 (cross-sectional screener only — no selection-gate, no overlay) |
| Primary universe | R2000 PIT — Russell 2000 small-caps, point-in-time via `alphalens/data/alt_data/pit_universe.py` ($300M-$3B band) augmented with `survivorship_pit.DelistingEvent` 180d fire-sale exclusion |
| Secondary diagnostic universe | R3000 PIT — full Russell 3000, reported in same audit JSON without separate Bonferroni count |
| Selection | Top-decile cross-sectional rank on score_t, equal-weight long-only |
| Rebalance | Monthly (stride=21d) |
| Cost profile | Standard project `RealisticCostModel` (consistent with v9D/v11/distress_credit). Note: small-cap bid-ask spreads + market impact higher; report cost-gross AND cost-net |
| Benchmark | Carhart 4-factor (Mkt-RF, SMB, HML, UMD) via `alphalens/attribution/factor_analysis.py:run_regression` |

## 6. Pre-reg gates (frozen — locked before run)

### 6.1 Primary R2000 verdict gates (dispersion 70pp)

| Verdict | Condition |
|---------|-----------|
| **PASS** | every-phase αt ≥ 1.5 AND mean αt ≥ 2.86 AND mean excess_net_ann ≥ 0 AND dispersion ≤ 70pp |
| **PASS_MARGINAL** | mean αt ∈ [2.50, 2.86) AND every phase αt ≥ 0 AND dispersion ≤ 70pp |
| **INCONCLUSIVE** | mean αt ∈ [2.50, 2.86) AND ≥1 phase αt < 0; OR (dispersion > 70pp AND mean ≥ 2.50) |
| **FAIL** | mean αt < 2.50 OR mean excess_net_ann < 0 OR (dispersion > 70pp AND mean < 2.50) |

**Pre-reg justification for 70pp gate (R2000 only):** R2000 monthly cross-sectional return dispersion runs ~1.4-1.7× R1000 (Russell index methodology + Fama-French SMB factor vol). The project-default 50pp gate was calibrated for large-cap; mechanical scaling 50 × 1.4 = 70pp. Reference: Asness, Frazzini, Israel, Moskowitz "Size Matters, If You Control Your Junk" (JFE 2018, Table 4 — SMB factor monthly std ~3% vs market ~4% = 1.5× ratio; cross-sectional dispersion in returns within SMB ≈ similar multiplier).

### 6.2 Secondary R3000 diagnostic gates (dispersion 50pp)

Same verdict structure with dispersion threshold 50pp. **R3000 verdict is informational only** — reported in audit JSON, NOT recorded in ledger as separate test, NO program-Bonferroni increment.

### 6.3 Auto-pivot triggers (Phase A on TRAIN, before full multi-phase run)

Phase A executes as a standalone script before audit_multi_phase invocation. Hard-coded in this pre-reg:

- **A0 Density (NEW per Z3 amendment)**: median opportunistic-insider transaction count per R2000 ticker per quarter ≥ 2 on TRAIN 2009-2017. If <2 → ABANDON, log "DENSITY-FAIL". Rationale: small-cap universe with sparse insider activity provides insufficient signal density.
- **A1 Breadth**: ≥30% of asof-quarters have ≥50 R2000 tickers with `score_t ≠ NaN`. If <30% → ABANDON, log "BREADTH-FAIL" (precedent: event_drift v3/v4).
- **A2 Direction**: TRAIN-period Spearman ρ(score_t, forward_21d_excess_return) > −0.05. If ≤ −0.05 → ABANDON (sign-flip indicator; do not waste multi-phase compute).

### 6.4 Bounds-adjusted CI

Romano-Wolf bootstrap with **block_size = 126 trading days** (≈ 6 months, matching signal rolling window — per Gemini Z1 amendment). 1000 reps. Report `bounds_alpha_t_lower` / `bounds_alpha_t_upper` in audit JSON.

### 6.5 Carhart regression HAC lock

**`run_regression(..., cov_type="HAC", hac_maxlags=126)`** — explicit override of project-default Newey-West lag rule. Default rule `int(4·(n/100)^(2/9))` yields ~14 lags on n=1500 daily obs, which under-specifies HAC kernel for the 6-month signal-induced autocorrelation in portfolio composition. Per `factor_analysis.run_regression` docstring guidance for overlapping-signal regimes. Without this override, t-stats would be artificially inflated by ~10-20% (estimate based on signal-window-to-default-lag ratio).

## 7. Bonferroni accounting

| Quantity | Value | Rationale |
|----------|-------|-----------|
| Intra-class n (within `insider_form4_opportunistic_2026_05_05` class) | 1 | First test in fresh class. Naive critical \|t\| ≥ 1.96. |
| Program-level Bonferroni n at registration | 27 (this test = the 27th counted test in program ledger) | Per `multiple_testing.bonferroni_critical_tstat` convention; threshold ≥ +2.86 naive. |
| Stealth-multiplicity correction | None (prose only) | Per Gemini Z2 + project policy: bumping n every class refresh leads to infinite regress; |t| ≥ 2.86 is already strict. Postmortem must explicitly acknowledge that class choice was conditioned on prior 11 failures + 2 INCONCLUSIVEs. |

## 8. Open risks (mitigations documented)

1. **3-year lookback dependency on 2006-2008 data quality**. SEC EDGAR Form-4 backfill quality drops pre-2003 (XBRL adoption); 2006-2008 should be reasonably complete. If 2006-2008 has >20% missing CIKs, classifier output noisy for 2009-2013 evaluations. **Mitigation:** probe Q1 2010 vs Q1 2020 classifier output distribution after backfill; if divergent (>10% absolute change in routine fraction), restrict IS to 2014-2018.
2. **Latent E4 ticker-as-of-filing risk** (per `pit_audit_2026_04_30_findings.md`). Reverse mergers misattribute old filings to new tickers. **Mitigation:** freeze ticker-CIK mapping snapshot from 2026-04-30 (`alphalens/data/alt_data/ticker_cik_map.py`); flag in postmortem if 5+ universe-rank changes detected.
3. **Cohen-Malloy decay since 2012 publication**. Effect may be partially arbitraged. **TREATED AS EXPECTED INFO, not bug.** If decay observed in OOS but not TRAIN, this is informative — confirms "FEATURES ARE THE BOTTLENECK" (per `nonlinear_alt_data_v1` postmortem) extends to informed-trader-flow mechanisms broadly.
4. **Burnt-2024-2026 caveat**. The archived cluster scorer was tested on 2024-2026 and FAILed. Final lock run uses different feature spec (opportunistic vs cluster) and different mechanism, but postmortem MUST explicitly acknowledge prior run on this window. Verdict gates remain identical (no prose-only discount).
5. **Stealth multiplicity** (Gemini Z2). Acknowledged in postmortem; no numerical correction applied (see §7).
6. **Higher cost drag in R2000 small-caps**. Bid-ask spreads + market impact higher than R3000. Standard `RealisticCostModel` may underestimate. **Mitigation:** report cost-gross AND cost-net Sharpe in final lock; if cost-net Sharpe < 0 but cost-gross > 0, flag as "implementation-bottlenecked" in verdict prose.

## 9. Implementation sequence

Per `/Users/jacoren/.claude/plans/sunny-brewing-biscuit.md`, the build proceeds:

1. ✅ Adversarial pre-run review (this memo)
2. Ledger pre-registration via `alphalens preregister add` (after this memo + params JSON locked)
3. Build new modules with TDD red→green:
   - `alphalens/data/alt_data/form4_bulk_backfill.py` (mock-tested)
   - `alphalens/data/store/form4_pit.py`
   - `alphalens/screeners/insider_activity/cohen_malloy_classifier.py`
   - `alphalens/screeners/insider_activity/opportunistic_form4.py`
4. Trigger SEC EDGAR backfill on runpod CPU pod (~3-5 days wall, mounted volume `xymjkwj580`)
5. Phase A check on TRAIN 2009-2017 — A0 density, A1 breadth, A2 direction
6. If Phase A passes → full 5-phase audit on OOS 2018-2023 via `audit_multi_phase.py insider_form4_opportunistic`
7. If OOS PASS or PASS_MARGINAL → final lock single-phase run on 2024-2026
8. Ledger completion via `alphalens preregister complete` with verdict + bounds CI

## 10. Citations

1. Cohen, L., Malloy, C., Pomorski, L. "Decoding Inside Information." *Journal of Finance* 67(3), 2012, pp. 1009-1043. [Primary literature anchor; opportunistic-routine classification framework, Section 3.2 + Table 2 size-sorted results.]
2. Lakonishok, J., Lee, I. "Are Insider Trades Informative?" *Review of Financial Studies* 14(1), 2001, pp. 79-111. [Baseline net-buy signal — predecessor to Cohen-Malloy refinement.]
3. Asness, C., Frazzini, A., Israel, R., Moskowitz, T. "Size Matters, If You Control Your Junk." *Journal of Financial Economics* 129(3), 2018. [70pp R2000 dispersion gate justification — small-cap return vol multiplier.]
4. Romano, J., Wolf, M. "Stepwise Multiple Testing as Formalized Data Snooping." *Econometrica* 73(4), 2005. [Bounds-adjusted CI methodology, project standard.]
5. `docs/research/v9d_retrospective_pre_2018_design_2026_05_05.md` — pre-reg structure template.
6. `docs/research/distress_credit_v1_design_2026_05_04.md` — single-class fresh-design template.
7. `docs/research/pit_audit_2026_04_30_findings.md` — F4 fire-sale exclusion 180d justification + E4 ticker-as-of-filing latent risk.
8. `feedback_burnt_holdout_multiplicity.md` — fresh-class refresh discipline.
9. `feedback_adversarial_review_saves_compute.md` — zen + perplexity pre-run review pattern.

## 11. Lock acknowledgment

This memo is LOCKED 2026-05-05. Modifications to §3 (periods), §4 (signal spec), §5 (architecture), §6 (gates), or §7 (Bonferroni) after this date invalidate the pre-registration. SHA256 hash of corresponding params JSON computed at registration time and stored in ledger entry.
