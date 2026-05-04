# Distress × Credit Regime — Experiment Design (v1, 2026-05-04)

**Status:** LOCKED 2026-05-04 (pending pre-reg ledger registration)
**Class:** `distress_credit_search_2026_05_04` (NEW, first in class)
**Pre-reg id:** `distress_credit_v1_2026_05_04`

## 1. Context (why this experiment)

After 17/17 completed pre-registered FAILs across 6 burnt classes (alt_data 6/6, options_implied 5/5, price_factor 4/4, multi_source 3/3, event_drift 2/2 BREADTH-FAIL, risk_overlay 1/1) plus 6 PROSPECTIVE/ABANDONED entries (n=23 in ledger), program-level Bonferroni accumulated to a level where the cross-sectional attack surface on the burnt 2024-04-30 → 2026-04-30 holdout is empirically saturated.

This experiment introduces a **structurally novel attack surface**: compound Layer 2 (Screener) × Layer 4 (Risk overlay) hypothesis per ADR 0007, using **two feature spaces never touched in any prior experiment**: `us-gaap:Liabilities` (leverage) via naive KMV Merton distance-to-default + ICE BofA US High Yield OAS macro regime gate.

## 2. Hypothesis

**H1 (PRIMARY).** Long-only equal-weighted bottom-quintile Merton-PD portfolio drawn from S&P 1500 PIT (excluding top-50 mega-caps and excluding top-quintile distress) — with portfolio dollar exposure modulated by HY OAS z-score (defensive sizing during stress regimes per Frazzini-Pedersen 2014 BAB convention) — produces:

- **mean Carhart-4F α t-stat ≥ 3.50** across 5-phase OOS audit on holdout 2024-04-30 → 2026-04-30, AND
- **Sharpe-improvement ≥ 0.50** vs Carhart-4F-residualized SP1500 buy-hold baseline (Sharpe primary per ADR 0007 since Layer 4 overlay is present), AND
- every-phase α t-stat ≥ 0, AND
- α t-stat dispersion across 5 phases ≤ 0.5 (per amended dispersion gate from v9), AND
- excess_net_ann dispersion ≤ 50pp.

## 3. Adversarial review (Perplexity Sonar Reasoning Pro, 2026-05-04, high effort)

Two FATAL flaws + five mitigable concerns identified.

### FATAL #1 — Exposure mapping inversion in post-shock recovery regimes

Wider HY OAS spreads can coincide with rising equity returns during recovery dynamics (post-2008-Q1, post-2020-Q1). A linear-interp gate that reduces exposure when spreads widen would **invert sign at exactly the wrong moment**, structurally underperforming buy-hold.

**Mitigation: pre-commit Phase A overlay sanity gate.** Phase A check A4-extended computes correlation(spread_z_t, forward_21d_market_return_t+1:t+21) over rolling 60-month windows on TRAIN (2017-01 → 2024-04-29). Pre-committed exit rule:
- If full-sample correlation is **positive** (i.e., wider spread predicts HIGHER returns — overlay would invert) → **DROP Layer 4 from PRIMARY**, run pure-Layer-2 long-only safe-decile as primary instead. Document Layer 4 as RESEARCH_ONLY artifact.
- If correlation flips sign across decade-windows (>0.4 absolute drift) → same drop.
- If correlation is stably negative (mean ≤ −0.05, regime-stable) → KEEP Layer 4 in PRIMARY.

This contingency is pre-committed in the params JSON (`success_criteria.auto_pivot_triggers.l4_overlay_sanity_failed`). The final hypothesis text therefore covers both branches: PRIMARY with overlay if A4-extended PASSes; PRIMARY without overlay if FAILs. Either way is honest pre-registration since the contingency is locked before any holdout look.

### FATAL #2 — Bonferroni n=24 doesn't account for hyperparameter grid

Distress quintile threshold and HY OAS gate boundaries (z=±1) are tuned hyperparameters. Implicit grid search would inflate n.

