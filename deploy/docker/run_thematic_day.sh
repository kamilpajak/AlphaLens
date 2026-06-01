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
#
# The cache rebuild (parquet → Postgres) lives in the Django stack and is
# invoked by systemd as a separate ExecStartPost step:
#     docker compose -f deploy/docker/django-prod/docker-compose.yaml \
#         --profile maintenance run --rm rebuild-cache
# That keeps the pipeline image free of Django + Postgres deps.
#
# Exit non-zero on any stage failure so systemd marks the run as failed
# (visible via `systemctl --user status alphalens-thematic-daily`).
set -euo pipefail

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thematic ingest"
# --force: the per-UTC-day read-through cache at
# alphalens_pipeline/thematic/sources/polygon_news.py:124 would
# otherwise short-circuit every run after the first of the day. The
# 6× timer (every 4 hours UTC) needs each run to actually re-fetch
# news so the SPA sees same-day catalysts the same day. Polygon
# Stocks Basic ($0/mo) has no daily cap, only a 5 req/min rate
# limit, so forced re-fetch is free. See
# docs/research/polygon_quota_6x_per_day_2026_05_30.md §"What changes
# in code".
alphalens thematic ingest --force

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thematic extract"
alphalens thematic extract

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thematic map-themes"
alphalens thematic map-themes

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thematic score"
alphalens thematic score

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thematic brief"
alphalens thematic brief

# VIX regime cache refresh (Track A v2 PR-2). Best-effort: a FRED blip must
# NOT fail the whole thematic build (the brief is already written above). The
# feedback POST path degrades to a "unknown" regime stamp if this cache goes
# stale, so `|| true` under `set -e` keeps a transient FRED error non-fatal.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cache refresh-vix"
alphalens cache refresh-vix || echo "WARN: vix refresh failed; regime stamps degrade to unknown"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] DONE"
