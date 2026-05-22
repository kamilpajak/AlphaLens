#!/usr/bin/env bash
# Pod-side A/B benchmark for the Form4PITStore partition cache (PR #93).
#
# Runs the same single-phase compound smoke twice — once on `main`
# (no cache) and once on `feature/form4-partition-cache` — and prints
# the wall-time delta. Designed to land a clean head-to-head on the
# same pod CPU so MooseFS variance doesn't muddy the result.
#
# Usage on the pod:
#   cd /workspace/AlphaLens && bash runpod/bench_form4_cache.sh
#
# Expected:
#   - Baseline (main):                ~20 min
#   - With cache (PR #93):            ~2-4 min target
#   - Reports both wall times + ratio
#
# Total compute: ~22-25 min on a 16-vCPU pod.
# ALPHALENS_WORKERS=1 is mandatory on MooseFS (see CLAUDE.md +
# feedback_runpod_moosefs_process_pool_antipattern.md).

set -euo pipefail

cd /workspace/AlphaLens
source /etc/rp_environment
export PATH="/root/.local/bin:$PATH"
export ALPHALENS_WORKERS=1

LOG_DIR=/workspace/bench_form4_cache_$(date -u +%Y%m%d-%H%M%S)
mkdir -p "${LOG_DIR}"
echo ">>> Logs in ${LOG_DIR}"

# Single-phase smoke matching tests/test_compound_audit_equivalence.py
# fixture window. Avoids the ~30-min precheck guard via --skip-precheck
# and runs one rebalance phase only (--phase-offset 0,
# --rebalance-stride 21 → 6 rebalance snapshots over 6 months).
COMMON_ARGS=(
    --is-start 2019-01-01 --is-end 2019-06-30
    --universe-size-cap 300
    --phase-offset 0 --rebalance-stride 21
    --skip-precheck
)

run_one() {
    local label="$1"; shift
    local branch="$1"; shift
    local out_json="${LOG_DIR}/${label}.json"
    local out_log="${LOG_DIR}/${label}.log"

    echo
    echo "=========================================="
    echo "[${label}] checkout ${branch}"
    echo "=========================================="
    git fetch origin "${branch}" --quiet
    git checkout "${branch}" --quiet
    git reset --hard "origin/${branch}" --quiet
    git rev-parse HEAD

    # Re-sync deps in case they drifted (cheap if nothing changed).
    uv sync --frozen --quiet

    echo "[${label}] starting at $(date -u +%H:%M:%S)Z"
    local start_s; start_s=$(date +%s)
    /usr/bin/time -p .venv/bin/python apps/alphalens-research/scripts/experiment_insider_pc_compound.py \
        "${COMMON_ARGS[@]}" \
        --out "${out_json}" \
        2>&1 | tee "${out_log}"
    local end_s; end_s=$(date +%s)
    local wall=$((end_s - start_s))

    echo "[${label}] wall=${wall}s"
    printf "%s\t%d\n" "${label}" "${wall}" >> "${LOG_DIR}/timing.tsv"
}

# Save current branch so we can restore it.
ORIG_BRANCH=$(git rev-parse --abbrev-ref HEAD)
trap 'git checkout "${ORIG_BRANCH}" --quiet || true' EXIT

run_one "baseline_main"        "main"
run_one "with_cache_pr93"      "feature/form4-partition-cache"

echo
echo "=========================================="
echo "RESULTS (wall seconds)"
echo "=========================================="
cat "${LOG_DIR}/timing.tsv"
awk '
    NR==1 { base=$2 }
    NR==2 { fast=$2 }
    END {
        if (base > 0 && fast > 0) {
            printf "speedup: %.2fx (baseline %ds → fast %ds, saved %ds)\n",
                base/fast, base, fast, base-fast
        }
    }
' "${LOG_DIR}/timing.tsv"
