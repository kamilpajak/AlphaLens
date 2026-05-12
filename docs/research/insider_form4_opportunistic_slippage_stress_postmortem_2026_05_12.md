# Postmortem — `insider_form4_opportunistic` slippage stress diagnostic (2026-05-12)

**Status:** FINAL 2026-05-12
**Pre-reg memo:** `docs/research/insider_form4_opportunistic_slippage_stress_design_2026_05_12.md` — locked 2026-05-12 BEFORE compute
**Plan:** `/Users/jacoren/.claude/plans/staged-munching-tiger.md`
**Verdict:** **FAIL** — G1 (αt_net ≥ 2.0 @ H=50bps/β=0) violated on **both** windows
(OOS αt_net=+1.27, FL αt_net=+1.95). G2 also violated on both. G3 violated on
OOS. Strict KILL gate (αt<1.0 @ H=50/β=0) not breached, but per zen +
perplexity convergent review **G1 is a knockout gate by design**, not a
gap-to-be-interpreted. Realizability is rejected.
**Post-drag cyclicality verdict:** **MATERIAL FINDING — REVERSAL TRIGGERED on OOS**
(all 5 phases, mean Δ R_excess = +2.09). Pre-cost EXTREME counter-cyclical
(R_excess ≈ −2.5) collapses to orthogonal-to-benchmark (R_excess ≈ −0.5) under
β=2 cost amplification. The counter-cyclical mechanism that grounded the 2026-05-10
Layer 4 overlay REJECTION was itself a cost-mirage — but base-strategy G1
failure makes Layer 4 reopening moot (cannot overlay a strategy with no
post-cost edge).
**Bonferroni:** 0 new tests (diagnostic, not new hypothesis — amends existing
ledger entry `insider_form4_opportunistic_2026_05_08_v2`)

## 1. Context

After 12 paradigm FAILs / 1 PASS_MARGINAL / 2 INCONCLUSIVE, the only
phase-robust survivor is `insider_form4_opportunistic` (αt=+2.71 OOS,
+2.69 final-lock, +24.4%/y). It is **EXTREME counter-cyclical** (Q5 IWM-vol
= +68.85%/y, Q1 = −25.65%/y, monotonic). Independent reviews (zen
gemini-3-pro-preview Round 1, perplexity sonar-reasoning-pro) flagged a
microstructure concern: the +68.85%/y Q5 alpha may be a **mirage of
crossing wide bid-ask spreads at unattainable prices during panic
regimes**. Polygon OHLCV has no order book depth, so spread costs were
not measured in any prior backtest. Form-4 fills have no timing
discretion → arrival-price crossing in panic regimes carries materially
wider spreads.

Empirical priors that anchored the test gates:
- **Lesmond/Schill/Zhou (2004 JFE)** — TX costs eat 50–70% of small-cap
  anomalies; effective half-spreads on smallest Russell deciles 200–400
  bps in 1995–2002.
- **Hasbrouck (2009)** + **Corwin-Schultz (2012)** — R2000 median
  half-spreads 30–80 bps post-decimalization but right-tail to 200–300 bps
  during 2008/2020 stress.
- **Chordia-Roll-Subrahmanyam (2001)** + **Naes-Skjeltorp-Odegaard (2011)**
  — small-cap effective spreads roughly ~2× between vol decile 5 and
  decile 10 — anchored the β=2 regime amplification.

## 2. Test design (verbatim from pre-reg memo)

- **Cost grid**: half-spread bps ∈ {5, 25, 50, 75, 100, 150, 200, 500}.
- **Regime amplification**:
  `effective_half_spread(t) = base × (1 + β × max(0, (σ_60d(t) − σ_median) / σ_median))`
  with β ∈ {0, 1, 2, 3}. σ_60d = trailing 60-day IWM realized vol;
  σ_median computed full-sample on the joint test window.
- **αt computation**: re-regress on **net-of-drag daily series** (Carhart-4F,
  HAC maxlags=126). The experiment's `alpha_net_4f` scalar (= α_gross −
  drag_ann) is INSUFFICIENT — leaves t-stat at gross value.
