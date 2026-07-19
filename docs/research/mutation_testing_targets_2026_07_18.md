# Mutation-testing targets — critical-area map

**Status:** LIVING (2026-07-18). Canonical list of which modules deserve
mutation testing and in what order, plus the operator recipe.

## Why this document

Mutation testing (cosmic-ray) measures **test-suite quality**, not coverage: it
makes a small edit ("mutant") to the source and checks the suite catches
("kills") it. A surviving mutant is a real gap — the tests still pass even though
the behaviour changed. We run it on **pure deterministic logic whose silent
failure would corrupt a money-relevant decision**: ledger verdicts, trade-setup
numbers shown to the group, and candidate selection/ordering.

Run a target **before** relying on it for a new decision and **after** any
significant refactor. It is not a CI gate (too slow); it is a periodic audit that
leaves behind pinning tests.

## Prioritisation

Each candidate is scored `criticality × suitability` (both 1–5):

- **criticality 5** — a silent bug corrupts a money-relevant decision.
- **suitability 5** — pure deterministic logic with a fast (<3 s) existing
  `unittest.TestCase` scope. Suitability ≤2 means the module needs a direct test
  file written first; mutation testing there is blocked until then.

## Completed runs

| Module | PR | Result |
|--------|----|--------|
| `backtest/metrics.py` (config seeded) | #845 | cosmic-ray dev-dep + working config |
| `backtest/multi_phase` doctrine-verdict + gate logic | #851 | boundary + gate pins |
| `backtest/sharpe_inference.py` | #852 | 353/389 = **90.7%**; 36 documented equivalents |
| `feedback/ladder_replay.py` (batch 1) | #853 | +62 mutants killed; ~710 survivors deferred (batch 2+) |
| `backtest/weighting.py` | #854 | 211/231 = **91.3%** (was 78.4%); 20 documented equivalents |
| `paper/sizing.py` | *this PR* | 425/440 = **96.6%** (was 86.1%); 15 documented equivalents |

### `feedback/ladder_replay.py` — batch 1 (#853)

The `/edge` ledger replay engine (1666 LOC): computes `realized_r`,
`filled_fraction`, MFE/MAE and the terminal classification that is the **sole
go-forward feedback metric** for real-money picks. Full run: **2163 mutants,
1216 killed by the prior suite, 947 survived (56.2 % baseline)**.

Batch 1 triaged the first ~¼ of survivors (237 mutants): **62 KILLABLE / 175
EQUIVALENT**. 47 new pinning tests
(`tests/feedback/test_ladder_replay_mutation_hardening.py`) kill all 62,
verified by a targeted cosmic-ray re-run (62/62 KILLED, source clean). The 175
equivalents are almost all `X | Y` swaps inside type annotations (dead under
`from __future__ import annotations`) plus a few proven-equivalent guards.

**Follow-up (backlog):** ~710 survivors from the remaining chunks are not yet
triaged — a `batch 2+` effort on the same module. The module's size means it
warrants several focused PRs rather than one mega-diff.

### `backtest/weighting.py` — complete (#854)

Position-weighting schemes (`compute_position_weights` + `weighted_return`, 87
LOC) — scale every portfolio return the engine emits, so a silent bug shifts
every backtested Sharpe/alpha. Full run: **231 mutants, 181 killed by the prior
suite, 50 survived (78.4 % baseline)**. 14 new pinning tests kill all 30 killable
survivors (verified by a targeted re-run), lifting the score to **91.3 %**. The
20 documented equivalents: the `if raw.sum() == 0` conviction safety branch is
unreachable (the top tier always contributes 2.0, so `raw.sum() ≥ 2.0`), making
its 4 guard mutants + 13 body mutants inert; `len(...) <= 0 ≡ == 0` (lengths are
non-negative); and `max(1, (n+2)//3) ≡ max(0, (n+2)//3)` since `(n+2)//3 ≥ 1`
for every reachable `n ≥ 1`.

### `paper/sizing.py` — complete (this PR)