**Mitigation: hyperparameters locked to literature values pre-look.** No sensitivity analysis as confirmatory.
- **Distress quintile = 20%** (Fama-French quintile standard, Bali-Cakici-Whitelaw 2011 MAX-effect convention, Frazzini-Pedersen 2014 BAB long-leg convention).
- **HY OAS gate z=±1** (1-sigma standard threshold, Frazzini-Pedersen 2014 BAB regime-gate, Asness-Frazzini-Pedersen 2019 QMJ).
- **Min exposure floor 0.5** at z=+1 (50% defensive allocation, Moreira-Muir 2017 vol-target convention with a slightly relaxed floor — Moreira-Muir uses 1/σ² capped which can de-lever further; we cap at 0.5 to keep exposure variance modest).
- **Top-50 megacap exclusion** (capacity convention, parity with prior pre-regs).
- **Rebalance stride 21d** (monthly convention, parity with multi-phase audit precedent).

These values are locked at design time and any post-hoc deviation invalidates registration.

### Mitigable concerns documented in memo

1. **Naive KMV adequacy** (Bharath-Shumway 2008 ρ≈0.95 for rank). For highly leveraged firms σ_E ≠ σ_V, possible rank flip near quintile boundary. Phase A check A6-extended computes Kendall-τ between naive-KMV-PD and a one-shot iterative-KMV-PD on a 50-name sample at TRAIN end-point. Reported as descriptive only; if τ < 0.92 noted as caveat in verdict memo. NOT a primary gate.

2. **SP1500 PIT survivorship bias.** Existing 100-300 bps/y caveat documented in `alphalens/data/universes/sp1500_pit.py`. The bias asymmetrically favors low-distress names (winners-only set) — directly inflates apparent safe-decile premium. Acknowledged: this raises ex-ante pass-prob expectation but also means a PASS verdict requires careful interpretation. PASS triggers prospective replication on accruing post-2026-04-30 data; capital deploy OFF-TABLE on this holdout regardless of verdict per program policy.

3. **Carhart-4F vs 6F (4F + QMJ + BAB) control sufficiency.** Low-distress is mechanically correlated with low-vol/quality/profitability premia. Standard 4F partially absorbs but not fully. **Decision:** PRIMARY metric is Carhart-4F α t-stat (parity with all prior pre-regs). 6F α t-stat reported as **descriptive sensitivity only**, NOT a primary gate. If 6F α t-stat drops by >1.0 below 4F α t-stat, flagged in verdict memo as "signal partially attributable to BAB/QMJ premia."

4. **FRED revision risk for HY OAS.** BAMLH0A0HYM2 is revised but typically <5bps. Acknowledged limitation; no ALFRED vintage retrieval in this iteration. If post-hoc divergence ≥0.20 z-score units detected in any audit period, treated as descriptive caveat.

5. **2024-2026 holdout regime unfavorability.** Mid-cap rally + spread compression + mega-cap-tech surge make the regime structurally **unfavorable** for safe-decile + spread-overlay hypothesis. Honest ex-ante pass-prob revised downward to **8-12%** (vs 15-20% from original plan). Documenting this here means a PASS would be meaningful; a FAIL was predicted.

## 4. Implementation specifics

### Layer 2 — Merton distance-to-default (naive KMV)

```
d2 = (ln(V/D) + (r − 0.5·σ²)·T) / (σ·√T)
PD = N(−d2)
score = −PD  # engine top_n picks lowest-PD = safest
```

Where:
- V = equity_mcap + total_liabilities (naive KMV; σ_V ≈ σ_E documented limitation)
- D = `us-gaap:Liabilities` PIT-filtered (filed_date ≤ asof, latest filed per period_end)
- σ = annualized 60-day realized daily log-return vol (`np.log(close/close_prev).std() × √252`)
- r = DGS1 (1-year T-bill rate, decimal)
- T = 1.0 year

Adapter zeroes scores outside bottom quintile by PD; engine selects via top_n (long-only equal-weight).

### Layer 4 — Credit regime overlay

