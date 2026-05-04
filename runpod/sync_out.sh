#!/usr/bin/env bash
# Push experiment results + manifest from ephemeral pod disk back to the
# runpod network volume (/network/results/<run_id>/).
#
# Each invocation creates a unique run_id directory. Existing results are
# never overwritten. Run id pattern: YYYYMMDD-HHMMSS-<git_short_sha>.

set -euo pipefail

NETWORK_DIR="${NETWORK_DIR:-/network}"
WORKSPACE="${WORKSPACE:-/workspace/alphalens}"
RUN_ID="${RUN_ID:-}"

if [[ -z "${RUN_ID}" ]]; then
    echo "ERROR: RUN_ID is not set (export it before sync_out)" >&2
    exit 1
fi

DEST="${NETWORK_DIR}/results/${RUN_ID}"
mkdir -p "${DEST}"

echo ">>> Persist results to ${DEST}"

# Files conventionally written by run_experiment.sh.
for f in run.log manifest.json; do
    src="${WORKSPACE}/runs/${RUN_ID}/${f}"
    if [[ -f "${src}" ]]; then
        cp "${src}" "${DEST}/"
        echo "    [ok] ${f}"
    fi
done

# Experiment-specific JSON / parquet outputs (whatever the script wrote
# under runs/<run_id>/artifacts/).
ART_SRC="${WORKSPACE}/runs/${RUN_ID}/artifacts"
if [[ -d "${ART_SRC}" ]]; then
    rsync -a --delete "${ART_SRC}/" "${DEST}/artifacts/"
    echo "    [ok] artifacts/ ($(du -sh "${ART_SRC}" | awk '{print $1}'))"
fi

echo ">>> sync_out done -> ${DEST}"
