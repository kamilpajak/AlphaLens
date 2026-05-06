# Design memo — Compound v12 Informed-Trader Equal-Weight Z-Score (compound_v12_informed_trader_2026_05_06)

**Status:** LOCKED 2026-05-06 (pre-data-look insurance test)
**Class:** `compound_v12_informed_trader_2026_05_06` (FRESH single-class)
**Owner:** Kamil Pająk (solo)
**Trigger:** Run ONLY if at least 1 of 4 components fails to PASS standalone after their respective audits.
**Plan reference:** `/Users/jacoren/.claude/plans/sunny-brewing-biscuit.md` (extended scope)

## 1. Hypothesis

Equal-weight z-score average of four pre-registered informed-trader-flow scorers, residualized cross-sectionally vs equity controls, generates Carhart 4F mean alpha t-stat ≥ 2.94 across a 5-phase OOS audit on Russell 2000 PIT 2018-2023.

**Components (all locked at design time, no subset selection):**

1. `v8_xing_frozen_direction_2026_05_03` — Xing 2010 model-free options-implied vol skew (ivp30-based direct replication)
2. `v9d_retrospective_pre_2018_2026_05_05` — cross-sectional residual of -ivp30 vs equity controls
3. `pc_abnormal_volume_retrospective_pre_2018_2026_05_05` — P/C abnormal volume residualized vs equity controls
4. `insider_form4_opportunistic_2026_05_05` — Cohen-Malloy 2012 opportunistic-insider net-buy magnitude residualized

**Rationale.** Three options-class signals (v8, v9D, v11) all show αt ∈ [+2.18, +2.65] standalone — **near-misses below program-Bonferroni bar +2.86**. Form-4 (insider_form4) tests fundamentally different feature class with expected lower mechanism correlation (ρ ≈ 0.2-0.4 vs within-options 0.5-0.7). Per Perplexity adversarial review 2026-05-06: combined αt could plausibly clear PASS bar IF cross-class correlation is sufficiently low AND each component remains directionally positive.

**Why pre-register NOW vs after seeing component results.** Per Perplexity deep-reasoning review (Feng-Giglio-Xiu 2020 JF): conditioning compound design on observed correlation introduces selection bias even when "structural" property. Locking design BEFORE component results = no data-dependent decision points = no HARKing.

**Trigger discipline.** Compound runs ONLY if any component fails to PASS standalone. Save Bonferroni budget if singles already win. This gating is structural (depends on PASS/FAIL classification, not magnitude) — does not introduce data-dependent design choice.

## 2. Periods (frozen, identical to component pre-regs)

- **OOS retrospective (5-phase audit, primary verdict source)**: 2018-01-01 → 2023-12-31
- **Final lock (single-phase, prose-discount-flagged)**: 2024-01-01 → 2026-04-30

Component-specific data dependencies inherit from each component's pre-registration. No new lookback windows introduced.

## 3. Compound formula (frozen)

### 3.1 Per-asof scoring

For each rebalance asof t and each ticker x in the intersection of component coverages:

1. Compute each component score s_k(x, t) for k ∈ {v8, v9D, v11, form4} via the pre-registered scorer module.
2. **Per-asof, per-component**: cross-sectionally z-score normalize:
   ```
   z_k(x, t) = (s_k(x, t) - μ_k(t)) / σ_k(x, t)
   ```
   where μ_k(t), σ_k(t) are the cross-sectional mean and sample std (ddof=1) of s_k across tickers at asof t. Robust standardization NOT used (locks raw OLS distribution per component).
3. **Compound score**: simple average:
   ```
   compound(x, t) = (z_v8 + z_v9D + z_v11 + z_form4) / 4
   ```

### 3.2 Inclusion rule

A ticker is scored at asof t **iff all 4 components produce a non-NaN score**. Tickers missing any component (e.g., not optionable for v8/v9D/v11; no Form-4 history for form4) are dropped.

**Justification of strict-intersection rule**: avoids component-coverage-imputation introducing implicit weighting choice. Forces honesty about effective universe coverage. Phase A0 breadth check enforces minimum density.

