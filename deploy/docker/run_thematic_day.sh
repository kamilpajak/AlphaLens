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
# NOT here: `alphalens thematic shadow-map` (the shadow-arm collection,
# docs/research/theme_shadow_arm_contract_2026_08_23.md). It draws once per
# day and takes 65-73 min — 4× this whole script — and until #1330 it sat
# between map-themes and score, so the 00:30 UTC slot timed out 12 days of
# 13 before the brief was written. It now runs in
# deploy/systemd/alphalens-thematic-shadow-map.service, activated by
# `OnSuccess=` on the build unit: after map-themes by construction, retried
# on every later slot until the day is collected, never in front of the
# product.
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
# 08:30 and 12:30 UTC repair slots still re-fetch the day's news: it feeds
# later dates' theme rollups, and never changes a published brief (#1479). Polygon
# Stocks Basic ($0/mo) has no daily cap, only a 5 req/min rate
# limit, so forced re-fetch is free. See
# docs/research/polygon_quota_6x_per_day_2026_05_30.md §"What changes
# in code".
alphalens thematic ingest --force

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thematic extract"
alphalens thematic extract

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thematic map-themes"
# map-themes, score and brief each check the date's publication state first
# (#1479, thematic/publication.py): a published brief is final, so a later slot
# neither re-maps nor regenerates it (brief only fills in late options
# telemetry), and after the arrival open a scheduled run creates no brief.
alphalens thematic map-themes

# Event lane (epic #1293). Detects insider purchase clusters for the brief date
# and writes ~/.alphalens/event_candidates/<date>.parquet, which `thematic score`
# merges into the day's candidates ONLY when ALPHALENS_EVENT_LANE=1 (the accrual
# switch, set in /etc/alphalens/env after #1297 is deployed — never before, or the
# thematic /edge aggregates would absorb the lane). Gated here as well so an
# unflagged build never touches EDGAR/yfinance for it. Best-effort: a candidate
# SOURCE must never fail the pipeline that produces the thematic product; when
# it fails, `score` merges the previous slot's parquet or nothing.
if [ "${ALPHALENS_EVENT_LANE:-0}" = "1" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] events insider-clusters (event lane, best-effort)"
    alphalens events insider-clusters \
        || echo "WARN: events insider-clusters failed; event lane absent or stale this slot" >&2
fi

# VIX refresh (Track A v2 PR-2), moved AHEAD of `score` by #1524. It used to run
# last, after brief, which meant that even a working refresher was too late to
# help the same run's market_state stamp — and it wrote to a throwaway temp dir,
# so it never helped that stamp at all. It now refreshes the SHARED FRED parquet,
# so this one fetch serves both the feedback POST path's JSON cache and the
# market_state VIX read in `score` below.
#
# Best-effort on purpose: a FRED blip must not fail the build. If this step dies,
# `score` asks the client for a current series itself and stamps market_state
# 'unknown' rather than a stale value, and the feedback POST path degrades to
# 'unknown' once the JSON cache passes 96h.
#
# Warn to stderr so the failure is visible in journald (StandardError=journal).
# On success this emits alphalens_vix_cache_fetched_at_timestamp_seconds, which
# the AlphalensVixCache{Stale,MetricMissing} rules in
# deploy/monitoring/prometheus/rules/alphalens.yaml alert on (those rules reach
# the VPS on their own, within an hour of a merge; THIS script does not).
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cache refresh-vix"
alphalens cache refresh-vix \
    || echo "WARN: vix refresh failed; market_state and regime stamps degrade to unknown" >&2

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thematic score"
alphalens thematic score

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thematic brief"
alphalens thematic brief

# The expert-panel qualitative layer used to run here, after `brief`. It moved
# to deploy/docker/run_experts_enrich.sh, which the systemd unit invokes as an
# ExecStartPost AFTER the publish chain — see that script's header for why. The
# short version: it is optional work, and while it lived inside this script a
# timeout in it skipped the publish steps and lost an already-written brief.

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] DONE"