```
spread_z(t) = (HY_OAS_t − MA252(spread, t<t)) / std252(spread, t<t)   # strict-history
exposure(z) = 1.00 if z ≤ -1.0
              0.75 if z = 0
              0.50 if z ≥ +1.0
              linear interp otherwise
              1.00 if z is None (warmup < 252 trading days available)
adjusted_returns(t) = exposure · port_returns(t) + (1 − exposure) · cash_rate(t)
```

Where:
- HY_OAS = FRED series `BAMLH0A0HYM2` (ICE BofA US High Yield Master OAS)
- cash_rate(t) = DGS3MO daily decimal / 252 trading days

Overlay applied OUTSIDE the engine in the experiment driver, NOT modifying scorer ranks.

### Universe

- S&P 1500 PIT (loader: `alphalens.data.universes.sp1500_pit`, sparse-snapshot caveat ack)
- Exclude top-50 mcap (capacity)
- Exclude top quintile distress (always — defensive prior, fixed at 20% literature)
- Min bottom-quintile breadth ≥ 20 names per asof (Phase A gate A8)
- Min total scored ≥ 100 names per asof (Phase A gate A8)

### Backtest config

- TRAIN: 2017-01-03 → 2024-04-29
- HOLDOUT: 2024-04-30 → 2026-04-30
- Rebalance stride: 21 trading days (monthly)
- Holding: 21 trading days (no overlap)
- Position weighting: equal-weight in bottom quintile
- 5-phase OOS audit, phase_offset ∈ {0, 4, 8, 12, 16}
- Cost model: half-spread 5bps per side (10bps round-trip per re-trade)

## 5. Pre-commit gates (locked in `params_distress_credit_v1_2026_05_04.json`)

| Verdict | Required (ALL conditions) |
|---------|---------------------------|
| **PASS** | mean Carhart-4F αt ≥ 3.50 AND every phase αt > 0 AND Sharpe-improvement ≥ 0.50 over Carhart-4F-residualized SP1500 baseline AND αt-dispersion ≤ 0.5 AND excess_net_ann dispersion ≤ 50pp |
| **MID** | 1.5 ≤ mean αt < 3.50, OR 0.30 ≤ Sharpe-improvement < 0.50, OR αt-dispersion > 0.5 (but mean ≥ 1.5), OR excess_net_ann dispersion > 50pp (but mean ≥ 1.5) |
| **FAIL** | any phase αt < 0, OR mean αt < 1.5, OR Sharpe-improvement < 0.30 |

Bonferroni accounting:
- In-class n=1 (first in class) → in-class threshold 1.96 (function output)
- Program-level n=24 (23 prior entries + this one) → naive Bonferroni 3.08 (function output)
- **PRIMARY threshold |t|≥3.50** = escalated above naive program-level for meta-multiplicity (parity with event_drift_v4 escalation 3.34→3.50)

## 6. Phase A engineering smoke (TRAIN only — diagnostic only)

8 checks emitted as JSON reports under `docs/research/distress_credit/phase_a_*.json`:

| # | Check | Pass criterion |
|---|-------|----------------|
| A1 | Feature joiner sanity (4 tickers × 1 asof) | All 4 inputs (mcap, liab, σ, r) populated, finite |
| A2 | Coverage audit per rebalance month 2017-01..2024-04 | ≥80% SP1500 names have all 4 inputs at month-end |
| A3 | PIT correctness (filed_date ≤ asof enforced via `_pit_filter`) | Asserted via direct call test |
| A4 | HY OAS sanity (no NaN gaps >5BD; z-score finite for all rebalances) | All gaps ≤5BD, all z finite |
| A4-extended | **HY OAS overlay sanity** (correlation(spread_z, forward 21d market return) on TRAIN, rolling 60-month windows) | Mean correlation ≤ −0.05 AND no decade-window flips >0.4. **FAIL → drop L4 from PRIMARY** (auto-pivot trigger) |
| A5 | Universe size after exclusions per asof | ≥150 names |
| A6 | IC decay (rank-IC of −PD vs forward-1d, 5d, 21d, 63d returns) | Monotone non-zero at 21d |
| A6-extended | Naive vs iterative-KMV Kendall-τ (50-name sample at TRAIN end) | τ ≥ 0.92 (descriptive only) |
| A7 | Smoke backtest TRAIN end-to-end (2017-01-03 → 2024-04-29, monthly stride) | Engine completes without error, ≥80 rebalances logged |
| A8 | Breadth gate per asof | scored_count ≥ 100 AND bottom-quintile size ≥ 20 |