### 3.3 Selection

Top-decile cross-sectional rank on `compound(x, t)`, equal-weight long-only. Same as components.

### 3.4 No additional residualization

Components are already individually residualized vs (`reversal_1m`, `momentum_6m`, `rv_30d`). Compound average does NOT re-residualize — would double-control and shrink residual.

## 4. Architecture

Single-layer Layer 2 compound screener per ADR 0007. **NO selection-gate, NO overlay** — preserves Bonferroni-unit count.

| Element | Value | Rationale |
|---------|-------|-----------|
| Primary universe | R2000 PIT (matches insider_form4) | Cohen-Malloy + Xing both small-cap-strongest |
| Selection | Top-decile rank on compound, equal-weight | Same as components |
| Rebalance stride | 21d (monthly) | Matches all 4 components |
| Holding period | 21d | Same |
| Cost profile | Standard `RealisticCostModel` | Cross-experiment comparability |
| Benchmark | IWM | Same as insider_form4 |
| Carhart regression | HAC, hac_maxlags=126 | Inherits 6m signal-window lock |

## 5. Pre-reg gates

### 5.1 Primary verdict gates (R2000, dispersion 70pp)

| Verdict | Condition |
|---------|-----------|
| **PASS** | every-phase αt ≥ 1.5 AND mean αt ≥ 2.94 AND mean excess_net_ann ≥ 0 AND dispersion ≤ 70pp |
| **PASS_MARGINAL** | mean αt ∈ [2.50, 2.94) AND every phase αt ≥ 0 AND dispersion ≤ 70pp |
| **INCONCLUSIVE** | mean αt ∈ [2.50, 2.94) AND ≥1 phase αt < 0; OR (dispersion > 70pp AND mean ≥ 2.50) |
| **FAIL** | mean αt < 2.50 OR mean excess_net_ann < 0 OR (dispersion > 70pp AND mean < 2.50) |

### 5.2 Bonferroni accounting

