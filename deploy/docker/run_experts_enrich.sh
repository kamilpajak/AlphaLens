#!/usr/bin/env bash
# Optional expert-panel enrichment, run AFTER the brief has been published.
#
# These two stages used to sit at the end of run_thematic_day.sh, inside the
# ExecStart whose success gates both publish steps of
# alphalens-thematic-build.service. `TimeoutStartSec` covers the whole of
# ExecStart, so a timeout here killed the unit, systemd skipped every
# ExecStartPost, and a brief already written to disk never reached Postgres.
# That is what happened on 2026-09-30 and it was repaired by hand.
#
# Splitting them out does NOT buy time: ExecStart and every ExecStartPost share
# one TimeoutStartSec, so the total budget is unchanged. It buys ORDERING. The
# brief now reaches the database before any of this runs, so the same timeout
# costs the Buffett qualitative drawer rather than the day's product.
#
# Everything here is best-effort by design, which is why there is no `set -e`:
# a failure in migrate must not skip enrich, and a failure in either must not
# mark the run bad. The unit invokes this step `-`-prefixed for the same reason.
#
# A second `rebuild-cache` runs after this script, because `experts enrich`
# stamps its columns INTO the brief parquet; without it the qualitative columns
# would sit on disk until the next day's slot.
set -uo pipefail

# MANDATORY ORDERING: migrate the qual cache into version tiers BEFORE enrich.
# The cache key carries a `config_version` tier so a future rubric bump cannot
# overwrite the corpus. The one-shot move relocates the existing pre-registry
# corpus into the v0 tier so enrich SHORT-CIRCUITS on a load-hit there instead
# of recomputing every cached name with a possibly-different (LLM-
# nondeterministic) verdict. Idempotent — re-runs migrate nothing.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] experts migrate-qual-cache"
alphalens experts migrate-qual-cache \
    || echo "WARN: experts migrate-qual-cache failed; legacy names may recompute into v0 tier" >&2

# `--all` runs every registered QUAL-capable expert: today that is Buffett,
# which classifies moat / trend / candor / understandability plus a rationale
# per brief survivor from its 10-K. O'Neil is numeric-only and is stamped
# earlier, at the `score` stage, at $0.
#
# The five thematic stages default to yesterday-UTC; `experts enrich` takes the
# date as a positional arg, so pass the same day explicitly. Results are cached
# immutably per (date, ticker, scuttlebutt) under ~/.alphalens/buffett_qual/, so
# a repair-slot rerun re-pays the LLM only for names not yet classified that day
# (~$3-4/day steady-state with scuttlebutt on; a no-10-K name costs nothing).
#
# `--scuttlebutt` adds a web-grounded Perplexity context block as UNVERIFIED
# narrative and surfaces the "scuttlebutt: web-grounded, unverified" footnote in
# the drawer. Needs PERPLEXITY_API_KEY; without it the fetch degrades to "no
# context" rather than failing. The cache is keyed by the flag, so the
# scuttlebutt and plain runs never collide.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] experts enrich"
QUAL_DATE="$(date -u -d 'yesterday' +%Y-%m-%d)"
alphalens experts enrich "$QUAL_DATE" --all --scuttlebutt \
    || echo "WARN: experts enrich failed for $QUAL_DATE; deep-read drawer absent until next run" >&2

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] DONE"