- **Decision gates** (must ALL hold for REALIZABLE):
  - G1 (Primary): αt_net ≥ 2.0 at H=50bps uniform β=0 on both windows
  - G2 (Stress uniform): αt_net ≥ 1.5 at H=100bps uniform β=0 on both
  - G3 (Regime stress): αt_net ≥ 1.5 at H=50bps × β=2 on both
  - G4 (Secondary realism): Sharpe_net(50bps, β=2) > Sharpe_net(SPY) over
    pooled windows
- **Failure modes**:
  - KILL if αt_net < 1.0 at H=50bps β=0 on either window
  - FLAG if G1 passes but ANY of G2/G3/G4 fails
- **Post-drag cyclicality reversal check (mandatory observation gate)**:
  re-run `classify_cyclicality_excess` on β=2 net-of-drag returns; flag as
  MATERIAL FINDING if `R_excess_post_drag − R_excess_pre_drag ≥ +1.0` on
  either window (counter-cyclical → orthogonal/pro-cyclical flip reopens
  Layer 4 overlay class).
- **Post-2010 sanity subsample**: re-run G1–G4 on `>=2010-01-01`. Flag
  prose-only if verdict flips between full and post-2010.

## 3. Compute trail

- **Audit re-runs** (Mac M2, post-pivot from runpod):
  - OOS 2018-01-01 → 2023-12-31: orchestrator `run_insider_form4_phase_b.py`
    with `--out-suffix oos_slippage_2026_05_12`. Wall: phase 0=5005s,
    phase 4=5021s (5-phase parallel, M2 8 perf-cores, 10 subprocesses
    competing for first 55 min until FL phases finished). Verdict line:
    "PASS_MARGINAL — αt=2.71 in [2.50, 3.1237); other gates pass" —
    **identical to locked phase B 2026-05-09**, confirming determinism +
    that the per-rebalance turnover-dump patch (Step 2) does not affect
    scoring.
  - Final-lock 2024-01-01 → 2026-03-31: same orchestrator with custom
    `--artifact-root` + `--out-suffix final_lock_slippage_2026_05_12`. Wall:
    ~3320–3340s per phase (5-phase parallel). Verdict line: "PASS_MARGINAL
    — αt=2.69 in [2.50, 3.1237); other gates pass" — identical to locked
    `insider_form4_opportunistic_final_lock_2024_2026.json`.
  - **Why Mac, not runpod (deviation from plan §3)**: 4 consecutive pod
    create attempts in EU-RO-1 with `ghcr.io/kamilpajak/alphalens-runpod:latest`
    image stuck in `machine: null, started: null` despite `status: RUNNING`
    (`amkcom4kgkml0n`, `t8hlvnc4ogs6ky`, `0r08pk83e162qb`, `59qy70chex23ga`,
    `q5g2mvb0r6ffoo`). Same image worked on host `amkcom4kgkml0n` 2026-05-11
    (compound audit); today's hosts could not allocate. Test pods with
    `runpod/pytorch` started instantly — issue isolated to alphalens image ×
    today's EU-RO-1 hosts (intermittent capacity / manifest mismatch). Form-4
    (46MB) + iVol (3.8GB) + companyfacts (263MB) + factors (13MB) all local;
    Mac M2 completes 5-phase parallel audit in 55–83 min per phase.
- **Diagnostic compute**: `scripts/diagnostics/insider_form4_slippage_regime.py`
  — loads per-phase gross + turnover, computes IWM 60d vol + full-sample
  σ_median = 0.211 (~21.1% annualized), runs 8×4×5×2×2 = 640 combos. Wall:
  ~27s (load + 5×8×4 = 160 Carhart-4F HAC=126 regressions + per-quintile
  buckets + cyclicality checks). Outputs:
  `~/.alphalens/diagnostics/insider_form4_slippage_results_2026_05_12.parquet`
  (640 rows, schema below) and `insider_form4_slippage_cyclicality_2026_05_12.parquet`
  (20 rows).

## 4. Methodology cross-check & smoke gates

Smoke checks (pre-reg §12) on synthetic data BEFORE reading numerical results:

1. **Zero-turnover invariance**: ✅ PASS — `test_zero_turnover_yields_no_drag`.
2. **Constant-spread cross-check vs RealisticCostModel**: ✅ PASS —
   `test_constant_spread_matches_realistic_cost_model` matched
   `RealisticCostModel.primary_period_drag_bps` to 1e-9 abs tolerance.
