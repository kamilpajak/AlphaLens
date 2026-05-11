# Postmortem — insider_pc_compound full audit launch 2026-05-11

**Status:** RESOLVED (audit running on re-launch; `alphalens preaudit` framework deployed to prevent recurrence).

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
