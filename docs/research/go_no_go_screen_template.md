# Go/No-Go Screen Template — Retail Quant Idea Evaluation

**Author:** Solo retail quant
**Last updated:** 2026-04-25
**Status:** Living template; refine after each new pilot
**Companions:**
- `docs/research/active_alpha_anti_patterns.md` — vulnerability checklist (AP-1..14)
- `docs/research/paradigm_failures_postmortem.md` — narrative of what failed and why
- `docs/research/kill_verdict_checklist.md` — 7-gate evidence template for KILL verdicts

## Purpose

Before committing infrastructure work to a new strategy idea, run a minimal pilot that can produce a KILL verdict in ≤1 day at near-zero cost. The goal is **discipline over discovery** — kill bad ideas fast, with pre-committed thresholds that prevent retroactive rationalization.

This template formalizes the workflow that emerged from Layer 2f (8-K screen — KILL in 1 day) and Layer 2g (GuruAgent pilot v2 — 4-year regime test before infrastructure). Apply it to any new idea before writing scorers, backtest harness, monitoring, or production data pipelines.

## When to use this template

Use go/no-go screen when:
- A new active alpha idea appears (academic paper, market observation, novel data source)
- Re-evaluating a previously-killed strategy after a trigger condition (see postmortem §"Conditions for project re-activation")
- Considering a strategy variant that materially deviates from a previously-validated baseline

Skip the screen only if:
- The strategy has already passed a screen and is in maintenance mode
- The work is pure infrastructure improvement on an already-validated strategy
- Pure research/educational exploration with no capital-deployment intent (label clearly: RESEARCH_ONLY)

## The five-phase workflow

### Phase 1 — Hypothesis specification (≤30 min)

Write down, in order:

1. **Claim:** "Strategy X earns Y bps/year over benchmark Z, after costs, OOS." Specific numbers, no hand-waving.
2. **Mechanism:** Why does the alpha exist? What inefficiency is being captured? Who is on the other side of the trade?
3. **Why retail can capture it:** Latency? Holding period? Tax? Niche too small for institutional? If you can't answer this, the signal is probably crowded (AP-9).
4. **Sample size needed:** How many independent observations to detect the claimed effect? Power analysis at α=0.05 with claimed effect size.
5. **Anti-pattern vulnerability scan:** Walk through `active_alpha_anti_patterns.md` AP-1 through AP-14. For each, mark vulnerable / not vulnerable / unclear. Address vulnerabilities in design before pilot.

### Phase 2 — Pre-commit kill thresholds (≤30 min)

Write a YAML file with kill thresholds. Hash it. Capture git SHA. **Do not edit after pilot starts.**

Reference YAML schema (see §"YAML schema" below for full template):

```yaml
strategy_id: my_new_idea_v1
hypothesis: "Strategy X earns ≥200 bps/year vs SPY, OOS"
kill_thresholds:
  primary:
    metric: oos_carhart_alpha_t
    operator: ge
    value: 1.5
  joint:
    - metric: passive_correlation
      operator: lt
      value: 0.95
    - metric: min_year_underperformance_pp
      operator: ge
      value: -5.0
  ...
```

Use `alphalens.rotation.precommit.ConfigFingerprint` to capture:
- `config_path`
- `content_sha256` of the YAML
- `git_sha` (must be clean repo)

This becomes the immutable record of what you committed to before seeing results.

### Phase 3 — Minimal pilot (≤1 day for go/no-go, ≤1 week for full regime test)

**Default pilot design (adjust per strategy type):**

| Parameter | Default | Rationale |
|---|---|---|
| Universe size | 30-150 tickers | Enough for statistical power; small enough for fast iteration |
| Regime years | 4 distinct years | Cover bull/bear/flat/concentrated-growth |
| Holding period | Match strategy claim | 1-day, 60-day, 1-year — whatever the hypothesis specifies |
| Selection size | Top-N where N/universe ≤ 0.10 | Avoid AP-3 concentration overfit |
| Cost model | Realistic per-ticker (spread × turnover × frequency) | Avoid AP-7 |
| Universe construction | PIT-correct, includes delisted | Avoid AP-8, AP-13 |