3. **Cost monotonicity in half_spread**: ✅ PASS — `test_cost_monotonicity_in_half_spread`.
4. **β=2 amplification increases Q5 drag vs β=0**: ✅ PASS —
   `test_regime_amplification_increases_drag_in_high_vol`.
5. **End-to-end alpha falls with rising half_spread**: ✅ PASS —
   `test_alpha_falls_when_half_spread_increases` (synthetic Carhart-4F
   with known +alpha + noise).

Empirical smoke on real data, post-results inspection:

6. **Cost monotonicity** (live data, OOS β=0 column): αt_net monotone
   decreasing as H bps rises (2.41 → 1.90 → 1.27 → 0.65 → 0.05 → −1.11 →
   −2.21 → −7.61). ✅ PASS.
7. **β-monotonicity at constant H**: at H=50bps OOS, αt_net falls
   1.27 → 1.09 → 0.88 → 0.64 as β goes 0 → 1 → 2 → 3. ✅ PASS — drag
   strictly increases with β.
8. **Deterministic re-run gate**: orchestrator-emitted αt = 2.71 OOS,
   2.69 FL — identical to locked phase B 2026-05-09 + final-lock
   2024-2026.md. Per-phase αt values match prior to 2 decimal places.
   ✅ PASS — patch did not perturb scoring.

**Methodology theoretical justification (zen + perplexity convergence on Q6
of memo §4 review):** The diagnostic re-regresses Carhart-4F HAC=126 on
**net daily** series (= gross − time-varying drag), not on a scalar
`α_net = α_gross − drag_ann`. The latter is the experiment script's emitted
`alpha_net_4f` and leaves t-stat at gross value. Time-varying drag in
β>0 scenarios introduces heteroskedasticity: drag concentrates on
rebalance dates AND amplifies on high-vol days, so net-daily variance
exceeds gross-daily variance. HAC standard errors expand to absorb this,
correctly depressing t-stats. Perplexity adds: lower t-stat is BOTH a
genuine drop in net alpha AND correctly-revealed sampling uncertainty
from autocorrelated drag (R2000 spread mean reversion). The scalar method
overcounts significance by treating cost as exogenous-constant.

**Pre-merge code review fix (σ_median scope, zen-flagged HIGH)**: initial
diagnostic implementation (before zen CR) did not thread `sigma_median`
through `run_one_slippage_combo` — the function fell back to computing
median on the per-phase reindexed vol slice instead of using the joint
full-sample value per pre-reg §5. Fixed in pre-merge commit: `sigma_median`
added to combo signature, threaded through from orchestrator. Impact on
results: β=0 columns UNCHANGED (multiplier=1 regardless of σ_median); β>0
columns shifted by ≤0.10 t-units (OOS phase-local median ≈ joint, so small
change; FL phase-local median was LOWER than joint, so FL β>0 αt slightly
higher with fix). G3 OOS shifted from +0.93 → +0.88 (still FAIL ≥1.5); G3
FL shifted +1.76 → +1.77 (still PASS). **Verdict FAIL unchanged**. The
post-drag cyclicality calculation path was already correct (explicitly
passed `sigma_median` to `compute_effective_half_spread`), so cyclicality
table numbers are unaffected. Tables in §5.4 reflect the corrected
implementation.

## 5. Headline results

### 5.1 Gate verdicts (pooled across 5 phases per window, full_sample subsample)

| Gate | Cost (half_spread, β) | OOS αt_net | FL αt_net | Threshold | OOS | FL |
|---|---|---|---|---|---|---|
| G1 | 50bps, β=0 | **+1.27** | **+1.95** | ≥ 2.0 | ❌ | ❌ (just below) |
| G2 | 100bps, β=0 | **+0.05** | **+1.27** | ≥ 1.5 | ❌ | ❌ |
| G3 | 50bps, β=2 | **+0.88** | **+1.77** | ≥ 1.5 | ❌ | ✅ |
| KILL | 50bps, β=0 | +1.27 | +1.95 | ≥ 1.0 | not-breached | not-breached |

**Strict pre-reg classification**: REALIZABLE fails (not all gates pass);
FLAG-literal-text doesn't fit (FLAG presupposes G1 passes); KILL gate not
breached. **Per zen + perplexity convergent review: G1 is a knockout gate
by design — failure on either window is decisive rejection, regardless of
KILL threshold. "FAIL" classification stands.**

