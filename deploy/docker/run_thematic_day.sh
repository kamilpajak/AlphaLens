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

# Eager expert-panel qualitative layer (card epic #500 / surfacing PRs #530-#535;
# generalized to the experts registry in PR-2). For each registered qual-capable
# expert (Buffett today) it classifies moat / trend / candor / understandability +
# a rationale per brief survivor from its 10-K and stamps the eight qual columns
# INTO the brief parquet the brief stage just wrote — so the rebuild-cache
# ExecStartPost below carries them into Postgres and the card's `buffett.deep-read`
# drawer lights up. `--all` runs every registered expert (identical to Buffett-only
# today).
#
# All five thematic stages above default to yesterday-UTC; `experts enrich` takes
# the date as a positional arg, so pass the same day explicitly. Results are cached
# immutably per (date, ticker, scuttlebutt) under ~/.alphalens/buffett_qual/, so
# the 6×/day reruns re-pay the LLM only for names not yet classified for the day
# (~$3-4/day steady-state with scuttlebutt on; a no-10-K name costs nothing).
#
# `--scuttlebutt` is ON: it adds a web-grounded Perplexity context block
# (competitive position, customer/supplier concentration, management reputation)
# to the classifier as UNVERIFIED narrative, and surfaces the "scuttlebutt:
# web-grounded, unverified" footnote in the drawer. Needs PERPLEXITY_API_KEY
# (already passed into the container); if it is missing the scuttlebutt fetch
# degrades to "no context" rather than failing — the qual layer still runs.
# Cache is keyed by the flag, so the scuttlebutt and plain runs never collide.
#
# Best-effort under `set -e` (same posture as the VIX refresh below): the brief is
# already written, so a DeepSeek / Perplexity / SEC hiccup must NOT fail the build
# — the drawer simply stays absent for that name until the next run re-tries.
#
# MANDATORY ORDERING: migrate the qual cache into version tiers BEFORE enrich.
# This deploy widened the cache key with a `config_version` tier so a future rubric
# bump can never overwrite the corpus. The one-shot move relocates the existing
# pre-registry corpus into the v0 tier so enrich SHORT-CIRCUITS on a load-hit there,
# instead of recomputing every cached name into v0 with a possibly-different
# (LLM-nondeterministic) verdict. Idempotent — re-runs migrate nothing. Best-effort
# under `set -e`: a migrate hiccup must not fail the build (it costs at most one run
# of recompute-waste), so warn to stderr and continue.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] experts migrate-qual-cache"
alphalens experts migrate-qual-cache \
    || echo "WARN: experts migrate-qual-cache failed; legacy names may recompute into v0 tier" >&2

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] experts enrich"
QUAL_DATE="$(date -u -d 'yesterday' +%Y-%m-%d)"
alphalens experts enrich "$QUAL_DATE" --all --scuttlebutt \
    || echo "WARN: experts enrich failed for $QUAL_DATE; deep-read drawer absent until next run" >&2

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