**Specific regime years to consider** (US equity, current era):
- 2018: quiet/down (Q4 sell-off)
- 2020: COVID growth bull
- 2022: bear (Fed tightening)
- 2024: AI growth bull (mega-cap concentration)

If the strategy claims regime-independence, all four years must pass. If it claims defensive (bear-only), at minimum 2018+2022 must pass and 2020+2024 must not catastrophically underperform.

### Phase 4 — Required output metrics

Every pilot must produce, at minimum:

**Distributional:**
- Per-year return vs benchmark (decompose, don't pool)
- Per-year std dev
- Median + winsorized mean (5/95th) + raw mean (AP-4)
- Per-regime decomposition (bull/bear/flat)

**Statistical:**
- Carhart-4F α with HAC standard errors → t-stat
- Bootstrap 95% CI on Sharpe (autocorr-adjusted, Lo 2002)
- Random-scorer null hypothesis test (does your scorer beat shuffle?)

**Sanity (4-gate framework):**
- `passive_correlation` to relevant benchmark (must be <0.95) — `alphalens/rotation/sanity_checks.py::passive_correlation`
- `rolling_sharpe_stability` — min 252-day Sharpe across windows (must be >0.30)
- `per_regime_vs_passive` — per-bucket marginal vs passive
- `overlay_alpha` (for tilt strategies) — α net of passive exposure

**Discipline:**
- True `n_tests` count from precommit log (every config-changing commit on this strategy branch)
- FDR-corrected p-value (Benjamini-Hochberg) given that `n`

### Phase 5 — Verdict and documentation

**Decision rules** (apply in order, fail any → KILL):

1. Did the run match the pre-committed config? (Hash + git SHA verification.) If not, the run is invalid — re-run from clean state.
2. Did any kill threshold fire? If yes → KILL.
3. Did any sanity gate fail? If yes → KILL (don't iterate the design without resetting `n_tests`).
4. Does the FDR-corrected p-value clear 5%? If not → KILL.
5. Does the worst-year underperformance clear the tolerance? If not → KILL.

**On KILL verdict — accept it.**
- Don't iterate the strategy to "fix" the failure without explicit acknowledgment that `n_tests` budget grew.
- Document failure modes in postmortem, append new patterns to `active_alpha_anti_patterns.md` if the failure mode is novel.

**On GO verdict — still cautious.**
- Pilot passed ≠ full strategy will work. Build minimal infrastructure for true OOS validation on independent window.
- Re-screen anti-patterns at full-OOS scale.
- Pre-commit capital-deployment thresholds (separate from research thresholds).

**On KILL verdict — evidence checklist:**
See `docs/research/kill_verdict_checklist.md` for the 7-gate evidence template. Future `__status__ = "CLOSED"` declarations require `__closed_evidence__: dict[str, str]` mapping all 7 gates to a documentation path, `"N/A: <reason>"` (gate doesn't apply), or `"UNTESTED: <reason>"` (gate applies but consciously not run).

## YAML schema (full template)

```yaml
# go_no_go/<strategy_id>.yaml
# Pre-committed BEFORE pilot. Do not edit after run starts.

strategy_id: my_new_idea_v1
created_date: 2026-04-25
author: <your_handle>
hypothesis: |
  Strategy X earns ≥200 bps/year vs SPY, OOS, after realistic per-ticker costs,
  on a PIT-correct universe of <description>, over 2018/2020/2022/2024 regimes.

mechanism: |
  <Why does the alpha exist? What inefficiency? Who is on the other side?>

retail_advantage: |
  <Why retail can capture this where institutional cannot/will not?>

# Kill thresholds — ANY single failure → KILL
kill_thresholds:
  primary:
    name: oos_carhart_alpha_t
    operator: ge
    value: 1.5
    rationale: "OOS lower SNR than IS; expect 50-70% degradation per AP-1"

  joint:
    - name: passive_correlation
      operator: lt
      value: 0.95
      rationale: "AP-2 — R²~1 = signal dead"

    - name: min_year_underperformance_pp
      operator: ge
      value: -5.0
      rationale: "AP-6 — structural drag tolerance per Perplexity follow-up"

    - name: mean_excess_return_bps
      operator: ge
      value: 200
      rationale: "Net of realistic costs"

    - name: median_winsorized_excess_return_bps
      operator: ge
      value: 100
      rationale: "AP-4 — median complements mean on heavy-tailed signals"

    - name: rolling_sharpe_min_252d
      operator: ge
      value: 0.30
      rationale: "Stability gate"

    - name: fdr_corrected_p
      operator: lt
      value: 0.05
      rationale: "AP-5 — multiple-testing budget"

# Pilot design (locked)
pilot:
  universe_source: "Polygon S&P 500 random sample"
  universe_size: 100
  regime_years: [2018, 2020, 2022, 2024]
  holding_period_days: 60
  top_n: 10
  weighting: equal
  rebalance_frequency: "annual"  # for regime test
  cost_model:
    type: per_ticker_realistic
    spread_source: "Polygon NBBO snapshot"
    fallback_bps: 50
  universe_construction:
    pit_correct: true
    include_delisted: true

# Multiple-testing budget
n_tests_budget:
  declared_max: 10
  current_count: 1  # increments on every config-changing commit
  fdr_method: "BH"  # or "bonferroni"

# Anti-pattern vulnerability scan
anti_pattern_scan:
  AP-1_is_oos_degradation: vulnerable  # standard
  AP-2_r_squared: not_vulnerable        # explicit non-overlay design
  AP-3_concentration: not_vulnerable    # 100/10 = 10% (boundary)
  AP-4_outliers: vulnerable             # event-driven component
  AP-5_multiple_testing: vulnerable     # standard
  AP-6_value_in_growth_bull: not_applicable
  AP-7_cost_model: vulnerable           # daily rebalance variant
  AP-8_survivorship: not_vulnerable     # PIT universe
  AP-9_crowded_alt_data: unclear        # need to research
  AP-10_event_asymmetry: not_applicable
  AP-11_external_llm: not_applicable
  AP-12_premature_infrastructure: not_vulnerable  # this IS the screen
  AP-13_bankruptcy_sign_flip: not_vulnerable      # PIT + delisted
  AP-14_forecasting_ceiling: vulnerable           # any directional strategy
```

## Worked example A: Layer 2f 8-K screen (1-day KILL)

**Strategy:** Event-driven go/no-go on 8-K filings, predict CAR direction.

**Phase 1 hypothesis:** Specific 8-K Items have positive median CAR at +5d holding period, ≥50 bps after costs, robust across 2022-2024.

**Phase 2 thresholds:**
```yaml
kill_thresholds:
  joint:
    - name: median_winsorized_car_5d_bps
      operator: ge
      value: 50
    - name: t_stat_per_item
      operator: ge
      value: 2.0
```

**Phase 3 pilot:** 150 random S&P 500 tickers, 2022-2024, all 8-K Items, +1/+5/+20/+60d CAR.

**Phase 4 result:** All Items had winsorized mean CAR < 50 bps or negative. Items 1.01/5.02/8.01/9.01 had median CAR -100 to -250 bps. Item 5.03 raw mean +606 bps but driven by single M&A spike; std 5783 across n=36 → t-stat <2.

**Phase 5 verdict:** KILL on median + t-stat thresholds. Total time: 1 day script + 1 hour analysis. **Saved 2-3 weeks** of building event-driven trading infrastructure.

**Lesson encoded:** AP-10 (8-K event asymmetry).

## Worked example B: Layer 2g GuruAgent pilot v2 (4-year regime test)

**Strategy:** Buffett-style LLM screen (Gemini 3.1 Pro), top-10 conviction, 1-year hold.

**Phase 1 hypothesis:** LLM-encoded Buffett analysis earns ≥200 bps/year vs SPY OOS, 2018/2020/2022/2024.

**Phase 2 thresholds (relaxed gate per Perplexity follow-up):**
```yaml
kill_thresholds:
  joint:
    - name: mean_excess_bps
      operator: ge
      value: 200
    - name: min_year_underperformance_pp
      operator: ge
      value: -5.0
    - name: spy_correlation
      operator: lt
      value: 0.95
```

**Phase 3 pilot:** 30 random S&P 500 tickers/year × 4 years, Polygon-backed financials in prompt, equal-weight top-10 by conviction.

**Phase 4 result:**

| Year | Regime | Outperf |
|---|---|---|
| 2018 | quiet/down | +9.15pp |
| 2020 | COVID growth bull | -3.56pp |
| 2022 | bear | +3.11pp |
| 2024 | AI growth bull | -5.43pp |

Mean +82 bps, min-year -5.43%, correlation +0.97 vs SPY.

**Phase 5 verdict:** KILL on all 3 joint gates:
- mean +82 bps < 200 bps floor
- min-year -5.43% beyond -5pp tolerance
- correlation +0.97 > 0.95 threshold

**Lesson encoded:** AP-6 (value-style structural drag in growth-bull regimes), AP-14 (forecasting ceiling).

## Adaptations per strategy type

**Event-driven (8-K, earnings, M&A):**
- Decompose by event-type bucket; pooled means hide structural negativity (AP-10)
- Median + winsorized mean per bucket, not pooled
- Holding period must match the event's information half-life

**Factor / cross-sectional:**
- Standard Carhart-4F + FF5+UMD attribution
- Bootstrap CI on Sharpe (autocorr-adjusted)
- Random-scorer null in same backtest harness

**Overlay / tactical tilt:**
- AP-2 design-stage check: tilt magnitude / benchmark daily volatility ratio. If <0.10, kill before any backtest.
- 4-gate sanity from `alphalens/rotation/sanity_checks.py`

**LLM-as-scorer:**
- Multi-regime mandatory (2018/2020/2022/2024 minimum)
- Prompt fingerprinting via `alphalens/guru/prompt.py`
- Treat external LLM consultation about LLM-scorer as AP-11 violation — circular validation

**Alt-data:**
- AP-9 latency/crowding analysis BEFORE pilot. If signal is publicly disclosed at <10s institutional latency, retail will not capture it.
- Search literature for prior academic exploitation of the same signal.

## Code references

Pre-commit infrastructure (use these directly):
- `alphalens/rotation/precommit.py::ConfigFingerprint` — path + content SHA-256 + git SHA
- `alphalens/rotation/precommit.py::count_config_commits` — true `n_tests` from git log
- `alphalens/rotation/precommit.py::check_oos_discipline` — validate clean run vs IS baseline
- `alphalens/rotation/precommit.py::record_run` — append JSON line to audit log

Sanity check framework:
- `alphalens/rotation/sanity_checks.py::passive_correlation`
- `alphalens/rotation/sanity_checks.py::rolling_sharpe_stability`
- `alphalens/rotation/sanity_checks.py::per_regime_vs_passive`
- `alphalens/rotation/sanity_checks.py::overlay_alpha`

Statistical infrastructure:
- `alphalens/backtest/factor_analysis.py` — Carhart-4F, FF5+UMD, Q4 with Newey-West HAC
- `alphalens/backtest/multiple_testing.py` — Bonferroni + BH-FDR
- `alphalens/backtest/sharpe.py` — autocorr-adjusted Sharpe (Lo 2002)

Prompt/config fingerprinting:
- `alphalens/guru/prompt.py` — prompt SHA-256 for LLM-as-scorer

## How this template evolves

After each pilot run (KILL or GO):
- If a novel failure mode emerged → append AP-N entry to `active_alpha_anti_patterns.md`
- If a methodology refinement helped → update this template's relevant phase
- If the YAML schema needed a new field → add it here with rationale comment

Version this file in git like any other research artifact. The template *is* the methodology.
