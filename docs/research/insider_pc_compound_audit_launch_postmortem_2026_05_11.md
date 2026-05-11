# Postmortem — insider_pc_compound full audit launch 2026-05-11

**Status:** RESOLVED after THREE launch attempts (1st = pre-screen data, 2nd = stride-mismatch + missing bootstrap, 3rd = custom orchestrator). Audit re-launched on the strategy-specific orchestrator `scripts/run_insider_pc_compound_audit.py`. Two independent prevention layers landed: `alphalens preaudit` framework (PR #97) + experiment-script hard-lock on `--rebalance-stride 21` (PR #98).

## Timeline

| time (UTC) | event |
|---|---|
| 2026-05-11 08:10 | `scripts/launch_dual_audits.sh` invoked on pod `7ou7g05r0er3ds`; tmux sessions `audit-oos` + `audit-fl` started |
| 08:11:36 | both phase-0 subprocesses began `_run_precheck` (signal-independence guard on hardcoded IS 2014-2017 window per memo §3.5) |
| 08:12:16 | both subprocesses raised `Pre-screen aborted: empty P/C feature frame on IS`; phases returned exit 8; tmux sessions wrote `AUDIT_*_DONE=1` |
| ~08:25 | user requested progress check; failure surfaced |
| 08:36 | PR #96 merged: `launch_dual_audits.sh` now passes `--skip-precheck` to both windows |
| 08:37 | re-launch — subprocesses confirmed running with `--skip-precheck` in cmdline; phase 0 proceeding past the prior failure point |

**Compute wasted:** ~27 min of pod time across two phases (× cpu5c-8-16 ≈ $0.05).

## Root cause

`Form4PITStore` and `iVolatility SMD` coverage on the pod `/workspace`
volume:

```
$ ls /workspace/ivolatility_smd/ | wc -l
3099                          # post-2018 cliff: all 3099 files start ≥ 2018-04-30
$ ls ~/.alphalens/ivolatility_smd/ | wc -l   # local Mac
4438                          # full 2007-2026 coverage
```

The pre-2018 backfill (memory entry
`project_ivolatility_pre2018_backfill_2026_05_04`: ~22 GB, 949/1626
tickers) was completed locally on 2026-05-04 but never synced to the
RunPod network volume. The `_run_precheck` guard inside
`scripts/experiment_insider_pc_compound.py` ran
`build_feature_frame(asof_dates=2014..2017_monthly)` against the
pod's iVol cache and got an empty frame because every file's
`tradeDate` minimum is 2018-04-30 — strictly after the precheck
window. The guard correctly classified this as "pre-screen FAIL" and
aborted the audit, but the failure mode was **environmental**, not
signal-corrosion.

## What broke (and why CI didn't catch it)

- The precheck guard itself worked as designed (memo §7 risk #4
  defense-in-depth).
- `tests/test_compound_audit_equivalence.py` golden master locks the
  full pipeline output but uses 2019-Q1 (post-cliff) — pre-cliff IS
  data is never exercised in CI.
- `runpod/verify_data.py` is existence-only — it confirmed all data
  dirs were present on the pod but did NOT check whether the data
  ranged back to 2014.
- The launcher script's own docstring already endorsed
  `--skip-precheck` *"on runpod when the guard has already cleared
  locally"* — the launcher just didn't pass the flag.

## Fix shipped

**Tactical (PR #96, `e5ebe83`):** pass `--skip-precheck` to both tmux
sessions in `launch_dual_audits.sh`. Pre-screens are TDD-verified
locally per memo §3.5 (ρ=-0.000035, coverage 154 mean,
EXTREME counter-cyclical P/C); component hash guard
(`_verify_component_hashes`) provides defense-in-depth against silent
code drift on the pod.

**Structural (PR #97+, this PR):** `alphalens preaudit <strategy>`
framework — strategy-agnostic, runs **before** any `alphalens audit`
launch. Two stages:

1. **Coverage** — per-`DataDep` checks that each data dir exists, is
   non-empty, and (for date-typed deps) actually spans the audit
   window. Uses multi-ticker sampling (zen 2026-05-11 critical catch:
   single-ticker AAPL peek would false-pass when most of the universe
   has shorter history).
2. **Smoke** — invokes the strategy's `experiment_*.py` on a tiny
   universe (cap=300) + short window (1 quarter) with ephemeral
   `--out /tmp/preaudit_smoke_<uuid>.json` (zen 2026-05-11 catch:
   prevents smoke from overwriting a concurrent audit's output).

`scripts/launch_dual_audits.sh` now invokes `alphalens preaudit
insider_pc_compound --skip-smoke` as a fail-fast gate before launching
either tmux session. (`--skip-smoke` because the smoke fixture window
overlaps with the OOS audit and would briefly contend on MooseFS
reads; coverage alone catches today's failure class.)

## What this framework DOES catch

- Missing data dir / empty data dir.
- Coverage gap — data dir exists but doesn't span the audit window
  (today's bug).
- Component-hash drift — the experiment script's
  `_verify_component_hashes` fires inside the smoke subprocess.
- Pre-reg constant drift — `tests/test_compound_audit_pre_reg_lock.py`
  catches this at CI time (and the smoke subprocess imports the
  experiment script, which would trip too).
- CLI passthrough breakage — smoke actually invokes the subprocess
  via the same code path used by `alphalens audit`.
- Any end-to-end pipeline failure (import errors, schema mismatches,
  iVol loader bugs, …).

## What this framework does NOT catch (honest scope, per zen review)

- **OOM-at-scale.** Smoke runs cap=300; full audit runs cap≈2000.
  Memory pressure that only manifests at full universe (or
  concurrent OOS+FL on the same pod) is invisible. Treat
  `runpod/audit_v4_memory.py` as the complement here.
- **MooseFS I/O contention under N concurrent workers.** Smoke is
  single-process. The
  `feedback_runpod_moosefs_process_pool_antipattern.md` finding
  (`ProcessPoolExecutor` on the network volume is counterproductive)
  cannot be reproduced by a single-process smoke.
- **Time-varying signal corrosion.** Smoke is a fixed-window
  reproducibility check; verdicts on alpha quality come from the
  audit itself, not the smoke.

## What changed in operational discipline

- Any future audit launcher SHOULD prepend a `preaudit` invocation.
- New strategy onboarding requires adding a `SmokeProfile` to
  `alphalens/preaudit/profiles.py::SMOKE_PROFILES` and a CI test that
  the profile resolves in `audit._SCRIPTS` (already enforced by
  `tests/test_preaudit_profiles.py::TestSmokeProfileRegistry`).
- `runpod/README.md` now mentions `alphalens preaudit` as a
  per-session step between bootstrap and `run_experiment.sh`.

## Lessons (for the project's "Workflow conventions")

1. **Existence checks aren't coverage checks.** `runpod/verify_data.py`
   is necessary but not sufficient; coverage of the actual audit
   window matters.
2. **Operational gotchas should fail at minute 2, not minute 1.5 ×
   N_phases.** Today's failure cost ~27 min × 0 useful work; a
   coverage check would have failed in <5 s.
3. **The script's own docstring is a hint.** `experiment_insider_pc_compound.py`
   already documented `--skip-precheck` for runpod usage; the
   launcher missed the cue. Code-as-documentation works only if
   adjacent code reads it.
4. **Pre-2018 iVol SMD remains a deferred operational task.** ~22 GB
   sync from local Mac → pod network volume would enable
   pre-2018 retrospective audits without `--skip-precheck`. Not
   blocking today's audit; revisit when a pre-2018 strategy lands.

---

## Addendum 2026-05-11 (~10:30 UTC) — second launch ABORTED at 2h, methodology bug

### What happened

The 08:37 re-launch (with `--skip-precheck` patch from PR #96) ran for
~2 hours of phase 0 before a process-tree inspection revealed the
subprocesses were running with a **5-day rebalance cadence** instead
of the memo §3.1 / §4 locked **21-day monthly**:

```
.venv/bin/python scripts/experiment_insider_pc_compound.py \
    --rebalance-stride 5 --phase-offset 0 \
    --is-start 2018-01-01 --is-end 2023-12-31 --skip-precheck
```

`trading_calendar[0::5]` produced ~302 rebalances per phase over the
6-year OOS window — **4.2x the locked 72 monthly rebalances**.

### Root cause

The generic `alphalens audit` CLI driver
(`phase_robust_backtesting.audit_multi_phase.run_audit`) conflates two
semantically distinct concepts:

```python
def run_audit(..., rebalance_stride=5):
    for phase in range(rebalance_stride):                  # ← number of phases
        cmd = ["--rebalance-stride", str(rebalance_stride), # ← day-step (SAME arg!)
               "--phase-offset", str(phase)]
```

The launcher's intent (`--rebalance-stride 5` = "5 phase offsets")
was correct as written; the driver's semantics (`rebalance_stride` =
"day step") quietly differed. With `_REBALANCE_STRIDE_LOCK = 21` as
the experiment script's default but `argparse` allowing CLI override,
the locked value silently lost to the override.

Zen 2026-05-11 review surfaced a SECOND deviation: **the generic
driver does not perform memo §5.4's synchronous-across-phases
block-bootstrap** (1000 reps × block_size=126 trading days for
`bounds_alpha_t_lower/upper`). It only summarises per-phase stderr.
Even with the stride fixed, the resulting audit JSON would have been
non-compliant with §5.4 — Romano-Wolf bounds simply absent.

The `insider_form4_opportunistic` PASS_MARGINAL audit (2026-05-05)
did NOT use the generic driver — it used a strategy-specific custom
script `scripts/run_insider_form4_phase_b.py` that separates `N_PHASES`
from `REBALANCE_STRIDE_DAYS` and runs the synchronous block-bootstrap
inline. This precedent was the right pattern; the compound launch
should have mirrored it from the start.

### Compute cost of the second abort

- 2h ~ 4 min phase setup + 1h56m on phase 0 prebuild + scoring
- Both OOS and final-lock subprocesses pinned ~99% CPU
- $0.29/h × 2h ≈ **$0.58 wasted compute**
- Cumulative across the day (3 launches): ~$0.65

### Structural fix shipped (PR #98)

Three changes, all defensive:

1. **`scripts/run_insider_pc_compound_audit.py`** — strategy-specific
   custom orchestrator, byte-for-byte mirror of the form4 launcher
   except the verdict matrix follows memo §5.1's compound-specific
   per-phase floors:
   - PASS: every phase αt ≥ 1.5 AND mean αt ≥ 2.974
   - PASS_MARGINAL: every phase αt ≥ 0 AND mean αt ∈ [2.50, 2.974)
   - INCONCLUSIVE: in-band mean with ≥1 phase αt < 0; or dispersion > 70pp
   - FAIL: mean αt < 2.50 OR mean excess_net_ann < 0

   `N_PHASES = 5` and `REBALANCE_STRIDE_DAYS = 21` are TOP-LEVEL
   constants; the subprocess argv hardcodes `--rebalance-stride 21`
   so a future caller cannot reintroduce the mismatch.

2. **Experiment-script hard-lock** in `experiment_insider_pc_compound.py
   main()`:
   ```python
   if args.rebalance_stride != _REBALANCE_STRIDE_LOCK:
       sys.stderr.write("PRE-REG VIOLATION: ...")
       return 9
   ```
   `--rebalance-stride 5` now exits with code 9 before any compute,
   plus a `PRE-REG VIOLATION` stderr message. Smoke run continues to
   work (preaudit profile already passes `--rebalance-stride 21`).

3. **`scripts/launch_dual_audits.sh`** rewritten to invoke the custom
   orchestrator directly via `python <path>` instead of routing through
   `alphalens audit`. Removes the driver-semantics dependency entirely.

### Tests added

- `tests/test_compound_audit_pre_reg_lock.py::TestEffectiveRebalanceStrideHardLock`
  — 4 tests: stride=5 fails, stride=42 fails, default (21) passes,
  explicit 21 passes. Uses subprocess to invoke `main()` with synthetic
  argv.
- `tests/test_run_insider_pc_compound_audit.py` — 18 tests on the
  orchestrator: cmdline locks stride=21 across phases, constants match
  memo, verdict matrix covers all rows of memo §5.1 (incl. boundary
  cases at 2.974 and 2.50).

### What the framework did NOT catch (gap closed)

The `alphalens preaudit` framework shipped earlier the same day (PR
#97) was deliberately scoped to environment + coverage failures, NOT
methodology drift inside the experiment script's CLI. The stride
mismatch was a code-level deviation from memo, not a data/env
failure. The new hard-lock + custom orchestrator closes the methodology
drift class; preaudit closes the data/env class. The two layers are
complementary.

### Operational discipline going forward

- Strategies destined for pre-reg audit MUST have a strategy-specific
  orchestrator that hardcodes the locked rebalance stride and emits
  the synchronous block-bootstrap output. The generic `alphalens
  audit` CLI is appropriate only for exploratory single-window
  sweeps where neither the stride nor the bootstrap method is
  pre-registered.
- Constant-lock tests (`_REBALANCE_STRIDE_LOCK == 21` in
  `tests/test_compound_audit_pre_reg_lock.py`) should be paired with
  **effective-value tests** that exercise `main()` end-to-end and
  assert the CLI cannot drift the locked value past the guard.

### Memo §5.1 verdict-matrix amendment (zen CR finding on PR #98)

The PASS_MARGINAL row in memo §5.1 specifies `mean αt ∈ [2.50, 2.974)`.
That bracket strictly excludes mean ≥ 2.974, which leaves the case
`(mean ≥ 2.974, every-phase ≥ 0, NOT every-phase ≥ 1.5, dispersion ≤ 70pp,
excess_net ≥ 0)` unclassified by the literal matrix — the PASS row
requires every phase ≥ 1.5, the PASS_MARGINAL row excludes mean ≥
2.974, and the FAIL / INCONCLUSIVE rows don't apply either.

Operationally, a stronger mean αt with one weak phase is at least as
good as a PASS_MARGINAL result; classifying it as INCONCLUSIVE (the
catch-all in the original implementation) would penalise a stronger
signal more harshly than a weaker one. `_classify_verdict` in the
custom orchestrator therefore widens PASS_MARGINAL's lower-bound-only
check: `mean ≥ 2.50 AND every-phase ≥ 0` returns PASS_MARGINAL after
the PASS branch has been exhausted. Since PASS is evaluated first,
this never claims PASS_MARGINAL for a result that qualifies for full
PASS.

Treat this as a memo §5.1 clarification rather than a deviation. The
verdict ladder intent (PASS > PASS_MARGINAL > INCONCLUSIVE > FAIL)
is preserved; the only change is that "stronger mean αt with weak phase
robustness" is now PASS_MARGINAL instead of INCONCLUSIVE. Test
`tests/test_run_insider_pc_compound_audit.py::test_high_mean_weak_phase_yields_pass_marginal_not_inconclusive`
locks this interpretation.

### HAC L/T ratio reporting (memo §7 risk #7 mandate)

The orchestrator's JSON output now includes `gates.hac_lt_ratio` (HAC
maxlags / bootstrap n_obs) plus a boolean `hac_lt_warning` flag when
the ratio exceeds the Andrews-Monahan small-sample rule of thumb
(L/T > 0.20). On the final-lock window (~567 obs), the ratio is
~22% → warning fires → downstream consumers know to rely on the
Romano-Wolf bounds (`bounds_alpha_t_lower/upper`) rather than the raw
HAC t-stat for primary inference, exactly as memo §7 risk #7
designates.
