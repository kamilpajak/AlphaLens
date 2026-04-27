# Kill Verdict Checklist — Cross-Layer Validation Standard

**Author:** Solo retail quant
**Last updated:** 2026-04-27
**Status:** Living standard; refine as new layers are closed
**Companions:**
- `docs/research/go_no_go_screen_template.md` — pre-pilot workflow (Phase 1-5, kill thresholds)
- `docs/research/5_paradigm_failures_postmortem.md` — per-layer narrative of what failed
- `docs/research/active_alpha_anti_patterns.md` — AP-1..14 vulnerability catalog

## Purpose

The audit of paradigms 2b/2c/2d/2e/2f/2g exposed a problem: **flat `CLOSED` status hides drastically different epistemic confidence**. Layer 2b/2d went through a 7-gate pipeline (Carhart-4F + HAC, walk-forward, Bonferroni, cost, bootstrap, survivorship, sanity). Layer 2c was archived on IS-only FF3 t=0.14. Layer 2f was killed on a 1-hour winsorized CAR observation with no Carhart, no OOS, no multiple-testing.

A future reader cannot tell from `__status__ = "CLOSED"` alone whether the kill was rigorous or thin.

This document codifies the **7-gate evidence checklist** that any future layer must complete to earn a `CLOSED` / `ARCHIVED` status with confidence. It also describes the `__closed_evidence__` constant (enforced by `tests/test_layer_status.py`) that pins the audit trail to source.

## The 7 gates

Each gate is named, has a reusable implementation in this repo, and a documented acceptance threshold.

### 1. `carhart_4f_hac` — Carhart-4F regression with Newey-West HAC

- **Measures:** Whether residual α survives once market, size, value, and momentum factors are stripped out.
- **Implementation:** `alphalens/backtest/factor_analysis.py::run_carhart_attribution` returns `[CAPM, FF3, Carhart-4F]` `AlphaResult`s with HAC-adjusted t-stats. Lag = `int(4·(n/100)^(2/9))`.
- **Acceptance:** OOS `alpha_tstat > 2.0` and `alpha_annualized > 0` (one-tailed positive). Both IS and OOS must be reported; degradation IS → OOS is a strong signal.
- **Anti-pattern:** AP-1 (overfit alpha) — a layer whose IS t-stat collapses by ≥3× OOS belongs in this column.

### 2. `sanity_checks_4gate` — `alphalens.rotation.sanity_checks` 4-gate

- **Measures:** Whether a strategy with a passive overlay actually adds value vs holding the passive benchmark unmodified.
- **Implementation:** `alphalens/rotation/sanity_checks.py` exposes four gates:
  - `check_passive_correlation` — kills if strategy↔passive correlation ≥ 0.95
  - `check_rolling_sharpe_stability` — kills if worst 252d rolling Sharpe < 0.4
  - `check_per_regime_vs_passive` — kills if strategy underperforms passive in ≥2 of 3 regimes (bull/bear/flat)
  - `check_overlay_alpha` — regresses strategy ~ passive + const; kills if α < 20 bps OR |t| < 1.0 OR α negative
- **Acceptance:** All four gates pass.
- **Where applicable:** Rotation/overlay strategies. **N/A** for pure ranking screeners (momentum, lean, insider, alt-data) and event-driven strategies (8-K, LLM-researcher) — they have no passive benchmark to overlay against. Mark `"N/A: <reason>"` rather than skipping silently.

### 3. `walk_forward_oos` — Walk-forward 252-day windows, C1-C5 gates

- **Measures:** Performance stability across rolling OOS windows; detects regime-specific gaming and momentum-crash exposure.
- **Implementation:** `alphalens/backtest/walk_forward.py::run_walk_forward` builds 252-day test windows stepped 21 days, computes per-window metrics, and evaluates 5 decision gates:
  - C1 Sharpe breadth: ≥ 70% of windows with Sharpe > 0.5
  - C2 Carhart α breadth: ≥ 50% of windows with α t > 1.5 HAC
  - C3 Block-return autocorr lag-1 < 0.5 (catches path-dependent gaming)
  - C4 Dark half: longest contiguous Sharpe<0 stretch < 12 windows
  - C5 Turnover ceiling: max per-window turnover < 100%
