# Polygon + Gemini quota for 6×/day thematic cadence

**Date:** 2026-05-30
**Status:** LOCKED — 6×/day approved
**Track:** epic #295 PR-F (issue #300)
**Author:** session 2026-05-30
**Related:** `project_exchange_agnostic_calendar_2026_05_30.md`, `feedback_zen_deepseek_first_always_2026_05_28.md`

## Question

Issue #300 proposes 4×/day cadence to fix Risk C (Saturday-PM ET news landing
in Sunday's brief). Polygon quota was the original gating concern. This memo
measures empirical per-run API usage, projects against actual plan limits,
and recommends a cadence — defaulting to the 4× from the issue but considering
6× for forward-compatibility with multi-exchange routing (XWAR / XTKS / XHKG /
XSHG) per the exchange-agnostic harness policy.

## TL;DR

- Polygon plan is **Stocks Basic (free, $0/mo)** — not Starter as the issue
  body assumed. Limit: **5 req/min, no daily/monthly cap**.
- Empirical per-run Polygon usage: **9-17 HTTP requests** (1 ingest + 6-7
  press batch + 2-9 per-ticker fallback).
- Empirical per-run Gemini usage: **~19 Pro calls + ~79 Flash calls**, zero
  quota errors over the last 30h on the live VPS.
- At 6×/day cadence, projected daily usage: 54-102 Polygon req, 114 Pro +
  474 Flash — **0.5-1.4% of any binding limit**.
- **Decision: 6×/day** (00 / 04 / 08 / 12 / 16 / 20 UTC). Maps cleanly onto
  global market session rotation; quota headroom is enormous on every axis.
- One required code change beyond the timer: `alphalens thematic ingest`
  needs `--force` in `run_thematic_day.sh` or every run after the first of
  the UTC day returns cached news (read-through cache at
  `alphalens_pipeline/thematic/sources/polygon_news.py:124`).

## Polygon plan — what we actually have

User confirmed (Polygon dashboard, screenshot 2026-05-30): **all asset classes
on `Basic` ($0/mo)**. The issue body's "Starter plan, 15 req/min, 21.6K/day"
is wrong on all three counts.

Verified via Perplexity against official Polygon KB (`intercom.help/polygonio/`
mirrored at `massive.com/knowledge-base/` — Polygon rebranded to Massive in
2026), Plans listing (`plans.apis.io/polygon-io/`), and 2026 third-party
comparisons (Flash Alpha 2026-04, apilayer 2026-05-28, brightdata blog):

| Stocks Basic (free) | Value |
|---|---|
| Per-minute rate | **5 req/min** (= 12s spacing) |
| Daily / monthly cap | **NONE** (only per-minute binds) |
| `/v2/reference/news` access | **YES** (KB: "reference data with every package, including free tiers") |
| Historical lookback | 2 years (our 30d window is well within) |
| Quote delay | 15-min delayed (irrelevant for news ingest) |

Theoretical daily ceiling = 5 × 60 × 24 = **7,200 req/day**, no enforcement at
the daily level. Our client (`polygon_client.py`) hardcodes 5 req/min as the
floor, matching the plan exactly.

## Empirical Polygon usage (1×/day baseline)

Measured from `~/.alphalens/thematic_press/_universe_*.parquet` row counts and
`~/.alphalens/thematic_news/polygon/*.parquet` (May 22-29 sample, n=8 days):

| Stage | Operation | Pages / req per run | Notes |
|---|---|---|---|
| `thematic ingest` | 1 firehose fetch `[asof, +24h)` UTC | **1 req** | 69-104 news rows / day, single page |
| `thematic map-themes` (batch) | 30-day window `_universe_*.parquet` | **6-7 req** | 5,926-6,292 rows, 7 pages × limit=1000 |
| `thematic map-themes` (per-ticker fallback) | per-candidate when ticker missing from batch | **2-9 req** | Recent days; older days saw 15-24 |
| `extract` / `score` / `brief` | no Polygon | 0 | LLM + local compute |
| **TOTAL per run (typical)** | | **9-17 req** | |

Wall time per run on Polygon: 9-17 req × 12s spacing = **108-204s** (~2-3.5 min).

## 6× cadence projection

Schedule: `OnCalendar=*-*-* 00,04,08,12,16,20:30:00 UTC` (4h spacing). Each run
at HH:30 to avoid contention with EDGAR detector (fires at every 15min mark).

| Run UTC | ET | CET | JST | Use case |
|---|---|---|---|---|
| 00:30 | 20:30 prev | 01:30 | 09:30 | **Pre-XTKS open** |
| 04:30 | 00:30 | 05:30 | 13:30 | Mid-Asia (XTKS lunch, XHKG/XSHG morning) |
| 08:30 | 04:30 | 09:30 | 17:30 | **Pre-XWAR open**, post-Asia close |
| 12:30 | 08:30 | 13:30 | 21:30 | Pre-XNYS open, XWAR lunch |
| 16:30 | 12:30 | 17:30 | 01:30 | Mid-XNYS (post-Warsaw close) |
| 20:30 | 16:30 | 21:30 | 05:30 | **Pre-XNYS close / after-hours** |

Every covered exchange gets at least one "right before open" + one "right after
close" run. Pipeline takes ~15-20 min wall time end-to-end; 4h spacing leaves
ample headroom.

### Polygon at 6×

- 9-17 req × 6 = **54-102 req/day** → 0.7-1.4% of theoretical 7,200 max
- 4h gap >> 3.5 min run length → **zero per-minute throttle risk**

