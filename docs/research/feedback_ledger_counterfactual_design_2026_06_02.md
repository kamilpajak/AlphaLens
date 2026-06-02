# Feedback ledger — counterfactual design (avoiding self-confirmation)

**Status: DRAFT** (forward design for feedback v3; not yet implemented). Distils a
Perplexity Sonar-Deep-Research consultation (2026-06-02, ~50 sources) grounded in
off-policy evaluation, recommender-system degenerate-feedback-loop, model-autophagy,
and factor-zoo overfitting literature, mapped onto the AlphaLens thematic ledger.

Companion: `alphalens_ideal_shape_2026_05_29.md` §4/§8 Track A; epic #301; issue #165.

## 1. The hazard this design exists to prevent

The thematic scorer is **deterministic** and surfaces only its top-N (~5-15/day). The
paper harness shadow-trades every **surfaced** candidate (honest `shadow_return`,
incl. UNFILLED), so outcomes exist for the surfaced set — but there is **zero outcome
data on candidates the scorer ranked LOW**. Re-weighting the scorer on outcomes of its
own selections is a **degenerate feedback loop / model autophagy**: a self-fulfilling
loop that amplifies the scorer's existing weights and overfits tiny samples.

Formally it is the **fundamental problem of causal inference** + a broken **overlap**
condition: off-policy importance weights blow up to ∞ for never-surfaced names, so a
naive IPW/ablation on the surfaced set is impossible, not merely noisy.

**Key correction to a common assumption:** "we shadow-trade everything" only covers the
**surfaced** top-N. It is **necessary but NOT sufficient** for honest gate-ablation —
the counterfactual you need lives in the low-ranked tail you never trade.

## 2. The protocol (what to build, in order)

### 2.1 Controlled exploration — the load-bearing fix
Shadow-trade a **stratified random control sample of NON-top-N candidates**: ε ≈ 0.05,
stratified by **score decile**, with the **exact selection probability `π` logged per
row**. This is the only way to restore overlap and get counterfactual data on what the
scorer ranked low. Without it, §2.2 is mathematically undefined.
- Selection prob per stratum *i*: `p_i = (1-ε)·1[i∈top_N] + ε/N_strata`.
- Never let ε decay below 5% without explicit human approval + multi-regime stable
  validation (autophagy backstop).

### 2.2 Counterfactual estimation — never re-weight on raw returns
Estimate "what would different gate weights have done" via **doubly-robust off-policy
evaluation** (model term + inverse-propensity term), **regime-adjusted**, with logged
`π`. Bootstrap (or Bayesian posterior) for **confidence intervals** — decisions use the
interval, never a point estimate. Propensity + outcome models trained **out-of-sample**
(time-series CV, strictly-prior data only).

### 2.3 Statistical power gates — refuse to act on noise
- **MDE / power analysis first.** At n=10-15/day, vol ~20-30%, you can only detect
  ~1-2% absolute return differences; treat anything below MDE as statistically equal.
- **No weight change below ~100-150 effective obs (~4-6 weeks).** Per-regime
  conclusions need n≥50 **per VIX cell** (~40+ days); regime-split at n≈50 is below the
  overfitting threshold — keep it pooled until cells fill.
- **Multiple testing:** gates are **correlated** → use **Holm-Bonferroni / FDR**, NOT
  blind Bonferroni; correct **within** each regime stratum. Pre-register endpoints.
- **Walk-forward** (train → eval → validate, 2-4 week windows, regime-aware warm-up).
  If validation lags evaluation → overfitting → auto-raise ε.
- **Shrinkage:** hierarchical/empirical-Bayes, pull regime estimates toward the pool;
  cross-validate the shrinkage strength itself.

### 2.4 Human click = exogenous channel, kept ORTHOGONAL
The `interested`/`dismissed` click is the **only** signal for "does the human add edge
over the deterministic scorer" — but it must **never** feed the scorer's weights until a
**causal test rejects** H0="human adds no value".
- Evaluate human value via **IPW on the click propensity** (corrects the ~15% optional
  click selection) + **double ML / causal forest with cross-fitting** for heterogeneous
  effects; **correct for position bias** (higher-ranked cards get more clicks regardless
  of quality).
- Keep two structurally separate evaluation pathways: scorer-weights vs human-value.
  (AlphaLens PR-4 `execution_modes` already gates on **regime only** and never reads
  `action` — that orthogonality is correct; preserve it with an enforcement test.)

### 2.5 Automation boundary (= the project's augmentation doctrine, encoded)
- **Fully-auto:** observational telemetry + counterfactual **estimates with CIs** +
  degradation monitors (exploration entropy, gate-correlation drift, concept-drift →
  auto-raise ε).
- **Human-in-the-loop (system PROPOSES, human DISPOSES):** any real weight change, gate
  removal, or architecture change. Never auto-apply. This is 1:1 with "augmentation, not
  execution / human final decision / no black-box scoring".

## 3. Do NOT (cardinal sins)
1. Re-weight on **uncorrected** observed returns (the cardinal sin — causes the loop).
2. Gate-ablation with insufficient power (<4-6 weeks / below MDE).
3. Pool data across market regimes.
4. Blind Bonferroni on correlated gates.
5. Same data for counterfactual estimation **and** validation.
6. Feed the human click into scorer weights before a causal test.
7. Exploration ε below 5%.
8. Act on point estimates without uncertainty.
9. Treat the ledger as a short-term **performance optimiser** (it is an adaptability
   instrument).
10. Assume human feedback is inherently valuable, or that regimes are discrete/static.

## 4. What AlphaLens already has vs the gap
- ✅ Honest `shadow_return` anchor for the surfaced set (PR-3); empirical-Bayes shrinkage
  + pooled n≥50 gate + INERT-by-default (`execution_modes`, PR-4); pre-reg ledger +
  Bonferroni discipline (factor-research methodology bundle); orthogonal click (PR-4
  never reads `action`).
- ❌ **The gap:** no exploration / low-ranked control universe → selection bias is
  unsolved → honest gate-ablation / re-weighting is impossible today. This is the single
  highest-leverage, user-independent step before the ledger can re-weight (vs confirm)
  the scorer.
- ❌ No action-aware consumer for the operator-vs-scorer edge test (click is write-only).
- ❌ Brief metadata (`model_version`/`prompt_version`/gate verdicts) not on the ledger
  schema — must be joined post-hoc from the brief parquet.

## 5. Near-term honest framing
Deliverable in weeks (once #379 press-release ingest is fixed + metadata plumbed):
**execution-quality telemetry** (shadow vs realized entry realism, per-regime non-fill
rates, pooled PnL distribution) — real, click-independent. **NOT** a self-driving
re-weighting loop (statistically premature, self-confirmation hazard). Re-weighting waits
on §2.1 exploration + §2.3 power.

## References
Degenerate feedback loops (arxiv 1902.10730); model autophagy / "model collapse"
(arxiv 2403.07857); doubly-robust off-policy evaluation (arxiv 1511.03722); position
bias in implicit feedback (eugeneyan); double ML / orthogonal ML (cross-fitting);
walk-forward (alphascientist); regime robustness in backtesting (FactSet). Full source
list in the session transcript (Perplexity Sonar Deep Research, 2026-06-02).