Phase A diagnostic outputs MUST NOT change hypothesis or thresholds (those are locked in pre-reg). The ONLY pre-committed action from Phase A is the A4-extended auto-pivot to drop L4 if overlay fails sanity.

## 7. Phase B holdout audit (one-shot, runpod)

```bash
.venv/bin/python scripts/audit_multi_phase.py distress_credit_v1 \
  --is-start 2017-01-03 --is-end 2024-04-29 \
  --oos-start 2024-04-30 --oos-end 2026-04-30 \
  --rebalance-stride 21 --phases 0,4,8,12,16 \
  --cost-half-spreads 5 \
  --out docs/research/distress_credit/audit_holdout.json
```

Adds `distress_credit_v1` to `_SCRIPTS` dict in `scripts/audit_multi_phase.py`; adds `--phases "0,4,8,12,16"` flag (subset selection from the existing `range(stride)` sweep).

## 8. Pre-reg ledger lifecycle

```bash
# Threshold check (in-class first-entry returns 1.96; program-level 3.50 documented in params JSON)
.venv/bin/alphalens preregister threshold --signal-class distress_credit_search_2026_05_04

# Lock entry
.venv/bin/alphalens preregister add \
    --id distress_credit_v1_2026_05_04 \
    --signal-class distress_credit_search_2026_05_04 \
    --hypothesis-file docs/research/preregistration/hypothesis_distress_credit_v1_2026_05_04.md \
    --scorer-path alphalens.screeners.distress_credit.scorer.distress_credit_adapter \
    --params-file docs/research/preregistration/params_distress_credit_v1_2026_05_04.json \
    --is-start 2017-01-03 --is-end 2024-04-29 \
    --oos-start 2024-04-30 --oos-end 2026-04-30

# After holdout audit
.venv/bin/alphalens preregister complete distress_credit_v1_2026_05_04 \
    --verdict {PASS|MID|FAIL} \
    --headline-file docs/research/distress_credit/audit_holdout.json
```

## 9. Capital deployment clause

Capital deploy is OFF-TABLE on this burnt holdout regardless of any verdict per program-level burnt-holdout policy. PASS triggers prospective walk-forward replication on data accruing post-2026-04-30 at unadjusted p<0.05 single-test before any escalation to capital deployment.

## 10. Pass-probability honest expectation

Per perplexity adversarial review with full regime context:
- Original 15-20% (vanilla classical anomaly base rate) revised downward to **8-12%** for 2024-2026 holdout regime (mid-cap rally + spread compression + mega-cap-tech).
- FAIL is the most likely outcome and is informational diagnostic — establishes leverage-feature-space + macro-regime-overlay attack surface as either weak or absent for the specific 2024-2026 regime.
- MID verdict (1.5 ≤ αt < 3.50) documents existence of real-but-modest signal in novel architecture, informs future iterations of the class.
- PASS verdict would be the FIRST in the program; would require external prospective replication before capital deploy.

## 11. Out of scope

- Long-short variant (separate pre-reg entry — would burn additional ledger slot)
- Full KMV iterative asset-vol unwind in production scoring (one-shot 50-name sample only as Phase A descriptive)
- ALFRED vintage HY OAS retrieval (HY OAS rarely revised >5bps; standard cache acceptable)
- Capacity/turnover modeling beyond 5bps half-spread
- 6F (4F+QMJ+BAB) as primary metric (descriptive sensitivity only)
- Sensitivity analysis on quintile threshold or z-boundary parameters as confirmatory (locked to literature)