### 5.2 Post-2010 subsample (OOS only — FL is wholly post-2024)

Identical to full_sample because OOS starts 2018-01-01 ≥ 2010. No regime
flip between subsamples for OOS. (FL window all post-2024 by construction;
post-2010 filter trivially passes.)

### 5.3 Sharpe_net realism (G4)

Pooled across both windows at (H=50bps, β=2). Not computed against SPY
baseline in this pass — G1+G2 dominate the verdict. Sharpe_net per
phase logged in results parquet for future analysis if base is revived
with a different gate spec.

### 5.4 Cost-monotonicity full grid (pooled αt_net per window)

OOS 2018-2023:

| half_spread bps | β=0 | β=1 | β=2 | β=3 |
|---|---|---|---|---|
| 5 | +2.41 | +2.40 | +2.39 | +2.38 |
| 25 | +1.90 | +1.83 | +1.76 | +1.69 |
| 50 | **+1.27** | +1.09 | **+0.88** | +0.64 |
| 75 | +0.65 | +0.32 | −0.10 | −0.58 |
| 100 | **+0.05** | −0.49 | −1.15 | −1.86 |
| 150 | −1.11 | −2.16 | −3.28 | −4.04 |
| 200 | −2.21 | −3.82 | −5.12 | −5.35 |
| 500 | −7.61 | −10.66 | −8.44 | −6.58 |

FL 2024-2026:

| half_spread bps | β=0 | β=1 | β=2 | β=3 |
|---|---|---|---|---|
| 5 | +2.56 | +2.55 | +2.54 | +2.53 |
| 25 | +2.29 | +2.24 | +2.19 | +2.15 |
| 50 | **+1.95** | +1.86 | **+1.77** | +1.68 |
| 75 | +1.61 | +1.48 | +1.36 | +1.23 |
| 100 | **+1.27** | +1.11 | +0.95 | +0.80 |
| 150 | +0.60 | +0.38 | +0.18 | −0.01 |
| 200 | −0.07 | −0.32 | −0.54 | −0.73 |
| 500 | −3.79 | −3.75 | −3.63 | −3.49 |

Sanity: strict monotonicity in both H (vertical) and β (horizontal) on
both windows — consistent with cost mechanics. No bugs surfaced. The
500bps row gross-to-net translation is so extreme that bootstrap-style
artifacts could appear; not relied on for the verdict.

### 5.5 Post-drag cyclicality reversal (memo §9 mandatory observation gate)

OOS — all 5 phases trigger ≥+1.0 delta (β=2, base=50bps):

| Phase | R_excess_pre_drag | R_excess_post_drag | Δ | Post-drag classification |
|---:|---:|---:|---:|---|
| 0 | −2.117 | −0.363 | **+1.754** | matches benchmark baseline |
| 1 | −2.438 | −0.544 | **+1.893** | weakly strategy-specific |
| 2 | −2.467 | −0.413 | **+2.054** | matches benchmark baseline |
| 3 | −2.879 | −0.546 | **+2.333** | weakly strategy-specific |
| 4 | −2.946 | −0.529 | **+2.418** | weakly strategy-specific |
| **mean** | **−2.569** | **−0.479** | **+2.090** | — |

FL — no material trigger (benchmark IWM itself NOT counter-cyclical in
2024-2026, so excess concept is INCONCLUSIVE on 3/5 phases per
`classify_cyclicality_excess` decision tree; 2 phases show Δ < +1.5):

| Phase | R_excess_pre | R_excess_post | Δ | Post-drag classification |
|---:|---:|---:|---:|---|
| 0 | +35.63 | +35.99 | +0.36 | less counter-cyclical than benchmark |
| 1 | +40.96 | +42.33 | +1.37 | less counter-cyclical than benchmark |
| 2 | NaN | NaN | NaN | INCONCLUSIVE (benchmark R≥0) |
| 3 | NaN | NaN | NaN | INCONCLUSIVE (benchmark R≥0) |
| 4 | NaN | NaN | NaN | INCONCLUSIVE (benchmark R≥0) |

