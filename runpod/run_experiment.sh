#!/usr/bin/env bash
# Wrap an AlphaLens experiment with manifest + tee log + telegram notify.
#
# Usage on the pod:
#   run_experiment.sh "scripts/experiment_event_drift_v4.py --mode breadth-audit \
#                      --start 2024-04-30 --end 2026-04-30 \
#                      --output /workspace/alphalens/runs/<run_id>/artifacts/breadth.json"
#
# What it does:
#   1. Generate run_id = UTC-timestamp + git short SHA
#   2. Write manifest.json (git SHA, deps, env, pod specs)
#   3. Run the wrapped command, tee'ing stdout+stderr to run.log
#   4. On exit (success or failure): Telegram notify if creds are set
#   5. Print run_id so the operator can sync_out.sh and locate results
#
# Auto-stop is intentionally NOT included: keep the pod alive after the run
# so the operator can inspect logs / results before manual stop.

set -uo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: run_experiment.sh \"<python script + args>\"" >&2
    exit 2
fi

CMD="$1"
WORKSPACE="${WORKSPACE:-/workspace/alphalens}"
cd "${WORKSPACE}"

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
TIMESTAMP="$(date -u +%Y%m%d-%H%M%S)"
RUN_ID="${TIMESTAMP}-${GIT_SHA}"
export RUN_ID

RUN_DIR="${WORKSPACE}/runs/${RUN_ID}"
mkdir -p "${RUN_DIR}/artifacts"

echo ">>> Run ID: ${RUN_ID}"
echo ">>> Run dir: ${RUN_DIR}"

.venv/bin/python runpod/manifest.py \
    --run-id "${RUN_ID}" \
    --command "${CMD}" \
    --output "${RUN_DIR}/manifest.json"

# Pre-flight integrity check: surface missing datasets BEFORE the experiment.
.venv/bin/python runpod/verify_data.py || {
    echo "VERIFY FAILED -- aborting before experiment runs" >&2
    exit 3
}

echo ">>> Starting experiment at $(date -u +%H:%M:%S)Z"
echo ">>> Command: ${CMD}"

START_S=$(date +%s)
set +e
.venv/bin/python ${CMD} 2>&1 | tee "${RUN_DIR}/run.log"
EXIT_CODE=${PIPESTATUS[0]}
set -e
END_S=$(date +%s)
DUR=$((END_S - START_S))

echo ">>> Experiment exit code: ${EXIT_CODE}; duration: ${DUR}s"

# Telegram notification (best-effort; missing creds are silently ignored).
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    if [[ "${EXIT_CODE}" == "0" ]]; then
        STATUS="OK"
    else
        STATUS="FAIL (exit ${EXIT_CODE})"
    fi
    MSG=$(printf "AlphaLens runpod\nrun: %s\nstatus: %s\nduration: %ds\ncommand: %s" \
                 "${RUN_ID}" "${STATUS}" "${DUR}" "${CMD}")
    curl -s -X POST \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" \
        -d text="${MSG}" > /dev/null || echo "WARN: telegram notify failed"
fi

echo ">>> Next step (operator):  sync_out.sh    # persist to network volume"
echo ">>> Then:                   runpodctl stop pod \$RUNPOD_POD_ID    # release pod"

exit "${EXIT_CODE}"
