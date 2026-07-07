# Design: `/edge` "SPY-relative signal telemetry" panel

**Status:** DRAFT (approved for planning 2026-07-07; revised after Perplexity adversarial review)
**Author:** Kamil Pająk
**Scope:** one read-only Django endpoint + one SPA scatter-chart component + one `/edge` panel. No pipeline change, no parquet change, no DB migration.

## Problem / how it works today

`/edge` computes an SPY-excess metric per surfaced candidate — `market_excess_return = forward_return − benchmark_window_return`, with SPY as the hardcoded benchmark (`benchmark_excess.py:73`, `DEFAULT_BENCHMARK_TICKER = "SPY"`). Each terminal position's value is stored per-row in `~/.alphalens/population_ladders/{date}.parquet` and mirrored into Postgres `edge_ladderoutcome` (`edge/models.py:38-164`).

There is **no view of this metric over time**. `build_edge_summary` (`summary.py:347-379`) collapses everything to a single window-gated snapshot (mean / median / quantiles / hit-rate). A user cannot see how the tool's SPY-relative signal is distributed or trending as more picks mature.

## Goal (the "why")

Give an honest, at-a-glance read on: *"Across the candidates the filter surfaced, how has per-trade excess over SPY been distributed and trending as picks mature?"* — as **R&D telemetry over a candidate population**, never a portfolio track record.