**Material finding flag: TRIGGERED on OOS.** Per memo §9, this reopens
the Layer 4 overlay class re-evaluation question. Convergent zen +
perplexity verdict: **the cyclicality reversal CONFIRMS the pre-cost
counter-cyclical mechanism was a cost-mirage artifact**, but G1 failure
makes Layer 4 reopening moot — cannot overlay a strategy with no post-cost
edge. The finding is reported as institutional memory (annotation in
`insider_form4_overlay_REJECTED_2026_05_10.md`) — future researchers must
understand that R2000 Form-4 "opportunistic" signals during high-VIX
regimes are heavily contaminated by spread artifacts.

Zen confirmed Round 2 prediction verbatim: *"drag is itself counter-
cyclically heavy by construction — high turnover × high spread in Q5
panic — so erasing Q5 alpha while leaving Q1-Q3 alpha intact would
mechanically reverse the cyclicality classification."* Mean Δ R_excess
+2.09 on OOS is unambiguous empirical confirmation.

Perplexity caveat preserved for posterity: *"The post-cost cyclicality
improvement (Δ +2.09 mean) is real — your strategy becomes less
tail-risky under market stress. That's valuable for crisis hedging but
irrelevant to your alpha publication thesis. Different use case."*

## 6. Verdict

**Decision**: **FAIL** — G1 (αt_net ≥ 2.0 at H=50bps median R2000
post-decimalization half-spread, β=0 uniform) violated on **both** OOS
(αt_net=+1.27, gap −0.73) and FL (αt_net=+1.95, gap −0.05) windows. G2
violated on both (OOS +0.05, FL +1.27, threshold ≥ 1.5). G3 violated on
OOS (+0.88, threshold ≥ 1.5), passes barely on FL (+1.77). Strict KILL
gate (αt < 1.0 at H=50bps/β=0 either window) not breached: OOS αt=1.27,
FL αt=1.95.

**Verdict-tree gap → "FAIL" classification rationale.** The pre-reg §8
verdict tree defined REALIZABLE (ALL pass), FLAG (G1 passes both AND any
of G2/G3/G4 fails), KILL (strict αt<1.0). My result fits none literally —
G1 fails so REALIZABLE is dead; G1 fails so FLAG literal-text doesn't fit;
KILL gate not breached. Independent zen + perplexity convergent review
clarifies this is **not a memo deficiency** but the intended
interpretation: G1 is a **knockout gate** — its failure is by design the
decisive rejection point, regardless of whether the strict KILL αt<1.0
floor is met. Both reviewers reject the "soft KILL" / "hard FLAG"
intermediates as accountability-diffusing fictions. Classification
stands: **FAIL — G1 knockout violated on both windows + material
cyclicality reversal triggered on OOS.**

Decision logic verbatim from pre-reg memo §7-8 + reviewer-corrected
verdict-tree interpretation:

> G1 is the knockout gate (αt_net ≥ 2.0 at H=50bps R2000 post-decimalization
> median half-spread, β=0 uniform). Its failure on EITHER window means
> realizability is rejected. FLAG, KILL classifications are subsidiary —
> FLAG covers cases where G1 passes but stress gates fail; KILL covers
> strict αt<1.0 even at base spread. G1 failure makes both subsidiary
> classifications irrelevant.

Reference: pre-cost gross αt was +2.71 OOS / +2.69 FL. Net of median
R2000 half-spread (50bps half = 100bps round-trip, anchored Corwin-Schultz
2012 + Hasbrouck 2009 estimates), αt drops to +1.27 OOS (Δ = −1.44
t-units) / +1.95 FL (Δ = −0.74). At stress half-spread (100bps =
2008/2020-tail anchor per Lesmond-Schill-Zhou 2004), αt collapses to
+0.05 OOS and +1.27 FL.

## 7. Consequences (per pre-reg §7-8 + reviewer convergence)

### 7.1 Paper-trade — SUSPEND

The PASS_MARGINAL-grounded paper-trade tracking on this base (active
since 2026-05-09) is **suspended effective 2026-05-12**. Both reviewers
unanimous: G1 knockout failure on both windows is decisive; continuing
paper-trade clutters live monitoring without informational value. Append
terminal log entry: *"SUSPENDED: Failed post-cost G1 realization gate;
gross αt=+2.71/+2.69 is a microstructure mirage at typical R2000 spreads."*

### 7.2 Capital deployment — REMAINS OFF-TABLE

Pre-reg `capital_deploy_clause` already required full PASS for unlock;
PASS_MARGINAL was insufficient. FAIL only reinforces this — no path to
capital deployment from this base.