- **Acceptance:** Verdict `PASS` (all 5 gates passing) or at minimum `BORDERLINE` with a documented justification.

### 4. `multiple_testing_correction` — Bonferroni / FDR-BH

- **Measures:** Whether the headline α survives correction for the number of hypotheses tested.
- **Implementation:** `alphalens/backtest/multiple_testing.py::apply_bonferroni` (decision gate) and `fdr_adjusted_pvalues` (Benjamini-Hochberg, diagnostic).
- **Acceptance:** Pre-committed `n_tests` count, two-tailed |t| > critical at α=0.05/n. Document `n_tests` rationale (which hypotheses are decision-critical, which are robustness checks that don't inflate the denominator — Harvey-Liu-Zhu 2016, Bailey-López de Prado).

### 5. `cost_drag` — Realistic per-trade cost simulation

- **Measures:** What fraction of gross α survives realistic execution costs.
- **Implementation:** `alphalens/backtest/cost_model.py::cost_sensitivity_table` reports Sharpe across gross / 75 bps / 100 bps / 150 bps annual drag profiles, scaled by realized turnover. For micro-cap or high-turnover strategies use `RealisticCostModel` with Almgren-Chriss impact (`k × sqrt(size/adv) × annual_vol × sqrt(horizon/252)`).
- **Acceptance:** Net Sharpe (after moderate cost profile, turnover-scaled) remains > 0.5 and net α retains a meaningful fraction of gross α (rule of thumb: drag ratio < 50%).

### 6. `bootstrap_ci` — Moving-block bootstrap on headline statistic

- **Measures:** Sampling uncertainty of mean α / mean return without parametric distributional assumptions.
- **Implementation:** Moving-block bootstrap, block size ≈ n^(1/3) (typically 21 = ~1 trading month for daily series). ≥10k iterations. Pattern in `alphalens/rotation/gates.py::gate_bootstrap_ci`; for non-rotation use a standalone helper that operates on the daily return series.
- **Acceptance:** 95% CI lower bound > 0 (excludes zero).

### 7. `survivorship_pit` — Cohort split + delisting selection bias

- **Measures:** Whether outperformance is driven by post-IPO cohort (backfit-to-hype) or by selecting names that subsequently delist (look-ahead).
- **Implementation:** `alphalens/backtest/survivorship_pit.py`:
  - C1 cohort split: `split_universe_by_ipo_cohort` + `run_cohort_backtests` — compares pre-existing vs post-IPO cohorts
  - C2 delisting bias: `compute_selection_bias` — Fisher exact on (picks delisting in window) vs (universe rate)
  - C3 wipeout audit: `audit_mid_holding_wipeout` — replay with mid-holding delistings as −100%
- **Acceptance:** No cohort dominates outperformance (≤20% Sharpe gap); Fisher p > 0.05 with lift ≤ 1.0; wipeout audit Sharpe delta ≤ 10%.
- **N/A:** ETF-only strategies (no survivorship risk) and live alt-data feeds with no historical backfill.

## `__closed_evidence__` schema

Every `CLOSED` / `ARCHIVED` layer must publish `__closed_evidence__` in its package `__init__.py`:

```python
__closed_evidence__: dict[str, str] = {
    "carhart_4f_hac": "docs/backtest/<layer>_oos.md",
    "sanity_checks_4gate": "N/A: rule-based screener, no passive overlay",
    "walk_forward_oos": "docs/backtest/<layer>_walkforward.md",
    "multiple_testing_correction": "docs/research/<layer>_validation_final.md",
    "cost_drag": "docs/backtest/<layer>_oos.md",
    "bootstrap_ci": "docs/backtest/<layer>_oos.md",
    "survivorship_pit": "UNTESTED: paradigm-level kill, re-val cost > value",
}
```

Each value is one of three forms:

1. **Path** ending in `.md`, resolving under repo root. Indicates the gate was run and documented.
2. **`"N/A: <reason>"`** — gate doesn't apply to this strategy class (e.g., `sanity_checks_4gate` on a momentum screener with no passive overlay). Justification is mandatory.
3. **`"UNTESTED: <reason>"`** — gate applies in principle but was consciously not run (paradigm-level kill, cost > value, infrastructure not built). Justification is mandatory.

Required keys (frozenset, single source of truth in `tests/test_layer_status.py::REQUIRED_EVIDENCE_KEYS`):

```
{carhart_4f_hac, sanity_checks_4gate, walk_forward_oos,
 multiple_testing_correction, cost_drag, bootstrap_ci, survivorship_pit}
```

The test `test_closed_layers_have_evidence` enforces:
- All 7 keys present, no extras
- Every value is non-empty string
- Path values exist on disk
- N/A / UNTESTED values carry a non-empty justification after the prefix

## Worked retrospective: rigorous vs minimal

### Layer 2b (themed momentum) — 7/7 documented

```python
{
    "carhart_4f_hac":              "docs/research/multiple_testing_audit_2026-04.md",
    "sanity_checks_4gate":         "N/A: momentum screen, not rotation overlay",
    "walk_forward_oos":            "docs/research/walk_forward_oos_validation.md",
    "multiple_testing_correction": "docs/research/multiple_testing_audit_2026-04.md",
    "cost_drag":                   "docs/backtest/cost_validation.md",
    "bootstrap_ci":                "docs/research/layer2b_audit_final.md",
    "survivorship_pit":            "docs/research/pit_universe_backtest.md",
}
```

6 paths + 1 justified N/A (momentum strategies have no passive overlay). Verdict: high-confidence kill.

### Layer 2f (8-K event screen) — 1 N/A + 6 UNTESTED

```python
{
    "carhart_4f_hac":              "UNTESTED: paradigm-level kill (event microstructure crowding); re-val needs CAR infra (~2-4 weeks)",
    "sanity_checks_4gate":         "N/A: event-driven, not rotation overlay",
    "walk_forward_oos":            "UNTESTED: paradigm-level kill; OOS not run",
    "multiple_testing_correction": "UNTESTED: exploratory CAR by Item type, no formal Bonferroni",
    "cost_drag":                   "UNTESTED: event screen, no execution model built",
    "bootstrap_ci":                "UNTESTED: winsorized mean only, no CI",
    "survivorship_pit":            "N/A: S&P 500 universe, delisted treatment implicit",
}
```

A reader at-a-glance sees: *thin evidence, paradigm-level conviction, not 7-gate verified*. The kill stands (event microstructure crowding is well-documented, AP-9), but the audit trail is honest about the rigor gap.

A `git grep "UNTESTED:" alphalens/*/__init__.py` produces a punch list of layers where re-validation could in principle change the verdict — useful prioritization material if the project ever pivots back to active alpha.

## Future closures: integration with go/no-go

When closing a future layer, the workflow is:

1. Run pilot per `go_no_go_screen_template.md` Phase 1-5.
2. If the verdict is KILL, populate `__closed_evidence__` mapping each gate to the artifact produced during pilot. Gates not run in pilot → `"UNTESTED: <reason>"`.
3. Add layer to `LAYERS_WITH_STATUS` in `tests/test_layer_status.py`.
4. Test enforces structure on next CI run.

For layers whose paradigm has already been falsified at the conceptual level (e.g., a future LLM-conviction variant after AP-14 is established), it is acceptable to declare most gates `"UNTESTED: <reason>"` provided the kill rationale is documented in `__closed_reason__` and a postmortem entry. The checklist's job is honesty, not gate-stuffing.