- **Intra-class n**: 1 (fresh single-class)
- **Program-level n at registration**: 29 (28 prior ledger entries + this compound test)
- **Critical |t|**: 2.94 = `scipy.stats.norm.ppf(1 - 0.025/29)` (one-step-larger than insider_form4's 2.86)
- **Selection penalty**: ZERO. Compound has no data-dependent design choices (formula locked, all 4 components included, equal weighting). Trigger condition (≥1 component fails standalone) is structural classification, not magnitude-dependent.

### 5.3 Auto-pivot triggers (Phase A on TRAIN 2009-2017)

- **A0 Coverage**: ≥30% of asof-quarters have ≥50 R2000 tickers with all-4-components scored. Below threshold → ABANDON (insufficient strict-intersection breadth). Logs "COVERAGE-FAIL".
- **A1 Component-correlation regime check**: empirical pairwise ρ between any 2 components on TRAIN must satisfy `min(ρ_pairs) > -0.5 AND max(ρ_pairs) < 0.95`. Outside this band suggests degenerate composition (extreme anti-correlation = signs flipped; near-perfect correlation = redundant signal). Below threshold → ABANDON, log "CORRELATION-DEGENERATE". **NOT a low-correlation gate** — rule excludes only pathological values, accepts anything in [−0.5, 0.95].
- **A2 Direction**: Spearman ρ(compound_score, forward_21d_excess) on TRAIN > −0.05. Sign-flip indicator. Below → ABANDON.

### 5.4 Bounds-adjusted CI

Romano-Wolf bootstrap with **block_size = 126 trading days** (matches 6m signal window of components). 1000 reps. Report `bounds_alpha_t_lower/upper` in audit JSON.

## 6. Trigger logic

```
After all 4 components have ledger.outcome.verdict assigned:
  if all 4 components verdict in {PASS, PASS_ROBUST}:
    skip compound (we already have winner; save Bonferroni budget)
    log "compound v12 SKIPPED — single-component PASS"
  else:
    run compound
```

If insider_form4_opportunistic FAIL → at least 1 of 4 fails → compound runs.
If insider_form4_opportunistic PASS but v8/v9D/v11 INCONCLUSIVE → compound runs (4-signal compound legitimate test).
If all 4 PASS standalone → compound skipped (no marginal value).

## 7. Open risks (documented)

1. **Form-4 ∩ options-flow correlation may be higher than expected** (per Perplexity Failure Mode #2). Insiders trade ahead of volatility events → Form-4 timing co-movs with options-implied vol. **Mitigation**: Phase A1 correlation regime check. If pathologically correlated → ABANDON.
2. **Strict-intersection rule shrinks universe**. R2000 ~2000 tickers; intersection ~600-1200 expected (optionable + Form-4 active). **Mitigation**: A0 breadth check at 50-ticker minimum per asof.
3. **Component pre-reg drift** — if any of 4 components has scorer module modified post-registration, compound test invalidated. **Mitigation**: Phase 0 hash check on all 4 component scorer modules at compound run time.
4. **Per-component PIT discipline different** — each component has its own data lookback (Form-4: 6m, options: 30d/60d). Compound respects each individually; no cross-component data leak. Locked at component-level pre-regs.
5. **Bonferroni inflation post-hoc** — if unforeseen new tests are added between this pre-reg and compound execution, n grows. Per Romano-Wolf step-down, critical |t| could rise beyond 2.94. **Mitigation**: lock n=29 at registration; if more tests added before compound runs, recompute critical |t| at run time and report both.

## 8. Implementation sequence

1. ✅ Pre-reg this design memo (this document, locked 2026-05-06)
2. Pre-register via `alphalens preregister add` with frozen params JSON (next step)
3. Wait for Form-4 backfill completion (~3-5 days VPS, in progress)
4. Run insider_form4_opportunistic standalone audit per its pre-reg
5. **Trigger evaluation** (after all 4 components have verdicts):
   - Skip compound if all 4 PASS
   - Run compound otherwise per implementation in step 6
6. Build compound scorer module (pending; not yet implemented):
   - `alphalens/screeners/compound_informed_trader/__init__.py` (RESEARCH_ONLY)
   - `alphalens/screeners/compound_informed_trader/zscore_compound.py` — implements §3.1
   - Tests with synthetic component-score fixtures
7. Build experiment driver `scripts/experiment_compound_v12_informed_trader.py` mirroring `experiment_insider_form4_opportunistic.py`
8. Add to `audit_multi_phase` `_SCRIPTS` registry
9. Run multi-phase audit on R2000 OOS 2018-2023
10. Final lock 2024-2026 single-phase if PASS or PASS_MARGINAL
11. Ledger completion via `alphalens preregister complete`

## 9. Citations

1. Asness, C., Frazzini, A., Pedersen, L. "Quality Minus Junk." *Review of Accounting Studies* 24, 2019. [Multi-factor compound discipline reference; pre-specifies z-score equal-weight averaging.]
2. Harvey, C., Liu, Y., Zhu, H. "...and the Cross-Section of Expected Returns." *Review of Financial Studies* 29(1), 2016. [Multi-testing inflation argument; informs Bonferroni penalty.]
3. Feng, G., Giglio, S., Xiu, D. "Taming the Factor Zoo." *Journal of Finance* 75(3), 2020. [Selection bias under repeated testing; informs no-conditioning discipline.]
4. Romano, J., Wolf, M. "Stepwise Multiple Testing as Formalized Data Snooping." *Econometrica* 73(4), 2005. [Bounds-adjusted CI methodology.]
5. `docs/research/insider_form4_opportunistic_design_2026_05_05.md` — component #4 pre-reg.
6. Component pre-reg JSONs for v8, v9D, v11 in `docs/research/preregistration/`.

## 10. Lock acknowledgment

This memo is LOCKED 2026-05-06. Any modification to §1 (hypothesis), §3 (formula), §4 (architecture), §5 (gates), §6 (trigger logic) after this date invalidates the pre-registration. SHA256 hash of corresponding params JSON computed at `alphalens preregister add` time and stored in ledger entry.