Pure position-sizing math (`validate_trade_setup`, `compute_daily_scale_factor`,
`compute_setup_plan`, 367 LOC) — turns a `brief_trade_setup` into concrete share
quantities and the account-currency notional (incl. the FX leg, PR #849). Full
run: **440 mutants, 379 killed by the prior suite, 61 survived (86.1 %
baseline)**. 33 new pinning tests
(`tests/paper/test_sizing_mutation_hardening.py`) kill all 46 killable survivors
(verified by a targeted re-run, 46/46), lifting the score to **96.6 %**. Coverage:
plannability guards (status/size/stop/tier boundaries, value-not-identity `status
== "OK"`), the scale-factor short-circuits (non-positive equity, zero/negative
aggregate), the per-tier and per-tranche skip/continue loops + default values, the
FX guards (same-currency rejection, rate boundaries, the one-line notional
conversion driving qty), and both keyword-only signature markers.

The 15 documented equivalents: 11 are `X | Y` swaps inside the `fx: FxConversion
| None` annotation (dead under `from __future__ import annotations`);
`math.floor(a // b)` ≡ `a // b` ≡ `math.floor(a / b)` for the qty floor; the two
`.get("limit", 0)`/`or 0` → `-1` mutants leave a missing/zero limit non-positive
so the tier is still dropped; and `paper_equity < 0` ≡ `<= 0` because equity `== 0`
yields a zero aggregate that returns 1.0 via the next guard anyway.

## Backlog (ranked)

Not yet run. `crit·suit` descending; `pipe:` = `apps/alphalens-pipeline/alphalens_pipeline/`,
`res:` = `apps/alphalens-research/alphalens_research/`.

| crit·suit | Module | Why it is critical |
|-----------|--------|--------------------|
| 25 | `pipe:data/store/form4_pit.py` | PIT integrity SoT for insider data — `filed_date<=asof` window + transaction lookback; a leak biases every insider signal. |
| 25 | `pipe:scorers/cohen_malloy_classifier.py` | ROUTINE/OPPORTUNISTIC/UNCLASSIFIED label gates which insider trades count — the only project positive line. |
| 25 | `pipe:thematic/screening/selection_score.py` | THE primary brief sort key (`layer4 − ATR ramp penalty`); a silent bug reorders every card. |
| 25 | `pipe:thematic/trade_setup/ladder.py` | Sole producer of the entry-tier + TP-tranche ladders the group trades. |
| 25 | `pipe:thematic/trade_setup/sizing.py` | Equal-risk allocation math + 25 % exposure cap. |
| 25 | `res:attribution/cost_model.py` | Cost drag turns gross into the net returns the Carhart regression + ledger verdict see. |
| 25 | `res:attribution/factor_analysis.py` | Produces the Carhart-4F alpha t-stat every ledger verdict compares to the bar. |
| 25 | `res:attribution/signal_vol_regime.py` | Its `proceed` verdict IS the mandatory Layer-4 overlay pre-screen gate. |
| 25 | `res:backtest/engine.py` | Produces `BacktestReport.portfolio_returns` — input to every Sharpe/Carhart/Bonferroni call. |
| 20 | `pipe:data/factors.py` | Sole loader of FF5/UMD/Industry12/Q4 factor returns feeding attribution. |
| 20 | `pipe:feedback/benchmark_excess.py` | `market_excess = forward − benchmark_window` — the `/edge` headline metric. |
| 20 | `pipe:feedback/population_ladder_monitor.py` | The `/edge` ledger SoT writer: plannability gating + touch-trigger screen. |
| 20 | `pipe:scorers/fcff_yield.py` | FCFF/EV imputation + z-score ranking feeding the live valuation signal. |
| 20 | `pipe:scorers/opportunistic_form4.py` | SHA-locked pre-reg scorer — **source frozen**, test-only hardening allowed. |
| 20 | `pipe:thematic/dedup.py` | Collapses multi-outlet echoes upstream of the catalyst resolver. |
| 20 | `pipe:thematic/mapping/catalyst_resolver.py` | Catalyst presence is stage 1 of the 5-stage selection funnel. |
| 20 | `pipe:thematic/screening/scorer.py` | Composes `layer4_weighted_score` (insider 2×, clip 1–5) + is_pass gates. |
| 20 | `pipe:thematic/trade_setup/builder.py` | Orchestrates the whole trade-setup (swing-lows/MAs/ATR fallbacks). |
| 20 | `pipe:thematic/trade_setup/levels.py` | ZigZag swing-point state machine feeding every support level. |
| 20 | `res:backtest/metrics.py` | The functions NOT covered by #845 — `sharpe_autocorr_adjusted`, `per_rebalance_turnover`. |
| 20 | `res:diagnostics/slippage_regime.py` | Spread-stress re-evaluation of a pre-registered net-alpha. |
| 20 | `res:overlays/vol_target.py` | The `.shift(1)` causality contract — a look-ahead here fakes alpha. |
| 16 | `pipe:feedback/breakeven_lenses.py` | Kind-dispatch for the exit-stop what-if grid. |
| 16 | `pipe:paper/calendar.py` | Session arithmetic behind TTL/time-stop sweeps + shadow-return anchors. |
| 16 | `pipe:thematic/extraction/themes.py` | `roll_up` novelty ratio + `flag_novel` threshold. |
| 16 | `res:attribution/walk_forward.py` | C1–C5 gate → PASS/BORDERLINE/FAIL stability verdict. |
| 15 | `pipe:feedback/execution_cost.py` | Per-arm execution-cost haircut. |
| 15 | `res:diagnostics/nofill.py` | NO_FILL root-cause classifier. |
| 12 | `pipe:scorers/_common.py` | Shared `winsorize` + `rank_zscore` normalisation. |
| 9 | `res:backtest/historical_validation.py` | DEPLOY/ITERATE/SKIP aggregate decision. |
| 8 | `pipe:feedback/bar_window.py` | `_window_vwap` anchor — **needs a direct test file first** (suitability 2). |

## Operator recipe

Run from the **repo root** so relative paths resolve. Config lives beside the
target's app (`apps/alphalens-research/cosmic-ray.toml` is the seeded example).

```bash
cosmic-ray init  <config.toml> /tmp/cr.sqlite
cosmic-ray baseline <config.toml>          # suite must be green on the unmutated source
cosmic-ray exec  <config.toml> /tmp/cr.sqlite
cr-rate   /tmp/cr.sqlite                    # survival %
cr-report /tmp/cr.sqlite                    # per-mutant killed/survived + diff
```

Hard-won rules:

- **`test-command` must call `.venv/bin/python` directly**, not `uv run` — per-mutant
  `uv` re-resolution times out and can trigger a full `uv sync` from a worktree
  with no local `.venv`.
- **PYTHONPATH canary before every run.** Editable installs resolve
  `alphalens_pipeline`/`alphalens_research` to the **main checkout**, so a
  worktree run silently mutates the wrong copy. Prefix the test-command with
  `env PYTHONPATH=<worktree>/apps/alphalens-pipeline:<worktree>/apps/alphalens-research`
  and assert `import <module>; module.__file__` points inside the working tree.
- **Never hard-kill `exec`.** cosmic-ray mutates the file in place and reverts
  after each mutant; a SIGTERM mid-mutant leaves the source mutated (restore with
  `git checkout -- <module>`). `exec` is resumable — re-run the same command and
  it skips completed mutants.
- **Targeted survivor re-verification is cheap.** Copy the finished sqlite,
  `DELETE FROM work_results WHERE test_outcome='SURVIVED'` (or restrict to the
  specific `job_id`s you wrote tests for), then `exec` again — it re-runs only
  those mutants (minutes, not the full run). Add any new test file to the config's
  `test-command` first.
- **Annotation mutants are auto-equivalent** when the module has
  `from __future__ import annotations` — the annotations are never evaluated.
- **Document accepted equivalents in the PR body** so a later run has a clean
  baseline and does not re-litigate them.

## Out of scope

- LLM / network client glue (`*_client.py`, ingest adapters) — behaviour is IO,
  not deterministic logic; covered by golden-replay + live probes instead.
- `apps/web` (SvelteKit) — different tooling.
- `phase-robust-backtesting` (`multiple_testing`, Bonferroni thresholds) — an
  external repo; mutate it there in a separate effort.
