#!/usr/bin/env bash
# Run all 30 cells of the P/C abnormal-volume retrospective pre-2018 battery.
#
# Layout: 2 universes × 3 sub-periods × 5 phase offsets = 30 cells.
# Each cell ~1 min wall on CPU; total ~30 min sequential, ~10 min parallel=4.
#
# Usage:
#     scripts/run_pc_retrospective_battery.sh [--parallel N]
#     scripts/run_pc_retrospective_battery.sh --parallel 4
#
# Outputs land in docs/research/pc_abnormal_retrospective_pre_2018/
# {U}_{sub}_{p}.json, ready for verdict aggregation.

set -euo pipefail

PARALLEL=1
if [[ "${1:-}" == "--parallel" ]]; then
    PARALLEL="${2:-4}"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRIVER="$REPO_ROOT/scripts/experiment_pc_retrospective.py"
PYTHON="$REPO_ROOT/.venv/bin/python"
LOG_DIR="$REPO_ROOT/docs/research/pc_abnormal_retrospective_pre_2018/logs"
mkdir -p "$LOG_DIR"

UNIVERSES=(U2 U1)
SUBPERIODS=(GFC_recovery mid_cycle_eu_debt late_cycle_china_shock)
PHASES=(0 1 2 3 4)

run_one() {
    local universe="$1"
    local subperiod="$2"
    local phase="$3"
    local log="$LOG_DIR/${universe}_${subperiod}_p${phase}.log"
    echo "[$(date +%H:%M)] Starting $universe / $subperiod / p$phase"
    "$PYTHON" "$DRIVER" \
        --universe "$universe" \
        --sub-period "$subperiod" \
        --phase-offset "$phase" \
        --log-level INFO \
        > "$log" 2>&1
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "[$(date +%H:%M)] FAILED $universe / $subperiod / p$phase (rc=$rc) — see $log"
    else
        local headline
        headline=$(grep -E "^.*\[CELL .*\]" "$log" | tail -1 || echo "no headline")
        echo "[$(date +%H:%M)] OK $universe / $subperiod / p$phase :: $headline"
    fi
    return $rc
}

export -f run_one
export DRIVER PYTHON LOG_DIR

CELLS=()
for u in "${UNIVERSES[@]}"; do
    for s in "${SUBPERIODS[@]}"; do
        for p in "${PHASES[@]}"; do
            CELLS+=("$u $s $p")
        done
    done
done

echo "Total cells: ${#CELLS[@]}"
echo "Parallelism: $PARALLEL"
echo

if [[ "$PARALLEL" -gt 1 ]]; then
    printf '%s\n' "${CELLS[@]}" | xargs -P "$PARALLEL" -L 1 bash -c 'run_one $@' _
else
    for cell in "${CELLS[@]}"; do
        run_one $cell || true
    done
fi

echo
echo "Battery complete. Outputs in:"
echo "  $REPO_ROOT/docs/research/pc_abnormal_retrospective_pre_2018/"
