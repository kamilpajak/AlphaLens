#!/usr/bin/env bash
# Single-tmux launcher for the idiosyncratic_momentum 3-window x 5-phase audit
# on a runpod CPU pod.
#
# Pre-reg windows are LOCKED per memo section 6:
#   IS:  2010-01-01 → 2017-12-31
#   OOS: 2018-01-01 → 2021-12-31
#   FL:  2022-01-01 → 2024-12-31
#
# Orchestrator runs all 3 windows sequentially (each spawning 5 parallel
# subprocess phases). Single tmux session is enough — same pattern as
# ev_fcff_yield, distinct from insider_pc_compound which needed dual
# tmux for separate OOS / FL machines.
#
# Monitoring:
#   tmux ls
#   tmux capture-pane -p -t audit-idio | tail -60
#   tail -f /workspace/idio_audit.log
#   grep AUDIT_IDIO_DONE /workspace/idio_audit.log
#
# Output:
#   /workspace/AlphaLens/docs/research/idiosyncratic_momentum_audit_<date>.json
#
# scp that locally after AUDIT_IDIO_DONE marker fires.

set -euo pipefail

cd /workspace/AlphaLens
source /etc/rp_environment
export PATH="/root/.local/bin:$PATH"

# Pre-audit smoke gate (PR #97 framework): coverage check on the
# strategy's data deps. Aborts BEFORE tmux launch if the runpod
# environment is missing data.
echo ">>> pre-audit smoke gate"
.venv/bin/alphalens preaudit idiosyncratic_momentum_2026_05_14_v1 --skip-smoke \
    || { echo "PRE-AUDIT COVERAGE FAILED — aborting before tmux launch" >&2; exit 1; }
# --skip-smoke because the 2020-Q1-Q2 smoke window overlaps with the OOS
# audit window and would briefly contend on the same prices cache.
# Coverage check alone catches the environmental-missing-data class.

ORCHESTRATOR=/workspace/AlphaLens/scripts/run_idiosyncratic_momentum_audit.py
ARTIFACT_ROOT=/root/.alphalens/audit/idiosyncratic_momentum

tmux new-session -d -s audit-idio \
    "set -o pipefail; ALPHALENS_WORKERS=1 .venv/bin/python ${ORCHESTRATOR} \
     --artifact-root ${ARTIFACT_ROOT} 2>&1 \
     | tee /workspace/idio_audit.log; \
     echo AUDIT_IDIO_DONE=\$? >> /workspace/idio_audit.log" \; \
    set-option -t audit-idio remain-on-exit on

echo "Audit session launched in tmux:"
tmux ls
echo ""
echo "Monitor: tmux capture-pane -p -t audit-idio | tail -60"
echo "         tail -f /workspace/idio_audit.log"
echo "         grep AUDIT_IDIO_DONE /workspace/idio_audit.log"
