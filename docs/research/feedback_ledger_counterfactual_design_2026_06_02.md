# Feedback ledger — counterfactual design (avoiding self-confirmation)

**Status: DRAFT** (forward design for feedback v3; not yet implemented). Distils a
Perplexity Sonar-Deep-Research consultation (2026-06-02, ~50 sources) grounded in
off-policy evaluation, recommender-system degenerate-feedback-loop, model-autophagy,
and factor-zoo overfitting literature, mapped onto the AlphaLens thematic ledger.

Companion: `alphalens_ideal_shape_2026_05_29.md` §4/§8 Track A; epic #301; issue #165.

## RESOLVED 2026-06-02 — premise correction + Variant A (conditional-support OPE)

**Premise correction.** The original draft below assumed the scorer ranks a large
universe and takes the top-N, so it proposed ε-exploration over a "low-ranked tail"
sampled with "small π". That framing is **wrong**. The pipeline is a
**generate-and-verify funnel**, not a rank-and-take-top-N. For each theme the LLM
proposes 5-15 candidate beneficiaries → an mcap-bracket filter drops out-of-range names
→ three independent verification gates run (`tenk` / `press` / `insider`; the
designed ETF/NPORT gate is **not wired**) → a per-theme diversity cap
(`_MAX_CANDIDATES_PER_THEME = 3`) trims the survivors → the rest are surfaced. A name
that never surfaced was therefore either **never proposed by the LLM** (its selection
probability is structurally **exactly 0** — a hard positivity violation, NOT a small-π
tail) or it **hard-failed a gate**. There is no low-ranked tail with tiny-but-positive π
to sample from.

**Resolution = Variant A (conditional-support OPE).** Two independent expert tracks
converged on this — a Perplexity literature pass (cascade / examination-click-ratio
models; Sachdeva's "restrict the policy space" result) and a zen `deepseek-v4-pro`
review. The never-proposed region is **structurally decision-irrelevant** to any
gate-weight estimand: re-weighting the gates only ever changes decisions *within* the
proposed-and-gated set, so restricting the estimand to the LLM-proposal support is the
**correct** estimand, not a compromise. Variant B — building a real cross-sectional
universe score and asking "should the generator have proposed differently" — is a
separate proposal-stage track and is **out of scope** here.

**Estimand.** `V(π_w) = E[outcome | LLM-proposed AND in mcap-bracket AND reached verify,
under gate/score weighting π_w]`. Conditioning on "LLM-proposed" is a **pre-treatment
inclusion criterion** (it cancels in any policy-vs-policy contrast), not a collider — so
the conditioning introduces no selection bias into a policy comparison.

**ε-injection point.** The per-theme diversity cap in `orchestrator.py` (~lines 367-369).
Lift the *verify* cap so rank-4+ in-bracket names also get gate-checked, producing a pool
of **verified-but-cap-excluded** names. An ε-sampler must then **own the
surface-vs-cap decision** and **log a selection propensity π for every verified name**,
**replacing** the current deterministic confidence-descending + ticker tie-break (ticker
correlates with sector / exchange, which correlate with outcome — so keeping a
ticker-ordered deterministic cut leaves an unlogged, non-ignorable selection step).
ε = 0.10, stratified by theme × score-tier; cap ≤ 2 per theme, ≤ 5 extra names surfaced
per day.

**Estimator.** Doubly-robust / IPW over the **conditional support** — NOT raw IPS. The
1-5 integer score is coarse and tie-heavy, which gives high IPS variance; the continuous
tiebreak fields (`catalyst_strength`, `insider_score_usd`) give a near-continuous
within-theme propensity that the DR / IPW estimator can use.

**Claim boundary.**
- **Allowed:** "among names the LLM proposed and that reached the gates, changing the gate
  weighting from g to g′ shifts `E[return]` by θ̂ (CI …), **conditional on the
  LLM-proposed pool**."
- **Forbidden:** "we should have surfaced [some never-proposed name]"; any full-universe
  claim; any hidden point estimate over not-yet-explored cells. For unexplored cells,
  report **coverage %** plus **partial-identification bounds**, never a point estimate.

**Honest limits.** This design **cannot** evaluate the generator / proposal step at all;
a positivity hole remains until the ε-arm accrues data; model autophagy is therefore only
**partially** broken until that ε-arm has enough observations.

**Roadmap impact.** The design spike is done — PR2 / PR4 / PR6 / PR7 are unblocked and
**no PR is killed**. PR1b must persist gate verdicts and score components for **all
verified candidates**, not just the surfaced ones, so the cap-excluded pool is
recoverable for OPE.

---

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
