# Strategy validation playbook

**Crystallized 2026-04-29** after the tri-factor session that took 3 sequential synthesis docs to settle on a verdict (FAIL → "phase-aliasing reversal" → FAIL phase-robust). The fix: never produce a verdict from a single-phase point estimate. Every candidate goes through this pipeline.

This is the **canonical pipeline for any future strategy candidate** in AlphaLens. Each step has its own infrastructure and tests.

## The 7-step pipeline

```
0. Pre-register hypothesis          ───►  alphalens preregister add ...
1. Define scorer adapter            ───►  experiment_<name>.py
2. Smoke run                        ───►  6m IS + 6m OOS, single ADV/cost
3. Full IS + OOS run                ───►  domyslny --phase-offset 0
4. Multi-phase audit (KEY GATE)     ───►  scripts/audit_multi_phase.py
5. Read robust_verdict (PASS/MID/FAIL) — compare αt vs Bonferroni threshold
6. PASS → forward-walk + alphalens preregister complete --verdict PASS
   MID  → regime-conditional sizing + complete --verdict MID
   FAIL → close + complete --verdict FAIL, document anti-pattern
```

## Step 0 — Pre-register (BEFORE step 1)

Closes Gap #1. Pre-registration is the commitment device that prevents
post-hoc parameter selection from inflating the apparent t-stat.

Compose a JSON file `params.json`:

```json
{
  "params_frozen": {
    "top_n": 5, "holding": 20, "rebalance_stride": 5,
    "weights": {"factor_a": 0.5, "factor_b": 0.5}
  },
  "periods": {
    "is_start": "2015-01-01", "is_end": "2022-12-31",
    "oos_start": "2023-01-01", "oos_end": "2026-04-22"
  },
  "success_criteria": {
    "mode": "multi_phase",
    "min_alpha_t_pass": 1.5, "min_alpha_t_mid": 1.0
  }
}
```

Then register, and look up the Bonferroni-corrected threshold for the
signal class **as it stands now** — the freshly-added entry counts as
the n-th test:

```bash
.venv/bin/alphalens preregister add \
    --id <slug> --signal-class <class> \
    --hypothesis "..." --scorer-path scripts/experiment_<name>.py \
    --params-file params.json

.venv/bin/alphalens preregister threshold --signal-class <class>
# → "<class>: N tests at α=0.05 → critical |t| ≈ ..." where N includes
#   the entry you just added.
```

Ledger lives at `docs/research/preregistration/ledger.json` (git-tracked).
Re-running with different parameters requires a NEW id — the original
registration is frozen.

## Step 1 — Scorer adapter

New strategy = new file `scripts/experiment_<descriptive_name>.py`. Use the existing tri-factor or mom+lowvol script as template:

- Adapter signature: `def <name>_adapter(histories, config) -> pd.DataFrame` returning `ticker, score` columns.
- `MIN_BARS_REQUIRED` attribute on the adapter (engine reads this for warmup).
- Required argparse flags (built into all current scripts; copy-paste from tri-factor):
  - `--is-start / --is-end / --oos-start / --oos-end`
  - `--lock-universe`
  - `--phase-offset` (wired to `BacktestEngine(phase_offset=...)`)
  - `--rebalance-stride`, `--top-n`, `--holding`
  - `--adv-thresholds`, `--cost-half-spreads` (sweep params)

Wrap argparse in `_build_parser() -> ArgumentParser` so CLI tests can construct it without invoking `main()`.

## Step 2 — Smoke run (always first)

Quick end-to-end check before committing to any long backtest:

```bash
.venv/bin/python scripts/experiment_<name>.py \
    --is-start 2015-01-01 --is-end 2015-06-30 \
    --oos-start 2015-07-01 --oos-end 2015-12-31 \
    --adv-thresholds 5000000 --cost-half-spreads 5 \
    --out /tmp/smoke.md
```

Validates: scorer wires up, no exceptions, non-zero scored count, Sharpe is a finite number. Sharpe values in 6-month window are **noisy garbage**; do not interpret them.

## Step 3 — Full IS + OOS (single phase)

```bash
.venv/bin/python scripts/experiment_<name>.py \
    --is-start 2015-01-01 --is-end 2022-12-31 \
    --oos-start 2023-01-01 --oos-end 2026-04-22 \
    --adv-thresholds 5000000 20000000 \
    --cost-half-spreads 5 15 \
    --out docs/research/<name>_extended_is.md
```