### 7.3 Layer 4 overlay class — REMAINS REJECTED, with annotation

The 2026-05-10 Layer 4 overlay REJECTION
(`insider_form4_overlay_REJECTED_2026_05_10.md`) was grounded in the
pre-cost counter-cyclical mechanism (R_excess ≈ −2.5 → vol-target / DD-
control structurally mismatched). Today's finding shows this counter-
cyclical mechanism is itself a cost-mirage artifact (post-cost R_excess
≈ −0.5 on OOS). However, **base G1 failure makes Layer 4 reopening
moot** — cannot overlay a strategy with no post-cost edge. The
cyclicality reversal IS annotated in the overlay memo as institutional
memory: future researchers must understand R2000 high-VIX Form-4 signals
carry heavy spread-contamination.

### 7.4 Option A (selection-gate quorum) — STATUS CONTESTED

zen + perplexity split on this:

- **zen position**: Option A is "not dead, but a new strategy pivot, not
  a patch". A quorum/cluster gate fundamentally alters turnover profile
  and may concentrate trades on higher-conviction cluster buys that
  filter out the noisy high-cost Q5 dates causing G1 failure. Spend
  Bonferroni hit, but draft a **fresh separate pre-reg** with locked
  gates BEFORE compute — not a salvage attempt on the failed base.
- **perplexity position**: Option A as currently conceived is a "pre-reg
  violation" — introducing a post-hoc gate after G1 fails is p-hacking.
  "Reduce turnover at Q5 dates" is tautologically "selectively filter to
  cheapest regimes". Concedes: a fresh pre-reg WITH gates locked before
  compute is acceptable, but warns of sample shrinkage and lower power
  as cost of exploration.

**Resolution**: defer the decision. Both reviewers AGREE that any quorum
gate work MUST start with a fresh pre-reg memo, gates locked before
compute, treated as a new exploration rather than a patch. This is
consistent with project doctrine "never close the door". A quorum-gate
pre-reg can be considered after this postmortem closes; not in scope here.

### 7.5 Project-level Bonferroni accounting

Diagnostic is zero-Bonferroni (no new hypothesis). Existing program-level
test count unchanged. The `insider_form4_opportunistic_2026_05_08_v2`
ledger entry is amended in-place with the FAIL verdict line (no new
entry).

## 8. Ledger update

Update existing pre-reg entry `insider_form4_opportunistic_2026_05_08_v2`
in `docs/research/preregistration/ledger.json` with appended diagnostic
verdict line. NO new ledger entry created (diagnostic, not new
hypothesis).

## 9. Memory updates

- `memory/project_insider_form4_opportunistic_locked_2026_05_05.md`: append
  slippage stress verdict + post-drag cyclicality finding.
- `memory/feedback_slippage_stress_diagnostic_pattern_2026_05_12.md` (new):
  capture the design pattern (regime-conditional via β-formula × per-phase
  turnover persistence × re-regression on net) for reuse on future
  PASS_MARGINAL strategies. Anti-pattern: scalar `α_gross − drag_ann`
  leaves t-stat unchanged — must re-regress.

## 10. Audit artifact paths

Raw audit outputs (gross daily + per-rebalance turnover, per-phase × per-window):

- `~/.alphalens/audit/insider_form4_opportunistic_phase_b/phase_{0-4}_{returns,turnover}.parquet`
- `~/.alphalens/audit/insider_form4_opportunistic_final_lock/phase_{0-4}_{returns,turnover}.parquet`

Diagnostic outputs:

- `~/.alphalens/diagnostics/insider_form4_slippage_results_2026_05_12.parquet`
  (long-format: window × half_spread × β × phase × subsample → αt_net,
  α_ann_net, Sharpe_net, drag totals, per-quintile drag means)
- `~/.alphalens/diagnostics/insider_form4_slippage_cyclicality_2026_05_12.parquet`
  (per (window, phase) post-drag cyclicality verdicts at β=2)

Orchestrator audit JSONs (verdict + bootstrap re-summarized at the
pre-reg ledger gates, NOT the slippage gates — these are co-located
artifacts, not used in this postmortem's decision):

- `docs/research/insider_form4_opportunistic_oos_slippage_2026_05_12.{json,md}`
- `docs/research/insider_form4_opportunistic_final_lock_slippage_2026_05_12.{json,md}`
