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

# Eager Buffett qualitative layer (card epic #500 / surfacing PRs #530-#535).
# Classifies moat / trend / candor / understandability + a rationale per brief
# survivor from its 10-K and stamps the seven qual columns INTO the brief parquet
# the brief stage just wrote — so the rebuild-cache ExecStartPost below carries
# them into Postgres and the card's `buffett.deep-read` drawer lights up.
#
# All five thematic stages above default to yesterday-UTC; qual-enrich takes the
# date as a positional arg, so pass the same day explicitly. Results are cached
# immutably per (date, ticker) under ~/.alphalens/buffett_qual/, so the 6×/day
# reruns re-pay DeepSeek only for names not yet classified for the day (~$2-3/day
# steady-state; a no-10-K name costs nothing — no LLM call).
#
# Best-effort under `set -e` (same posture as the VIX refresh below): the brief is
# already written, so a DeepSeek / SEC hiccup must NOT fail the build — the drawer
# simply stays absent for that name until the next run re-tries. `--scuttlebutt`
# is intentionally left OFF: it adds Perplexity cost + an UNVERIFIED footnote;
# enable it here per cost appetite.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] buffett qual-enrich"
QUAL_DATE="$(date -u -d 'yesterday' +%Y-%m-%d)"
alphalens buffett qual-enrich "$QUAL_DATE" \
    || echo "WARN: buffett qual-enrich failed for $QUAL_DATE; deep-read drawer absent until next run" >&2

# VIX regime cache refresh (Track A v2 PR-2). Best-effort: a FRED blip must
# NOT fail the whole thematic build (the brief is already written above). The
# feedback POST path degrades to a "unknown" regime stamp if this cache goes
# stale, so `|| true` under `set -e` keeps a transient FRED error non-fatal.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cache refresh-vix"
# Warn to stderr so the failure is visible in journald (StandardError=journal)
# even though the step is non-fatal. A persistently dead refresher ages the
# cache past 96h and the feedback POST path degrades to "unknown". On success
# this command emits alphalens_vix_cache_fetched_at_timestamp_seconds, which
# the AlphalensVixCache{Stale,MetricMissing} rules in
# deploy/monitoring/prometheus/rules/alphalens.yaml alert on (live rules are
# hand-synced on the VPS, outside this repo) — so a silently-dead refresher
# now pages instead of degrading stamps unnoticed.
alphalens cache refresh-vix \
    || echo "WARN: vix refresh failed; regime stamps degrade to unknown" >&2

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] DONE"