AlphaLens has no deployed capital; `_build_portfolio` (`summary.py:260-275`) already deleted shared-book aggregates because "each member sizes independently … a single shared capital book never existed for this tool" (ADR 0012). The panel measures a **population statistic over surfaced candidates**, gross of cost, over all names (not a user's cherry-picked subset).

## What we measure — reframed estimand (post-review)

**Estimand (say it out loud):** *average **per-trade** total excess vs SPY, measured over each trade's own arrival→exit holding window.* This is a per-trade outcome, **not** a per-unit-time rate. A 5-day trade and a 20-day trade each count as one observation.

**Per-position quantity (already stored):** `market_excess_return = forward_return − benchmark_window_return`, terminal rows only, where `benchmark_window_return` is SPY's return over that same position's window.

### Visual form: scatter + smoother + uncertainty band

Chosen over a lone running-mean line because at the current thin sample (~50–60 matured) a single averaged line stabilizes **by construction** (grand mean over an expanding sample), not by signal — Perplexity flaw #1. The scatter keeps the raw observations honest and visible:

- **Points:** one dot per terminal position, x = `matured_at` (exit date), y = `market_excess_return`. Color/opacity may encode repeat-ticker episodes so pseudo-replication is *visible*, not hidden.
- **Smoother:** a trailing-window mean (trailing-N matured trades, N configurable, default 20) drawn as a line — a *local* read of recent effectiveness, not an inception-to-date average.
- **Band:** a bootstrap confidence band around the smoother, **resampled by ticker cluster** (not by row) so the pseudo-replication (repeat episodes of the same ticker) does not fake precision.
- **Zero reference line** = SPY parity.

## Non-goals (explicitly out of scope for MVP)

- Equity-curve / compounding view (manufactures a "fund that never existed").
- `realized_r` overlay — risk-normalised R-multiples are dimensionally incompatible with SPY's raw % on one axis (category error).
- **Per-day-normalized companion** (excess ÷ `holding_days_elapsed`) and **horizon-bucket stratification** (1–5 / 6–10 / 11–20 d) — the principled fixes for horizon heterogeneity (Perplexity flaw #2). Deferred: the honest relabel below is the MVP mitigation; normalization/stratification is the next iteration.
- Per-theme / per-sector slicing (blocked on N — needs N≥150 with a concentration guard before slicing is honest).
- Net-of-cost adjustment; alternative benchmarks (IWM / QQQ — `DEFAULT_BENCHMARK_TICKER` hardcoded to SPY).
- Any pipeline / parquet / schema change.

## Architecture

Three thin units, each independently testable:

### 1. Aggregation function (pure, Django app `edge`)
- **What it does:** given terminal `LadderOutcome` rows, return (a) the raw per-position points, (b) a trailing-window smoother series, (c) a ticker-clustered bootstrap band, (d) sample metadata including **effective N**.
- **Where:** new function next to `build_edge_summary` in `edge/api/summary.py` (e.g. `build_excess_telemetry(...)`), reusing the same gate constant, `metric_note`, and `benchmark` fields.
- **Input:** queryset of `LadderOutcome` with `terminal=True`, `market_excess_return IS NOT NULL`, ordered by `matured_at`.
- **Output dict:**
  ```
  {
    "benchmark": "SPY",
    "status": "ok" | "accumulating",       // gates the SMOOTHER+BAND, not the points
    "gate_threshold": <int>,               // UX gate, NOT a statistical-validity claim
    "n_total": <int>,                      // raw terminal rows with non-null excess
    "n_effective": <int>,                  // unique tickers (pseudo-replication proxy)
    "median_holding_days": <float>,
    "smoother_window": <int>,              // trailing-N, default 20
    "metric_note": "<verbatim honesty string>",
    "benchmark_note": "SPY = broad-market proxy; does not reflect sector/factor exposures of surfaced names.",
    "points": [ { "date": "YYYY-MM-DD", "excess": <float>, "ticker": "<T>", "holding_days": <int>, "episode_repeat": <bool> }, ... ],
    "trend": [ { "date": "YYYY-MM-DD", "mean": <float>, "lo": <float>, "hi": <float> }, ... ]  // [] when accumulating
  }
  ```
- **Gate semantics (revised):** `points` are **always returned** (raw observations are honest at any N). `status: "accumulating"` with `trend: []` when `n_total < gate_threshold` — i.e. we withhold the *smoother + band*, never the dots. `n_total`, `n_effective`, `median_holding_days` always populated so the UI renders "N/threshold · unique tickers K".
- **Threshold:** keep the existing snapshot gate value (currently 30) for consistency, but **framed as a display gate, not a CLT threshold** (Perplexity flaw #3). `n_effective` is surfaced precisely because effective N ≪ raw N under episode repetition. (Open: raising to ≥100 / ≥30 unique tickers — deferred, noted in review section.)
- **Bootstrap band:** cluster-resample by ticker (draw tickers with replacement, take all their episodes), recompute the trailing mean per resample, take percentile CI. Deterministic seed for reproducibility/testability.

### 2. Read-only API endpoint (Django REST)
- **Where:** new `APIView` modeled on `EdgeSummaryView` (`edge/api/views.py:69-154`), route `GET /v1/edge/excess-telemetry` in `edge/api/urls.py:16-24`.
- **How you use it:** SPA GETs it; response = the dict above. Same auth / CORS path as `/v1/edge/*` (CF Access cookie).
- **Depends on:** the aggregation function (unit 1) + the `LadderOutcome` ORM model.

### 3. Scatter-chart component + `/edge` panel (SPA)
- **`ExcessScatter.svelte`** — new component under `apps/web/src/lib/components/`. Renders: scatter points, a smoother line, a shaded band, and a zero reference line.
  - **Charting-lib decision (RESOLVED — hand-rolled SVG):** `LadderChart.svelte` uses Lightweight Charts v5.2, but LWC **cannot** render this scatter: every LWC series requires **unique, ascending timestamps**, and multiple positions routinely mature on the same `matured_at` (many y-values at one x). So the component is a **self-contained inline SVG** (no LWC, no new dependency): scatter `<circle>`s, a smoother `<polyline>`, a band `<path>`/`<polygon>`, and a zero reference line. All statistics (smoother, band) are computed **backend-side** (Python, unit-tested); the SVG geometry (data→pixel scales, path strings) lives in a pure companion module `excessScatter.ts` and is vitest-unit-tested. The Svelte component only maps prepared numbers to SVG coordinates — no chart lib, so no `browser`/dynamic-import dance is needed (SVG is SSR-safe), though the panel still lazy-fetches on expand.
  - **Interface:** props `{ points, trend, status, nTotal, nEffective, gateThreshold, medianHoldingDays, metricNote, benchmarkNote }`. When `status === "accumulating"`, render the scatter (points) but replace the smoother+band with an "accumulating · N/threshold · K unique tickers" state.
- **`getEdgeExcessTelemetry()`** — new helper in `apps/web/src/lib/api.ts` calling `/v1/edge/excess-telemetry`, following existing `apiFetch` + session-expiry handling.
- **`/edge` panel** — a collapsed/expandable block in `apps/web/src/routes/edge/+page.svelte`, reusing the existing lazy-fetch + per-key cache pattern (`toggleRow` / `chartCache`, lines 44-68). Placed adjacent to the outcomes table.
- **Panel title (not a track-record framing):** "SPY-relative signal telemetry (not investable performance)".
- **Caption (verbatim; atomic-token nowrap where dates appear):** *"Per-trade excess over SPY across each trade's holding window, for all surfaced candidates — not a user-selected portfolio, not a track record. In-sample, gross of cost, telemetry only. SPY is a broad-market proxy and does not reflect the sector/factor exposures of these names."* Keep the existing `in-sample` chip; add a small "N=… · unique tickers=…" line.

## Data flow

```
edge_ladderoutcome (Postgres, terminal rows, market_excess_return, ticker, holding_days_elapsed)
        │  filter terminal=True, non-null excess, order by matured_at
        ▼
build_excess_telemetry()  ──► points[] + trailing-mean smoother + ticker-clustered bootstrap band
        │                     + n_total / n_effective / median_holding_days + gate
        │  JSON
        ▼
GET /v1/edge/excess-telemetry  (EdgeExcessTelemetryView)
        │  fetch (apiFetch, CF cookie)
        ▼
getEdgeExcessTelemetry()  ──►  ExcessScatter.svelte  ──►  /edge panel
                                (points always; smoother+band only when status=="ok")
```

## Honesty guardrails (load-bearing, not cosmetic)

1. Estimand named explicitly: "per-trade total excess vs SPY over each trade's holding window" — never "return", never "per-period".
2. **Scatter of raw points**, so variability/outliers are visible; smoother is a **trailing-window** local read, never inception-to-date.
3. **Band bootstrapped by ticker cluster** — no fake precision from pseudo-replication.
4. **Effective N shown** (unique tickers + median holding days) beside raw N; the gate is a **display** gate, explicitly not a statistical-validity badge.
5. SPY rendered as the **zero reference line**, not a competing equity curve; **benchmark-limitation note** shipped inline.
6. Panel titled/captioned as **telemetry, not investable performance / not a portfolio**; `in-sample` chip + `metric_note` ("gross of cost, telemetry only, not confirmatory").

### Known caveats to surface (from `docs/research/edge_signal_attribution_2026_07_06.md`)
- Single developing in-sample window (~50 brief-dates), current baseline negative across horizons — **shown, not hidden**.
- Ticker-episode pseudo-replication (same ticker recurs within days; one ticker spans 18 days) — made **visible** via point encoding + `n_effective`, and handled in the band via cluster bootstrap. NOT de-duplicated in the points themselves (stated limitation).
- Terminal-only: NO_FILL / still-open picks are absent from the scatter.
- **Horizon heterogeneity remains** in MVP (5-day and 20-day excess share the y-axis with equal weight) — mitigated by the honest relabel; per-day normalization / horizon stratification deferred (see Non-goals).

## Perplexity adversarial review — findings & resolutions (auditable)

Reviewed 2026-07-07 (Sonar Reasoning Pro, high context). Verdict digest and disposition:

| # | Flaw | Resolution in this spec |
|---|------|-------------------------|
| 1 | Running mean-to-date is structurally misleading (flattens by construction, invites false time-series reading) | **Adopted:** dropped lone running-mean; scatter + trailing-window smoother + band instead. |
| 2 | Averaging across different holding-window lengths silently mixes units (5-day vs 20-day equal weight) | **Partially adopted (MVP):** explicit estimand relabel ("per-trade total excess over each trade's window"). **Deferred:** per-day normalization (excess ÷ `holding_days_elapsed`) + horizon-bucket stratification — flagged in Non-goals. |
| 3 | N≥30 gate is cosmetic/deceptive under pseudo-replication (effective N ≪ 30; "CLT@30" is folk-stat) | **Adopted:** gate reframed as display-only; `n_effective` (unique tickers) + median holding days surfaced; band bootstrapped by ticker cluster. **Open:** raise to ≥100 / ≥30 unique tickers (deferred decision). |
| Ethics | A "vs SPY" line with a parity baseline reads as an investable track record (gross-of-cost, all-candidates, no implementation) — near non-compliant with CFA III(D)/GIPS spirit | **Adopted:** panel retitled "SPY-relative signal telemetry (not investable performance)"; benchmark-limitation note; "all surfaced candidates, not user-selected" stated in caption. |
| Keep | Per-trade excess over real holding window; no compounding; in-sample chip; showing negative baseline; acknowledging pseudo-replication | **Kept** as designed. |

## Testing (TDD, red→green)

**Django (`apps/alphalens-django`):**
- `build_excess_telemetry` units: (a) points include every terminal non-null-excess row with correct fields; (b) non-terminal and null-excess rows excluded; (c) trailing-window smoother math on a fixture with known values; (d) gate withholds `trend` (empty) but keeps `points` + `n_total`/`n_effective` when `n_total < threshold`; (e) `n_effective` = unique-ticker count; (f) bootstrap band is deterministic under fixed seed and *widens* when episodes cluster on few tickers vs when spread across many (the pseudo-replication property).
- `EdgeExcessTelemetryView` shape test: JSON keys present, `benchmark == "SPY"`, `status` transitions ok/accumulating.

**SPA (`apps/web`):**
- vitest on `getEdgeExcessTelemetry` (mock `apiFetch`, assert URL + passthrough shape).
- `ExcessScatter.svelte`: renders points always; renders the "accumulating" state (no smoother/band) when `status === "accumulating"`; renders smoother+band otherwise. Playwright smoke on the `/edge` panel toggle.

Structure: Given-When-Then; mock external deps; edge cases (empty, exactly-at-threshold, all-one-ticker → n_effective=1 → wide band).

## Risks / rollback

- **Primary risk — misread as fund performance.** Mitigated by the six guardrails + the retitle + benchmark note. This risk is the reason equity-curve and `realized_r` variants were rejected and the running-mean line was dropped after review.
- **Secondary — false precision from pseudo-replication.** Mitigated by ticker-cluster bootstrap + visible `n_effective`.
- Pure additive change (new endpoint + new component + new panel). Rollback = revert the branch; nothing in the pipeline or DB is touched.

## Links

- Feasibility workflow synthesis + Perplexity adversarial review (this session, 2026-07-07).
- `docs/research/edge_signal_attribution_2026_07_06.md` — negative baseline + pseudo-replication caveats.
- `docs/adr/0012-decommission-paper-trading-and-broker-chain.md` — why no shared book / portfolio.
- Key code pointers: `benchmark_excess.py:73,131-206`; `edge/models.py:38-164`; `edge/api/summary.py:123,164,243-245,260-275,347-379`; `edge/api/views.py:69-154`; `edge/api/urls.py:16-24`; `apps/web/src/lib/components/LadderChart.svelte`; `apps/web/src/routes/edge/+page.svelte:44-68`.

## Implementation note (multi-session discipline)

Authored from the **primary main checkout** (memory/CLAUDE.md steward — no commits in main). Implementation runs in a dedicated `git worktree` off fresh `origin/main`; this spec is committed on the implementation branch, not in the main checkout. Per the "worktree editing pipeline/research code needs its own `uv sync`" rule, the worktree runs its own `uv sync` before touching Django/pipeline code.
