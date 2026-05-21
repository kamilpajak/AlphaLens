#!/usr/bin/env bash
# End-to-end daily thematic pipeline. Runs inside the alphalens-pipeline
# container, called by the systemd-user timer
# `alphalens-thematic-daily.timer`.
#
# Stages, per `alphalens_cli/commands/thematic.py`:
#   1. ingest     — Polygon + RSS + EDGAR news → ~/.alphalens/thematic_news/
#   2. extract    — Gemini Flash theme extraction → ~/.alphalens/thematic_events/
#   3. map-themes — Gemini Pro beneficiary mapping + 4 verification gates
#                   → ~/.alphalens/thematic_candidates/{date}.parquet
#   4. score      — Layer 4 quant scorer → ~/.alphalens/thematic_scored/
#   5. brief      — Layer 5 brief generator → ~/.alphalens/thematic_briefs/
#   6. export     — parquet → JSON the web container serves (legacy, kept
#                   until PR 4 deprecates it)
#   7. api cache  — parquet → SQLite the api container serves over REST
#
# Exit non-zero on any stage failure so systemd marks the run as failed
# (visible via `systemctl --user status alphalens-thematic-daily`).
set -euo pipefail

# Output dir for the JSON the web container serves. Compose binds the host's
# `./web-data/` to `/web-data` inside this container.
OUT_DIR="${ALPHALENS_WEB_DATA_DIR:-/web-data}"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thematic ingest"
alphalens thematic ingest

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thematic extract"
alphalens thematic extract

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thematic map-themes"
alphalens thematic map-themes

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thematic score"
alphalens thematic score

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thematic brief"
alphalens thematic brief

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] export briefs → ${OUT_DIR}"
python /app/scripts/export_briefs_to_json.py --out "${OUT_DIR}"

# Rebuild the API SQLite cache from the same parquet set the export step
# just consumed. The api container reads ~/.alphalens/api/briefs.db in
# WAL mode, so this write is safe to do while it's serving traffic.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] api rebuild-cache"
alphalens api rebuild-cache

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] DONE"
