#!/usr/bin/env bash
# Pull data from runpod network volume (/network) into ephemeral pod disk
# (/workspace/data).
#
# Why both: zen review (2026-05-04) flagged that runpod network volumes
# are NVMe-backed but have higher latency than the local pod disk -- which
# matters for our access pattern (2784 small parquet files, random reads).
# So we rsync the working set into ephemeral once at session start;
# subsequent reads hit local fast NVMe.
#
# Idempotent: rsync skips files that are already up-to-date. First run
# transfers ~250 MB (companyfacts_parquet + ivolatility_smd + prices +
# factors). Subsequent runs are O(1) when nothing changed.

set -euo pipefail

NETWORK_DIR="${NETWORK_DIR:-/network}"
WORKSPACE_DATA="${WORKSPACE_DATA:-/workspace/data}"

if [[ ! -d "${NETWORK_DIR}" ]]; then
    echo "ERROR: network volume ${NETWORK_DIR} not mounted" >&2
    exit 1
fi

mkdir -p "${WORKSPACE_DATA}"

# Datasets the experiments read at runtime. companyfacts_parquet replaces
# the legacy companyfacts/ JSON tree post-2026-05-04 refactor.
DATASETS=(
    companyfacts_parquet
    ivolatility_smd
    prices
    factors
    pit_universe
    survivorship
    ticker_cik_map
)

echo ">>> rsync ${NETWORK_DIR} -> ${WORKSPACE_DATA}"
for d in "${DATASETS[@]}"; do
    src="${NETWORK_DIR}/${d}"
    if [[ ! -e "${src}" ]]; then
        echo "    SKIP ${d}: not present on network volume"
        continue
    fi
    dst="${WORKSPACE_DATA}/${d}"
    # --delete propagates removals from the network volume so a data
    # refresh that retracts files (rare but possible on schema migrations)
    # does not leave stale parquet on the pod's ephemeral disk.
    rsync -a --delete --info=stats2 --human-readable "${src}/" "${dst}/" \
        | tail -8 \
        | sed "s/^/    [${d}] /"
done

# Symlink so scripts that hard-code ~/.alphalens/ still resolve.
ALPHALENS_HOME="${HOME}/.alphalens"
mkdir -p "${ALPHALENS_HOME}"
for d in "${DATASETS[@]}"; do
    src="${WORKSPACE_DATA}/${d}"
    dst="${ALPHALENS_HOME}/${d}"
    if [[ -e "${src}" && ! -e "${dst}" ]]; then
        ln -s "${src}" "${dst}"
    fi
done

echo ">>> sync_in done. Total ephemeral data: $(du -sh "${WORKSPACE_DATA}" | awk '{print $1}')"
