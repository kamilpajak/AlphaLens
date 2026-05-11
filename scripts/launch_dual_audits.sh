#!/usr/bin/env bash
# Concurrent OOS + final-lock audit launcher for insider_pc_compound.
#
# 2026-05-11 amendment: switched from the generic `alphalens audit` CLI to
# the strategy-specific orchestrator `scripts/run_insider_pc_compound_audit.py`.
# Reason: the generic driver
# (`phase_robust_backtesting.audit_multi_phase.run_audit`) conflates
# "number of phases" with "rebalance day-step" — passing `--rebalance-stride 5`
# intending "5 phase offsets" silently runs 5-day cadence inside the
# experiment, deviating from memo §3.1's locked 21d monthly stride. ALSO:
# the generic driver does not perform the synchronous-across-phases
# block-bootstrap required by memo §5.4. The custom orchestrator pins
# REBALANCE_STRIDE_DAYS=21 + N_PHASES=5 + runs the bootstrap inline,
# mirroring the form4-PASS_MARGINAL launcher pattern. See
# `docs/research/insider_pc_compound_audit_launch_postmortem_2026_05_11.md`.
#
# Pre-reg windows are LOCKED per
# docs/research/insider_pc_compound_design_2026_05_10.md:
#   OOS:        2018-01-01 → 2023-12-31
#   Final-lock: 2024-01-01 → 2026-03-31
#
# Both windows run --skip-precheck because the RunPod /workspace volume
# carries only post-2018 iVolatility SMD coverage; pre-2018 cache lives
# on the local Mac. Pre-screens are already TDD-verified locally per
# memo §3.5. The component-hash guard inside
# experiment_insider_pc_compound.py provides defense-in-depth against
# silent code drift on the pod.
#
# Monitoring (every few hours):
#   tmux ls
#   tmux capture-pane -p -t audit-oos | tail -40
#   tmux capture-pane -p -t audit-fl  | tail -40
#   grep AUDIT_ /workspace/{oos,fl}_audit.log
#
# Output JSON files written by the orchestrator:
#   /workspace/AlphaLens/docs/research/insider_pc_compound_oos_<date>.json
#   /workspace/AlphaLens/docs/research/insider_pc_compound_finallock_<date>.json
#
# scp those locally after both AUDIT_*_DONE markers fire.

set -euo pipefail

cd /workspace/AlphaLens
source /etc/rp_environment
export PATH="/root/.local/bin:$PATH"

# Pre-audit smoke gate (PR #97 framework): coverage check on the
# strategy's data deps. Aborts BEFORE tmux launch if the runpod
# environment is missing data — would catch today's 2026-05-11 false-
# launch (post-2018-only iVol SMD on pod, precheck wanted 2014-2017).
echo ">>> pre-audit smoke gate"
.venv/bin/alphalens preaudit insider_pc_compound --skip-smoke \
    || { echo "PRE-AUDIT COVERAGE FAILED — aborting before tmux launch" >&2; exit 1; }
# Smoke subprocess skipped here because the smoke fixture window
# (2019-Q1) overlaps with the OOS audit window and would briefly
# contend on MooseFS reads. Coverage check alone catches the
# environmental-data-missing failure class.

ORCHESTRATOR=/workspace/AlphaLens/scripts/run_insider_pc_compound_audit.py
# Per-window artifact roots — CRITICAL: without distinct paths, the OOS
# and FL orchestrator instances would both write per-phase outputs to
# ~/.alphalens/audit/insider_pc_compound/phase_{0..4}_{returns.parquet,report.md},
# stomping on each other (2026-05-11 launch-4 incident).
ARTIFACT_ROOT_OOS=/root/.alphalens/audit/insider_pc_compound/oos
ARTIFACT_ROOT_FL=/root/.alphalens/audit/insider_pc_compound/finallock

# `remain-on-exit on` keeps the pane visible after the command exits so
# the scrollback (audit progress + AUDIT_*_DONE marker) stays inspectable.
# `set -o pipefail` is critical: without it `cmd | tee` returns tee's exit
# code (always 0), so AUDIT_*_DONE would falsely report success even if the
# audit crashed mid-run — silently producing a half-failed verdict artifact.
tmux new-session -d -s audit-oos \
    "set -o pipefail; ALPHALENS_WORKERS=1 .venv/bin/python ${ORCHESTRATOR} \
     --is-start 2018-01-01 --is-end 2023-12-31 \
     --artifact-root ${ARTIFACT_ROOT_OOS} \
     --out-suffix oos_$(date +%Y-%m-%d) --skip-precheck 2>&1 \
     | tee /workspace/oos_audit.log; \
     echo AUDIT_OOS_DONE=\$? >> /workspace/oos_audit.log" \; \
    set-option -t audit-oos remain-on-exit on

tmux new-session -d -s audit-fl \
    "set -o pipefail; ALPHALENS_WORKERS=1 .venv/bin/python ${ORCHESTRATOR} \
     --is-start 2024-01-01 --is-end 2026-03-31 \
     --artifact-root ${ARTIFACT_ROOT_FL} \
     --out-suffix finallock_$(date +%Y-%m-%d) --skip-precheck 2>&1 \
     | tee /workspace/fl_audit.log; \
     echo AUDIT_FL_DONE=\$? >> /workspace/fl_audit.log" \; \
    set-option -t audit-fl remain-on-exit on

echo "Both audit sessions launched in tmux:"
tmux ls
echo ""
echo "Monitor: tmux capture-pane -p -t audit-oos | tail -40"
echo "Monitor: tmux capture-pane -p -t audit-fl  | tail -40"