This is the **point estimate** at phase 0. **Do not draw verdicts from this alone.** Use it for:

- Sanity check (does the strategy plausibly work?)
- Parameter sweep (which roe_w / ADV / cost-bp combo looks promising?)
- Sharpe / α / t-stat headline numbers for documentation

**Anti-pattern (today's session lesson):** producing FAIL/PASS verdicts from this step. Single-phase Sharpe in our harness has 30-77pp/y dispersion across phases — verdict noise.

## Step 4 — Multi-phase audit (THE GATE)

```bash
.venv/bin/python scripts/audit_multi_phase.py <name> \
    --is-start 2015-01-01 --is-end 2022-12-31 \
    --oos-start 2023-01-01 --oos-end 2026-04-22 \
    --adv-thresholds 5000000 \
    --cost-half-spreads 5 \
    --lock-universe \
    --rebalance-stride 5 \
    --out docs/research/<name>_multi_phase_audit.json
```

Notes:

- **`--lock-universe` mandatory** for halves stability checks. Otherwise per-period universes diverge.
- Strip the parameter sweep down to a **single representative config** for the audit. Phase × roe_w × ADV × cost is too many combinations; pick the best from step 3 and audit just that. Multi-phase amplifies compute 5×, do not also amplify across configs.
- Background it (`run_in_background=True`) — 25-60 min for typical 8y IS.
- Driver script's `_SCRIPTS` mapping (in `scripts/audit_multi_phase.py`) needs your strategy name added.

## Step 4.5 — Optional: Risk overlay test (Layer 4)

Only after Step 4 produces a phase-robust positive base or a base whose only failure mode is excessive drawdown / dispersion.

If the multi-phase audit shows the screener has any phase-robust merit but unsatisfactory risk profile (e.g. dispersion > 30pp, deep drawdowns, Sharpe net < 0.5), test a Layer-4 sizing overlay before declaring final FAIL:

- `alphalens/overlays/vol_target.py` — Moreira-Muir 2017 vol-targeting (`VolTargeter`, `apply_vol_target`).
- Wrap base experiment via `scripts/experiment_vol_target_overlay.py` (or analogous wrapper).
- **Critical:** dynamic per-rebalance cost is required (`turnover_t = base_turnover · scale_t + |scale_t − scale_{t-1}|`). Constant-drag accounting inflates reported alpha.
- Pre-register in a fresh signal class (e.g. `risk_management_overlay_<date>`) — avoids Bonferroni inflation in the screener's own class.
- **Primary success metric: Sharpe-improvement vs ungated BASE**, NOT Carhart α t-stat. Vol-scaling makes betas time-varying and OLS attribution distorts α; Sharpe is robust. See ADR 0007 for the time-varying-beta limitation.

Verdict-feed back into Step 5: the robust verdict for a Layer-4-augmented strategy is computed on *scaled* returns vs *unscaled* base, not vs benchmark in isolation.

## Step 5 — Robust verdict

`scripts/audit_multi_phase.py` writes JSON + prints a verdict per config to stderr. Or compute manually from the JSON:

```python
from alphalens.backtest.multi_phase import robust_verdict
verdict = robust_verdict(per_phase_rows)
```

`robust_verdict()` rules (`alphalens/backtest/multi_phase.py`):

| Verdict | Condition |
|---|---|
| **PASS** | every phase α t ≥ 1.5 AND every phase excess net ≥ 0 AND mean α t ≥ 1.5 |
| **MID** | mean α t ≥ 1.0 AND mean excess net > 0 AND not majority of phases negative |
| **FAIL** | mean α t < 1.0 OR mean excess net ≤ 0 OR majority of phases negative |

The thresholds match the original gate matrix (`project_next_session_edgar_backfill.md`) adapted from single-point to phase-distributed.

## Step 6 — Action by verdict

### PASS → forward-walk with pre-registration

Document the strategy commit-style in `docs/research/<name>_passed.md`:

- Exact scorer signature, parameter values, period bounds.
- Pre-registration: state forward-walk start date, gate (Sharpe ≥ 0.9 for PASS, 0.7 for MID), maximum drawdown to abort, position size budget.
- Multiple-testing correction: how many strategies have been tested in this signal class to date? Apply Bonferroni or BH-adjustment to required t. (Per Harvey-Liu-Zhu 2016 framework — currently informal; needs to be formalised.)
- Add `__status__ = "ACTIVE"` to scorer's `__init__.py` if it lives in a screener package.

### MID → regime-conditional sizing

If one phase is materially negative or alpha is borderline:

- Halve position size relative to PASS budget.
- Add a regime detector: drawdown threshold, factor-loading shift, or volatility regime.
- Forward-walk with a tighter Sharpe gate (≥ 0.7) and a hard kill on first 90-day window with Sharpe < 0.

### FAIL → kill, document anti-pattern

- Add `__status__ = "CLOSED"` + `__closed_date__` + `__closed_reason__` to the scorer's `__init__.py` (per `tests/test_layer_status.py`).
- Populate `__closed_evidence__` with required gate paths (carhart_4f_hac, sanity_checks_4gate, walk_forward_oos, multiple_testing_correction, cost_drag, bootstrap_ci, survivorship_pit) — use `"N/A: <reason>"` or `"UNTESTED: <reason>"` for gates not run.
- File the multi-phase audit JSON + synthesis MD in `docs/research/`.
- Update `docs/research/paradigm_failures_postmortem.md` if the failure adds a new anti-pattern (don't duplicate established ones).
- Memory: short `project_<name>_failed.md` entry + MEMORY.md index update.

## Common gotchas (from today's session)

1. **Phase-aliasing across runs is silent.** Two backtests of the same strategy on the same period with start dates offset by 1 trading day sample disjoint sets of rebalance days. **Always use the audit driver, not separate engine runs at different start dates.**
2. **Halves sum to full only when phase-aligned.** Full IS at `--is-start 2015-01-02 --is-end 2022-12-31` does NOT equal halves at `--is-start 2015-01-02 --is-end 2018-12-31` + `--is-start 2019-01-02 --is-end 2022-12-31`. The 2019 half lands on a different phase than the full run's 2019 portion. Either:
   - partition saved `report.portfolio_returns` by date offline, or
   - run halves with explicit `--phase-offset` matching full IS phase.
3. **Forward returns include weekends.** `RebalanceSnapshot.portfolio_return` is **1-day forward return** of top-N picks; with `stride=5` we sample only 1-in-5 days. The `× 252` annualisation in `assess()` over-annualises by stride× — but consistently, so cross-strategy comparison is fine.
4. **Universe drift between periods.** `load_pit_union(start, end)` is period-dependent. Without `--lock-universe`, halves use narrower universes than full IS, creating apples-to-oranges in the comparison. Always `--lock-universe` for halves.

## Infrastructure inventory (built today)

Per `feedback_phase_aliasing_in_strided_backtests.md`:

- `BacktestEngine(phase_offset=0..stride-1)` + 6 tests
- `--phase-offset` CLI flag + 5 tests
- `alphalens/backtest/multi_phase.py` (summarise + robust_verdict) + 2 tests
- `scripts/audit_multi_phase.py` driver
- `alphalens/data/fundamentals/edgar_companyfacts.py` — PIT TTM ROE store + 13 tests
- `--lock-universe` flag in tri-factor + mom+lowvol scripts
- `--is-start/--is-end/--oos-start/--oos-end` flags in same

Total: 1349/1349 tests green; ~600 LOC of reusable validation infrastructure.

The Step 0 + Step 4 infrastructure (pre-registration ledger + multi-phase audit + Bonferroni helpers + audit driver) also ships as a standalone OSS toolkit at [`kamilpajak/phase-robust-backtesting`](https://github.com/kamilpajak/phase-robust-backtesting). See [ADR 0006](../adr/0006-phase-robust-backtesting-extraction.md) for the extraction rationale.

## What this playbook does NOT yet have (open gaps)

1. ~~**Pre-registration ledger.**~~ **CLOSED 2026-04-29** — `alphalens preregister add/list/show/complete/threshold` ships with a JSON ledger at `docs/research/preregistration/ledger.json`. `bonferroni_threshold()` returns the corrected critical |t| for the signal class as it stands. Step 0 above.
2. **One-command wrapper.** Steps 2-5 are 4 separate invocations. A `alphalens validate <name>` CLI consolidating them would reduce friction.
3. **Forward-walk harness.** Step 6 (PASS path) is descriptive. No tooling for "run from 2026-Q3 forward, kill if Sharpe < threshold".
4. **Regime detector library.** Step 6 (MID path) requires regime-conditional sizing — no shared utilities yet.
5. **Cost-stress sweep utility.** Each script duplicates cost-stress logic. Consolidation reduces maintenance.