### Gemini at 6×

Measured from `journalctl --user -u alphalens-thematic-build.service --since
"30 hours ago"` grep of `generativelanguage.googleapis.com/v1beta/models/`:

- Pro: 93 calls / 5 runs = **~19 calls/run**
- Flash: 397 calls / 5 runs = **~79 calls/run**
- Quota errors (real `RESOURCE_EXHAUSTED`): **0**

At 6×/day:

| Model | Calls/day | Paid Tier 1 RPM | Paid Tier 1 RPD |
|---|---|---|---|
| `gemini-3.1-pro-preview` | 114 | 1000 | 60,000 |
| `gemini-3.5-flash` | 474 | 2000 | ~10,000 |

Headroom 99%+ on both axes. Zero current errors confirms the project is on a
paid tier (free Pro tier is 5 RPM / 25 RPD — we'd already be hitting it at 1×).

## Cost projection

Per-call token counts estimated at ~5K input + 2K output (typical thematic
extract / map-themes prompt).

| Model | $/M in | $/M out | Per call | 1×/day | 6×/day | Δ |
|---|---|---|---|---|---|---|
| Gemini Pro 3.1 | $1.25 | $10 | $0.026 | $15/mo | $90/mo | +$75 |
| Gemini Flash 3.5 | $0.075 | $0.30 | $0.001 | $2/mo | $14/mo | +$12 |
| **Gemini total** | | | | **$17/mo** | **$104/mo** | **+$87** |
| DeepSeek v4-pro | $0.55 | $2.20 | $0.007 | $4/mo | $24/mo | +$20 |
| DeepSeek v4-flash | $0.07 | $0.28 | $0.001 | $2/mo | $14/mo | +$12 |
| **DeepSeek total** | | | | **$6/mo** | **$38/mo** | **+$32** |

DeepSeek swap (separate follow-up PR) saves ~$66/mo at 6× cadence. Recommended
to schedule after PR-F lands and 7-day burn-in confirms 6× behaves cleanly.

## What changes in code

### `deploy/systemd/alphalens-thematic-build.timer`

```ini
# was:
OnCalendar=*-*-* 06:30:00 UTC
# becomes:
OnCalendar=*-*-* 00,04,08,12,16,20:30:00 UTC
```

systemd `OnCalendar` syntax supports comma-separated hour lists natively. The
companion `Persistent=true` stays so missed runs at boot still fire once.

### `deploy/docker/run_thematic_day.sh`

```bash
# was:
alphalens thematic ingest
# becomes:
alphalens thematic ingest --force
```

`polygon_news.py:124` has a read-through cache keyed by `{YYYY-MM-DD}.parquet`.
Without `--force`, the second run of a UTC day returns the cached news from
the first run — defeating the entire point of 6× cadence. Polygon Basic has
no daily cap, so the cost of forced re-fetch is zero.

### Press batch cache: leave as-is (known issue)

`map-themes` batch fetch caches `_universe_{asof}.parquet`. Subsequent same-day
runs reuse it, which means later runs verify candidates against a stale press
window. Cost on Basic = zero, correctness impact = small (we miss new press
items published in the last few hours).

This is a perf-vs-freshness tradeoff. Adding `--force-press-window` flag is a
clean follow-up if weekend briefs feel stale. NOT in PR-F scope — single-axis
change.

### `alphalens-thematic-build` Prometheus staleness alert

PR #312 set the alert threshold at `> 48h` since last success. At 6×/day the
expected interval is 4h; 48h would silence-then-alert only after 12 missed
runs. Pre-emptive tighten to `> 12h` (3× cadence buffer) is in scope for
PR-F since the threshold lives in the same `deploy/monitoring/prometheus/
rules/alphalens.yaml` file.

## Side observations (not in scope)

### GDELT 429s

VPS logs show sporadic GDELT 429s on buckets `med_devices_health` and
`energy_clean` at 1×/day cadence. At 6× these will hit 6× more often.
GDELT errors are caught per-bucket and gracefully skipped (the run still
exits 0), so this is NOT a blocker. Worth monitoring post-cutover; if
multiple buckets start failing every run, add per-bucket throttle.

### Polygon client throttle

`polygon_client.py` hardcodes `rate_limit_per_min=5`. This matches Basic
exactly; no change needed. If we ever upgrade to Stocks Starter (unlimited
in-asset-class), the throttle could be relaxed to e.g. 60 req/min and runs
would finish faster. Out of PR-F scope.

### DeepSeek swap

Standalone PR-G candidate — swap `gemini-3.1-pro-preview` → `deepseek/
deepseek-v4-pro` and `gemini-3.5-flash` → `deepseek/deepseek-v4-flash` in
the extract + map-themes stages. Quality validation required (extract and
map-themes are different workloads than zen codereview). Estimated saving:
~$66/mo at 6× cadence.

## Decision summary

- **Cadence: 6×/day** at `00,04,08,12,16,20:30 UTC`
- **Code changes**: timer schedule + `--force` on ingest + staleness alert
  threshold `12h`
- **Polygon quota**: 0.7-1.4% utilization, no risk
- **Gemini quota**: <12% utilization on Pro tier, no risk
- **Cost increase**: ~$87/mo on Gemini (mitigated by future DeepSeek swap)
- **Known issue (PR body)**: press batch cache stays read-through; potential
  follow-up `--force-press-window`
