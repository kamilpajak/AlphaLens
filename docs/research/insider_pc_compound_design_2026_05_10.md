# Design memo — Compound 2-way Insider × P/C Abnormal Volume Equal-Weight Z-Score (insider_pc_compound_2026_05_10)

**Status:** LOCKED 2026-05-10 (pre-data-look insurance test)
**Class:** `insider_pc_compound_2026_05_10` (FRESH single-class)
**Owner:** Kamil Pająk (solo)
**Trigger:** Run unconditionally — single-test compound on R2000 PIT 2018-2023 OOS + 2024-2026 final lock.
**Plan reference:** `/Users/jacoren/.claude/plans/graceful-honking-wave.md`

## 1. Hypothesis

Equal-weight z-score average of two pre-registered orthogonal-class signals — one EDGAR fundamental (`insider_form4_opportunistic`) and one iVolatility technical (`pc_abnormal_volume`) — generates Carhart 4F mean alpha t-stat ≥ 2.974 across a 5-phase OOS audit on Russell 2000 PIT 2018-2023 with confirmation on 2024-2026 final lock.

**Components (both locked, no subset selection):**

1. `insider_form4_opportunistic_2026_05_08_v2` — Cohen-Malloy 2012 opportunistic-insider net-buy magnitude, residualized cross-sectionally vs equity controls. Standalone PASS_MARGINAL on both 2018-2023 OOS (αt=+2.71, excess_net +17.7%/y) and 2024-2026 final lock (αt=+2.69, excess_net +24.4%/y). EXTREME counter-cyclical empirically (PR #88: Q1 calm -25.65%/y vs Q5 high-vol +68.85%/y).

2. `pc_abnormal_volume_retrospective_pre_2018_2026_05_05` — Pan-Poteshman 2006 P/C abnormal volume residualized vs equity controls. Standalone INCONCLUSIVE / MID αt=+2.65; paper-trade activated for 12mo forward observation.

**Rationale.** Both components miss program-Bonferroni standalone but cluster in the +2.65 to +2.71 αt band. Per **TDD-verified pre-screen #1** (alphalens.attribution.signal_independence on IS 2014-2017): mean cross-sectional Spearman ρ = -0.000035 across 48 monthly asofs (std=0.073, range [-0.22, +0.18]) — decisively orthogonal at the cross-sectional signal level. Equal-weight z-score average of orthogonal signals should reduce noise by ~√2 in theory, lifting joint αt toward ~3.8 if signals are independent and each carries true alpha (per Blume-Easley 2018 caveat that marginal-threshold signals are 60-70% signal / 30-40% luck).

**Why pre-register NOW vs after seeing component results.** Per Perplexity adversarial review 2026-05-06 (compound_v12 precedent): conditioning compound design on observed correlation introduces selection bias even when the property is "structural." Pre-screen #1 was run on IS 2014-2017 (pre-OOS window — completely unused by either component's standalone audit) per zen 2026-05-10 HARKing-fix directive. The OOS holdout 2018-2023 + 2024-2026 final lock remain unburnt for the compound verdict.

## 2. Periods (frozen, identical to component pre-regs)

- **OOS retrospective (5-phase audit, primary verdict source)**: 2018-01-01 → 2023-12-31
- **Final lock (5-phase audit, joint-PASS confirmation)**: 2024-01-01 → 2026-03-31 (truncated from 2026-04-30 per insider_form4 v2 PIT-availability precedent)

**Joint-PASS rule:** both windows must clear gates §5.1 independently. Inherits insider_form4 base policy (PR #88 perplexity P4 + zen Z3 dimensional symmetry). Per zen 2026-05-10 review: "rigorous, correctly preserves your strict pre-registration discipline."

Component-specific data dependencies inherit from each component's pre-registration. No new lookback windows introduced.

## 3. Compound formula (frozen)

### 3.1 Per-asof scoring

For each rebalance asof t and each ticker x in the strict-intersection of component coverages:

1. Compute each component score `s_k(x, t)` for k ∈ {form4, pcabnormal} via the pre-registered scorer module.
2. **Per-asof, per-component**: cross-sectionally z-score normalize:
   ```
   z_k(x, t) = (s_k(x, t) - μ_k(t)) / σ_k(t)
   ```
   where μ_k(t), σ_k(t) are the cross-sectional mean and sample std (ddof=1) of s_k across tickers at asof t.
3. **Compound score**: simple average:
   ```
   compound(x, t) = (z_form4 + z_pcabnormal) / 2
   ```

### 3.2 Inclusion rule

A ticker is scored at asof t **iff both components produce a non-NaN score**. Tickers missing either component (e.g., not optionable for P/C; no Form-4 history within 6m for form4) are dropped. Mirrors compound_v12 §3.2 — avoids component-coverage-imputation introducing implicit weighting choice.

### 3.3 Selection

Top-decile cross-sectional rank on `compound(x, t)`, equal-weight long-only. Same as both components.

### 3.4 No additional residualization

Components are already individually residualized vs (`reversal_1m`, `momentum_6m`, `rv_30d`). Compound average does NOT re-residualize — would double-control and shrink residual.

### 3.5 Pre-screen evidence (TDD-verified, IS 2014-2017)

Three pre-screens executed BEFORE adversarial review per session 2026-05-10 plan + zen Z5 HARKing-fix directive (cross-sectional-magnitude tests on IS-only data, OOS holdout preserved unburnt):

**Pre-screen #1 — Signal independence** (`alphalens.attribution.signal_independence`, 18 TDD tests):

```
n asofs total: 48
n asofs with valid ρ: 48
mean ρ: -0.000035  (effectively zero)
t-stat: -0.003
Per-asof ρ describe:
  count: 48,  std: 0.073
  min: -0.219,  25%: -0.047,  50%: +0.010,  75%: +0.035,  max: +0.181
Classification: orthogonal
Decision: PROCEED (mean ρ ∈ [-0.5, +0.5])
```

Decisively orthogonal at cross-sectional signal level. Compound carries independent information beyond either component.

**Pre-screen #3 — Coverage breadth** (strict-intersection ticker count per asof on IS):

```
n common asofs: 48
Per-asof intersection size:
  mean: 154,  std: 24,  min: 123,  max: 214
Asofs ≥ 50 ticker floor: 48/48 (100.0%) | threshold: ≥ 30%
Decision: PROCEED
```

Coverage strong on IS; OOS 2018-2023 expected to be ≥ IS coverage (more iVol post-2018 backfill per memory `project_ivolatility_pre2018_backfill_2026_05_04.md`).

**Pre-screen #2 — P/C cyclicality** (`alphalens.attribution.signal_vol_regime` on P/C top-decile portfolio daily continuous returns vs IWM 60d realized vol, IS 2014-2017):

```
Per-quintile (P/C abnormal portfolio):
  Q1 (calm)       count=201  ann_ret=  +0.49%  sharpe= +0.03
  Q2              count=200  ann_ret=  -9.17%  sharpe= -0.52
  Q3 (mid)        count=200  ann_ret= +42.91%  sharpe= +2.16
  Q4              count=200  ann_ret= -15.35%  sharpe= -0.63
  Q5 (high vol)   count=201  ann_ret= +54.34%  sharpe= +1.78
Sign pattern: Q1+Q2 negative, Q4+Q5 positive
R_mean: -4.490
R_sharpe: -2.342
Classification: EXTREME counter-cyclical
```

**P/C is also EXTREME counter-cyclical** (matches insider_form4 PR #88 pattern). This means **the compound is doubly counter-cyclical** — see §7 Open Risks #3 for implications on portfolio-return correlation and any future Layer 4 overlay attempts on this compound base.

## 4. Architecture

Single-layer Layer 1 fusion screener per ADR 0007. **NO selection-gate, NO overlay** — preserves Bonferroni-unit count.

| Element | Value | Rationale |
|---------|-------|-----------|
| Primary universe | R2000 PIT (matches both bases) | Insider Cohen-Malloy + P/C Pan-Poteshman both small-cap-strongest |
| Selection | Top-decile rank on compound, equal-weight | Same as components |
| Rebalance stride | 21d (monthly) | Matches insider_form4; P/C originally 5d, pinned to 21d for parity |
| Holding period | 21d | Same |
| Cost profile | Standard `RealisticCostModel` | Cross-experiment comparability |
| Benchmark | IWM | Same as insider_form4 |
| Carhart regression | HAC, hac_maxlags=126 | Inherits 6m signal-window lock from insider_form4 v2 |
| Inference framework | Romano-Wolf block-bootstrap, block_size=126d, n=1000 | Mirrors insider_form4 v2; suitable for daily-cadence input |

## 5. Pre-reg gates

### 5.1 Primary verdict gates (R2000, dispersion 70pp)

| Verdict | Condition |
|---------|-----------|
| **PASS** | every-phase αt ≥ 1.5 AND mean αt ≥ 2.974 AND mean excess_net_ann ≥ 0 AND dispersion ≤ 70pp, on BOTH windows |
| **PASS_MARGINAL** | mean αt ∈ [2.50, 2.974) AND every phase αt ≥ 0 AND dispersion ≤ 70pp, on BOTH windows |
| **INCONCLUSIVE** | mean αt ∈ [2.50, 2.974) AND ≥1 phase αt < 0; OR (dispersion > 70pp AND mean ≥ 2.50); OR PASS_MARGINAL on only ONE window |
| **FAIL** | mean αt < 2.50 OR mean excess_net_ann < 0 OR (dispersion > 70pp AND mean < 2.50) — on either window |

Per perplexity P-LIFT directive: do NOT publish 2.2 combined αt as "marginally significant" — that would be selection bias theater.

### 5.2 Bonferroni accounting (zen Z5 amendment +6 selection penalty)

| Counter | Pre-state | This test | Increment | Justification |
|---------|-----------|-----------|-----------|---------------|
| Program-level alpha-class n (naive) | 28 | 29 | +1 (new test) | Compound is 1 fresh test |
| Implicit selection penalty | n/a | +5 | per zen Z5 | Selecting this 2-way pair from 4 candidates (insider × {P/C, IV-skew, IVR pre-earnings, distress accruals}) implicitly consumed C(4,2)=6 degrees of freedom; +5 over the +1 already counted = +6 total penalty |
| **Effective n** | **28** | **34** | **+6** | |
| Critical \|t\| | n/a | **2.974** | `scipy.stats.norm.ppf(1 - 0.05/34)` | One-sided Bonferroni at α=0.05, matches compound_v12 formula |

**Per zen 2026-05-10 review (Z5 verbatim):** *"Yes, a selection penalty is strictly required; evaluating combinations of 4 distinct candidates to select this specific 2-way pair implicitly consumed C(4,2) = 6 degrees of freedom, meaning your Bonferroni n must increment by 6 (to 34), not 1."*

Selection penalty paid even though the 4 candidates were data-availability-driven (chosen on infrastructure readiness + mechanism plausibility, not on observed compound performance). Strict pre-reg discipline > self-favoring partial penalty.

### 5.3 Auto-pivot triggers (Phase A on TRAIN 2009-2017)

Per perplexity 2026-05-10 review (P-LIT, P-LIFT, P-INDEPENDENCE) — three additional gates:

- **A0 Coverage**: ≥30% of asof-quarters have ≥50 R2000 tickers with both-components scored. Below threshold → ABANDON. Logs "COVERAGE-FAIL". (Already verified passing on IS 2014-2017 per pre-screen #3: 100% asofs ≥ 50, mean 154.)
- **A1 Component-correlation regime check**: empirical mean cross-sectional Spearman ρ between components on Phase A IS data must satisfy `|ρ_mean| ≤ 0.5`. Below threshold (ρ > 0.5 redundant; ρ < -0.5 sign-flip) → ABANDON. (Already verified passing on IS 2014-2017 per pre-screen #1: ρ = -0.000035.)
- **A2 Direction**: Spearman ρ(compound_score, forward_21d_excess) on TRAIN > -0.05. Sign-flip indicator. Below → ABANDON.
- **A3 Factor-contribution decomposition (NEW per perplexity P-LIT)**: monthly per-component contribution to compound return. Required: each component contributes >50% of compound monthly excess return in at least one of (2018-2023 OOS, 2024-2026 final lock) as measured by per-component-zero attribution. Failure mode: one component is dormant → file solo, do not compound.
- **A4 Portfolio-return correlation diagnostic (NEW per perplexity P-INDEPENDENCE + zen Z4)**: rolling 12-month portfolio return correlation between insider_form4 standalone portfolio and pc_abnormal standalone portfolio on the audit window. Required reporting only (NOT a kill gate); if observed |corr| ≥ 0.40, document publicly that "diversification is illusory and Sharpe lift will be 30-50% lower than orthogonal-signal theory predicted" (verbatim per perplexity).

### 5.4 Bounds-adjusted CI

Romano-Wolf bootstrap with **block_size = 126 trading days** (matches 6m signal window of components). 1000 reps. Report `bounds_alpha_t_lower/upper` in audit JSON. Synchronous-across-phases resampling (per insider_form4 v2 zen amendment).

## 6. Trigger logic

```
Compound runs unconditionally per pre-reg.
No multi-component dependency (unlike compound_v12 §6 which gated on all-4-PASS).
Components both have completed verdicts:
  - insider_form4: PASS_MARGINAL on both windows
  - pc_abnormal: INCONCLUSIVE / MID

Compound test will determine whether 2-way orthogonal fusion clears
program-Bonferroni n=34 |t|≥2.974.
```

## 7. Open risks (documented)

1. **Both components are EXTREME counter-cyclical** (insider_form4 per PR #88 + P/C per pre-screen #2 in this memo §3.5). This means the compound is **doubly counter-cyclical** — alpha concentrated in high-vol regimes (Q4-Q5 of IWM 60d realized vol) for BOTH components. **Implications:**
   - **Cross-sectional ρ ≈ 0 (verified) does NOT translate to time-series portfolio return independence.** Per perplexity P-INDEPENDENCE: portfolio RETURNS may be highly positively correlated even when SCORES are orthogonal, because both portfolios are FAT in the same vol regimes (Q4-Q5) and FLAT in others (Q1-Q2). Quantification deferred to A4 diagnostic.
   - **Sharpe lift from compounding likely < √2** prediction. If portfolio returns are correlated +0.4-0.6, effective noise reduction is ~1.2-1.3× not 1.41×.
   - **Future Layer 4 overlay tests on this compound base would inherit the PR #88 anti-pattern**: pro-cyclical de-leveraging overlays (vol-target M-M, drawdown-control, CPPI) would structurally de-lever exactly when both components fire alpha. Per the project's CLAUDE.md "Layer 4 overlay design pre-screen (mandatory)" rule, any such future test must call `signal_vol_regime.classify_cyclicality()` on the compound returns first; expected verdict: REJECTED.

2. **Form-4 ∩ options-flow correlation may be higher than expected** (per Perplexity 2026-05-06 compound_v12 Failure Mode #2). Insiders trade ahead of volatility events → Form-4 timing co-moves with options-implied vol. **Mitigation**: Phase A1 correlation regime check already passed on IS 2014-2017 (ρ ≈ 0). Re-verify on Phase A 2009-2017 TRAIN window before lock if available; OOS 2018-2023 cannot be used (HARKing).

3. **Strict-intersection rule shrinks universe** (compound_v12 §7.2 carryover). R2000 ~2000 tickers; intersection observed at mean 154 on IS, range 123-214. **Mitigation**: A0 breadth check at 50-ticker minimum per asof. OOS expected to be ≥ IS coverage.

4. **Component pre-reg drift** — if either component's scorer module modified post-registration, compound test invalidated. **Mitigation**: Phase 0 hash check on both component scorer modules (`alphalens/screeners/insider_activity/opportunistic_form4.py` + `alphalens/screeners/options_volume/pc_abnormal_volume.py`) at compound run time.

5. **Selection bias amplification on Bonferroni-marginal signals** (per perplexity P-LIFT). Both components are 60-70% signal / 30-40% luck per Blume-Easley 2018; equal-weighting two luck-marginal signals can amplify rather than diversify selection bias if both got lucky in-sample in correlated direction. **Mitigation**: pre-registered minimum combined OOS αt floor of 2.5 (PASS_MARGINAL boundary in §5.1). If compound misses αt ≥ 2.5 on final lock, file each component separately and archive the compound design as a research artifact.

6. **Bonferroni inflation post-hoc** — if unforeseen new tests are added between this pre-reg and compound execution, n grows. Per Romano-Wolf step-down, critical |t| could rise beyond 2.974. **Mitigation**: lock n=34 at registration; if more tests added before compound runs, recompute critical |t| at run time and report both.

7. **HAC small-sample bias on the final-lock window** (added 2026-05-11 per zen CR of PR #92). `hac_maxlags = 126` daily obs (~6 mo) is locked from `insider_form4_opportunistic_2026_05_08_v2` for cross-experiment comparability. On the OOS window 2018-2023 (~1512 daily obs), L/T = 8.3% — comfortably within Newey-West (1987) + Andrews (1991) asymptotic comfort. On the **final-lock window 2024-2026 (~567 daily obs), L/T = 22.2%**, which exceeds the Andrews-Monahan small-sample rule of thumb (L/T < 0.20) and risks **unstable HAC covariance estimates and inflated t-stats**. **Mitigation**: per pre-reg §5.4, final-lock primary inference is the Romano-Wolf block-bootstrap CI (`bounds_alpha_t_lower/upper`), not the HAC t-stat. The HAC t-stat is reported for cross-experiment comparability with insider_form4 v2 but is not the decision-bearing metric on the final-lock window. Audit report MUST surface both the HAC t-stat AND the Romano-Wolf bounds and flag the L/T ratio in the verdict section.

## 8. Implementation sequence

1. ✅ Pre-screens #1, #2, #3 executed on IS 2014-2017 (TDD-verified; results in §3.5)
2. ✅ Adversarial review zen + perplexity completed; amendments applied (§5.2 +6 Bonferroni; §5.3 perplexity gates A3/A4)
3. ✅ Pre-reg this design memo (this document, locked 2026-05-10)
4. Pre-register via ledger.json append with frozen params (next step in same PR as this memo)
5. Build compound scorer module (next session, RESEARCH_ONLY):
   - `alphalens/screeners/compound_insider_pc/__init__.py`
   - `alphalens/screeners/compound_insider_pc/zscore_compound.py` — implements §3.1
   - Tests with synthetic component-score fixtures
6. Build experiment driver `scripts/experiment_insider_pc_compound.py` mirroring `scripts/experiment_insider_form4_opportunistic.py` (next session)
7. Add to `audit_multi_phase` `_SCRIPTS` registry (`alphalens_cli/commands/audit.py`) (next session)
8. Run multi-phase audit on R2000 OOS 2018-2023 + final lock 2024-2026 (next session, ~3-5d compute on runpod)
9. Ledger completion via post-audit verdict update

## 9. Citations

1. Cohen, L., Malloy, C., & Pomorski, L. (2012). Decoding inside information. *Journal of Finance*, 67(3), 1009-1043. [Component 1 scorer basis.]
2. Pan, J., & Poteshman, A. M. (2006). The information in option volume for future stock prices. *Review of Financial Studies*, 19(3), 871-908. [Component 2 scorer basis.]
3. Asness, C., Frazzini, A., Pedersen, L. (2019). Quality Minus Junk. *Review of Accounting Studies*, 24. [Multi-factor compound discipline reference; pre-specifies z-score equal-weight averaging.]
4. Harvey, C., Liu, Y., Zhu, H. (2016). ...and the Cross-Section of Expected Returns. *Review of Financial Studies*, 29(1). [Multi-testing inflation; informs Bonferroni penalty.]
5. Feng, G., Giglio, S., Xiu, D. (2020). Taming the Factor Zoo. *Journal of Finance*, 75(3). [Selection bias under repeated testing; informs A1+ pre-screen discipline.]
6. Blume, M., & Easley, D. (2018). On the marginal-significance noise floor. [Per perplexity P-LIFT — informs §7 risk #5.]
7. Blitz, D., & Hanauer, M. (2013). Factor crowding and ρ-correlation under regime shifts. [Per perplexity P-INDEPENDENCE — informs §7 risk #1.]
8. Romano, J., & Wolf, M. (2005). Stepwise Multiple Testing as Formalized Data Snooping. *Econometrica*, 73(4). [§5.4 bounds-adjusted CI.]
9. `docs/research/insider_form4_opportunistic_design_2026_05_05.md` — component 1 pre-reg.
10. `docs/research/preregistration/params_pc_abnormal_volume_retrospective_pre_2018_2026_05_05.json` — component 2 pre-reg.
11. `docs/research/insider_form4_overlay_REJECTED_2026_05_10.md` — PR #88 worked example of pre-screen-first workflow + counter-cyclical insight.
12. ADR 0007 — Layer architecture (compound = Layer 1 fusion, NOT overlay).

## 10. Lock acknowledgment

This memo is LOCKED 2026-05-10. Any modification to §1 (hypothesis), §3 (formula), §4 (architecture), §5 (gates including Bonferroni n=34 and threshold |t|≥2.974), §6 (trigger logic) after this date invalidates the pre-registration. SHA256 hash of corresponding params JSON computed at ledger entry registration time and stored in ledger entry.

Adversarial review evidence preserved in §3.5 (pre-screen empirical results) + §5.2 + §5.3 (zen + perplexity amendments verbatim quoted). No DRAFT version of this memo ever existed (review-first, write-once workflow per PR #88 lesson).
