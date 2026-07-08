# EDGE "SPY-relative signal telemetry" panel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/edge` panel that plots per-trade excess-over-SPY as a scatter with a trailing-mean smoother and a ticker-clustered bootstrap uncertainty band — R&D telemetry over the surfaced-candidate population, never a portfolio track record.

**Architecture:** A pure Django aggregation function (`build_excess_telemetry`) rolls terminal `LadderOutcome` rows into `points[]` + a `trend[]` (smoother + CI band), N-gated. A read-only DRF endpoint (`GET /v1/edge/excess-telemetry`) serves it. The SPA draws it with a **hand-rolled inline SVG** (no chart library — Lightweight Charts requires unique ascending timestamps and cannot render multiple points per date), backed by a pure, vitest-tested geometry module. The panel lazy-fetches on first expand.

**Tech Stack:** Python 3.12 / Django / DRF (pure functions, dict rows); Svelte 5 runes / SvelteKit static adapter / inline SVG; pytest (Django) + vitest (web unit) + Playwright (web smoke).

## Global Constraints

- **No pipeline / parquet / schema change, no DB migration** — read-only aggregation over existing `edge_ladderoutcome` rows.
- **Metric is BENCHMARK-EXCESS, never raw R**; every number is **gross / pre-cost**; axis says "excess", never "return".
- **N-gate = `N_GATE_THRESHOLD` (30), imported from `edge.api.summary`** — a DISPLAY gate, never framed as statistical validity. Gate withholds the `trend` (smoother+band) only; `points` are always returned.
- **Estimand string (verbatim `metric_note`):** `"per-trade total excess over SPY across each trade's holding window; all surfaced candidates, not a user-selected portfolio; gross / pre-cost; in-sample; telemetry / exploratory only — not confirmatory."`
- **Benchmark note (verbatim):** `"SPY is a broad-market proxy and does not reflect the sector/factor exposures of these names."`
- **Constants:** `SMOOTHER_WINDOW = 20`, `BOOTSTRAP_ITERS = 500`, `BOOTSTRAP_SEED = 12345`, CI at the 2.5 / 97.5 percentiles.
- **Bootstrap resamples by TICKER cluster**, not by row (pseudo-replication must not fake precision).
- **Panel copy is telemetry-framed**, title `SPY-relative signal telemetry (not investable performance)`; keep an `in-sample` chip; show `N=… · unique tickers=…`.
- Django tests run from `apps/alphalens-django` via `uv run pytest`. Web unit tests live in `apps/web/tests/unit/**/*.test.ts` (`pnpm --filter web test:unit`, vitest node env, `$lib` aliased). Web smoke is Playwright (`pnpm --filter web test:smoke`).
- Atomic tokens (dates `YYYY-MM-DD`, `N=…`) get Tailwind `whitespace-nowrap`.
- Work in a dedicated git worktree off fresh `origin/main`; run `uv sync` in the worktree before touching Django code.

## JSON contract (produced by Task 3, consumed by Tasks 4-7)

```jsonc
{
  "benchmark": "SPY",
  "status": "accumulating" | "ok",     // "accumulating" when n_total < gate_threshold
  "gate_threshold": 30,
  "n_total": 42,                        // terminal, plannable, finite market_excess_return, with matured_at
  "n_effective": 17,                    // unique tickers among those
  "median_holding_days": 8.0,           // null when no points
  "smoother_window": 20,
  "metric_note": "…verbatim estimand…",
  "benchmark_note": "…verbatim…",
  "points": [
    { "date": "2026-06-02", "excess": -0.017, "ticker": "DFIN", "holding_days": 11, "episode_repeat": true }
  ],
  "trend": [                             // [] when status == "accumulating"
    { "date": "2026-06-02", "mean": -0.012, "lo": -0.031, "hi": 0.004 }
  ]
}
```

---

## File Structure

- **Create** `apps/alphalens-django/edge/api/excess_telemetry.py` — pure aggregation (`build_excess_telemetry` + helpers). Django-free, mirrors `summary.py`.
- **Create** `apps/alphalens-django/edge/tests/test_excess_telemetry.py` — pure-function unit tests.
- **Modify** `apps/alphalens-django/edge/api/serializers.py` — add `EdgeExcessTelemetrySerializer` (+ nested point/trend serializers).
- **Modify** `apps/alphalens-django/edge/api/views.py` — add `EdgeExcessTelemetryView`.
- **Modify** `apps/alphalens-django/edge/api/urls.py` — add the route.
- **Modify** `apps/alphalens-django/edge/tests/test_api.py` — add an endpoint integration test.
- **Create** `apps/web/src/lib/excessScatter.ts` — pure SVG geometry (scales + path strings).
- **Create** `apps/web/tests/unit/excessScatter.test.ts` — vitest geometry tests.
- **Modify** `apps/web/src/lib/types.ts` — add `EdgeExcessPoint` / `EdgeExcessTrend` / `EdgeExcessTelemetry`.
- **Modify** `apps/web/src/lib/api.ts` — add `getEdgeExcessTelemetry`.
- **Create** `apps/web/src/lib/components/ExcessScatter.svelte` — the SVG panel body.
- **Modify** `apps/web/src/routes/edge/+page.svelte` — collapsible panel with lazy fetch.
- **Create** `apps/web/tests/edge-telemetry.test.ts` — Playwright smoke (mocked endpoint).

---

## Task 1: Pure aggregation core — points + metadata + gate

**Files:**
- Create: `apps/alphalens-django/edge/api/excess_telemetry.py`
- Test: `apps/alphalens-django/edge/tests/test_excess_telemetry.py`

**Interfaces:**
- Consumes: `edge.api.summary.N_GATE_THRESHOLD` (int, 30); `_finite` semantics (copy the same finite-or-None helper).
- Produces: `build_excess_telemetry(rows: Iterable[dict]) -> dict` returning the JSON contract above **with `trend` always `[]` in this task** (the smoother/band arrive in Task 2). Also `_collect_points(rows) -> list[dict]` and module constants `SMOOTHER_WINDOW`, `METRIC_NOTE`, `BENCHMARK_NOTE`.

- [ ] **Step 1: Write the failing test**

```python
# apps/alphalens-django/edge/tests/test_excess_telemetry.py
"""Unit tests for the SPY-relative telemetry aggregation (pure functions)."""
from __future__ import annotations

from edge.api.excess_telemetry import build_excess_telemetry
from edge.api.summary import N_GATE_THRESHOLD


def _row(ticker, matured, excess, *, terminal=True, plannable=True, holding=8):
    return {
        "ticker": ticker,
        "brief_date": "2026-05-27",
        "matured_at": matured,          # ISO str or datetime.date or None
        "plannable": plannable,
        "terminal": terminal,
        "market_excess_return": excess,
        "holding_days_elapsed": holding,
    }


def test_points_include_only_terminal_plannable_finite_excess_with_matured_at():
    rows = [
        _row("AAA", "2026-06-01", 0.02),
        _row("BBB", "2026-06-02", None),               # null excess -> excluded
        _row("CCC", "2026-06-02", 0.01, terminal=False),  # ongoing -> excluded
        _row("DDD", "2026-06-03", 0.03, plannable=False),  # not plannable -> excluded
        _row("EEE", None, 0.04),                        # no matured_at -> excluded from points
    ]
    out = build_excess_telemetry(rows)
    tickers = [p["ticker"] for p in out["points"]]
    assert tickers == ["AAA"]
    assert out["n_total"] == 1
    assert out["benchmark"] == "SPY"
    assert out["gate_threshold"] == N_GATE_THRESHOLD


def test_points_sorted_by_date_then_ticker_and_episode_repeat_flagged():
    rows = [
        _row("ZZZ", "2026-06-02", 0.01),
        _row("AAA", "2026-06-02", 0.02),
        _row("AAA", "2026-06-05", -0.01),   # AAA repeats -> both AAA points flagged
    ]
    out = build_excess_telemetry(rows)
    assert [(p["ticker"], p["date"]) for p in out["points"]] == [
        ("AAA", "2026-06-02"),
        ("ZZZ", "2026-06-02"),
        ("AAA", "2026-06-05"),
    ]
    repeat = {(p["ticker"], p["date"]): p["episode_repeat"] for p in out["points"]}
    assert repeat[("AAA", "2026-06-02")] is True
    assert repeat[("AAA", "2026-06-05")] is True
    assert repeat[("ZZZ", "2026-06-02")] is False


def test_n_effective_is_unique_ticker_count_and_median_holding_days():
    rows = [
        _row("AAA", "2026-06-01", 0.01, holding=4),
        _row("AAA", "2026-06-02", 0.02, holding=8),
        _row("BBB", "2026-06-03", 0.03, holding=12),
    ]
    out = build_excess_telemetry(rows)
    assert out["n_total"] == 3
    assert out["n_effective"] == 2
    assert out["median_holding_days"] == 8.0


def test_gate_accumulating_below_threshold_trend_empty_but_points_kept():
    rows = [_row(f"T{i}", "2026-06-01", 0.01) for i in range(N_GATE_THRESHOLD - 1)]
    out = build_excess_telemetry(rows)
    assert out["status"] == "accumulating"
    assert out["trend"] == []
    assert len(out["points"]) == N_GATE_THRESHOLD - 1  # points never withheld


def test_gate_ok_at_threshold():
    rows = [_row(f"T{i}", "2026-06-01", 0.01) for i in range(N_GATE_THRESHOLD)]
    out = build_excess_telemetry(rows)
    assert out["status"] == "ok"
    assert out["n_total"] == N_GATE_THRESHOLD
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-django && uv run pytest edge/tests/test_excess_telemetry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'edge.api.excess_telemetry'`.

- [ ] **Step 3: Write minimal implementation**

```python
# apps/alphalens-django/edge/api/excess_telemetry.py
"""SPY-relative signal telemetry — the scatter + smoother + band aggregation.

Pure functions over ``LadderOutcome`` dict rows (Django-free, unit-testable),
sibling to ``summary.py``. Telemetry / exploratory ONLY:

* Per-trade BENCHMARK-EXCESS (``market_excess_return``), never raw R.
* N-gate is a DISPLAY gate (imported from ``summary``): below it the smoother +
  band (``trend``) are withheld; the raw ``points`` are always returned.
* The uncertainty band is bootstrapped by TICKER cluster so repeated episodes of
  one ticker cannot fake precision.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

from edge.api.summary import N_GATE_THRESHOLD

SMOOTHER_WINDOW = 20
METRIC_NOTE = (
    "per-trade total excess over SPY across each trade's holding window; all "
    "surfaced candidates, not a user-selected portfolio; gross / pre-cost; "
    "in-sample; telemetry / exploratory only — not confirmatory."
)
BENCHMARK_NOTE = (
    "SPY is a broad-market proxy and does not reflect the sector/factor "
    "exposures of these names."
)


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _iso(value: Any) -> str | None:
    """Coerce a matured_at (date / datetime / ISO str) to a ``YYYY-MM-DD`` string."""
    if value is None or value == "":
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _collect_points(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Terminal + plannable + finite-excess rows carrying a matured_at, sorted."""
    raw: list[dict[str, Any]] = []
    for row in rows:
        if not (row.get("plannable") and row.get("terminal")):
            continue
        excess = _finite(row.get("market_excess_return"))
        date = _iso(row.get("matured_at"))
        if excess is None or date is None:
            continue
        hd = _finite(row.get("holding_days_elapsed"))
        raw.append(
            {
                "date": date,
                "excess": excess,
                "ticker": str(row.get("ticker") or ""),
                "holding_days": None if hd is None else int(hd),
            }
        )
    counts: dict[str, int] = {}
    for p in raw:
        counts[p["ticker"]] = counts.get(p["ticker"], 0) + 1
    for p in raw:
        p["episode_repeat"] = counts[p["ticker"]] > 1
    raw.sort(key=lambda p: (p["date"], p["ticker"]))
    return raw


def build_excess_telemetry(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    points = _collect_points(rows)
    n_total = len(points)
    n_effective = len({p["ticker"] for p in points})
    median_hd = _median([p["holding_days"] for p in points if p["holding_days"] is not None])
    gated = n_total < N_GATE_THRESHOLD
    return {
        "benchmark": "SPY",
        "status": "accumulating" if gated else "ok",
        "gate_threshold": N_GATE_THRESHOLD,
        "n_total": n_total,
        "n_effective": n_effective,
        "median_holding_days": median_hd,
        "smoother_window": SMOOTHER_WINDOW,
        "metric_note": METRIC_NOTE,
        "benchmark_note": BENCHMARK_NOTE,
        "points": points,
        "trend": [],  # populated in Task 2 when not gated
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-django && uv run pytest edge/tests/test_excess_telemetry.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-django/edge/api/excess_telemetry.py apps/alphalens-django/edge/tests/test_excess_telemetry.py
git commit -m "feat(edge): pure SPY-excess telemetry aggregation (points + gate)"
```

---

## Task 2: Trailing-mean smoother + ticker-clustered bootstrap band

**Files:**
- Modify: `apps/alphalens-django/edge/api/excess_telemetry.py`
- Test: `apps/alphalens-django/edge/tests/test_excess_telemetry.py`

**Interfaces:**
- Consumes: `points` list from Task 1 (`{date, excess, ticker, ...}`), `SMOOTHER_WINDOW`, `BOOTSTRAP_ITERS`, `BOOTSTRAP_SEED`.
- Produces: `_trailing_trend(points, *, window, iters, seed) -> list[dict]` (one `{date, mean, lo, hi}` per distinct date, trailing window); wired so `build_excess_telemetry` fills `trend` when `status == "ok"`.

- [ ] **Step 1: Write the failing test**

```python
# append to apps/alphalens-django/edge/tests/test_excess_telemetry.py
from edge.api.excess_telemetry import _trailing_trend  # noqa: E402


def _pts(spec):
    # spec: list of (ticker, date, excess)
    return [{"ticker": t, "date": d, "excess": e} for (t, d, e) in spec]


def test_trailing_trend_one_point_per_distinct_date_mean_is_trailing_window():
    pts = _pts([("A", "2026-06-01", 0.0), ("B", "2026-06-02", 0.2), ("C", "2026-06-03", 0.4)])
    trend = _trailing_trend(pts, window=2, iters=50, seed=1)
    assert [t["date"] for t in trend] == ["2026-06-01", "2026-06-02", "2026-06-03"]
    # window=2 trailing means: [0.0], [0.0,0.2]->0.1, [0.2,0.4]->0.3
    assert trend[0]["mean"] == 0.0
    assert abs(trend[1]["mean"] - 0.1) < 1e-9
    assert abs(trend[2]["mean"] - 0.3) < 1e-9
    for t in trend:
        assert t["lo"] <= t["mean"] <= t["hi"]


def test_bootstrap_is_deterministic_under_fixed_seed():
    pts = _pts([(f"T{i}", "2026-06-01", (i % 5) * 0.1) for i in range(40)])
    a = _trailing_trend(pts, window=20, iters=200, seed=12345)
    b = _trailing_trend(pts, window=20, iters=200, seed=12345)
    assert a == b


def test_cluster_bootstrap_wider_band_with_fewer_unique_tickers():
    # Same N (30), same excess values, but 3 tickers vs 30 tickers. Clustering by
    # ticker means 3 clusters -> higher resample variance -> WIDER CI than 30.
    vals = [(i % 2) * 0.2 for i in range(30)]  # identical value multiset both ways
    few = _pts([(f"K{i % 3}", "2026-06-01", vals[i]) for i in range(30)])
    many = _pts([(f"U{i}", "2026-06-01", vals[i]) for i in range(30)])
    w_few = _trailing_trend(few, window=30, iters=400, seed=7)[-1]
    w_many = _trailing_trend(many, window=30, iters=400, seed=7)[-1]
    assert (w_few["hi"] - w_few["lo"]) > (w_many["hi"] - w_many["lo"])


def test_build_excess_telemetry_populates_trend_when_ok():
    rows = [
        {
            "ticker": f"T{i}",
            "brief_date": "2026-05-27",
            "matured_at": "2026-06-01",
            "plannable": True,
            "terminal": True,
            "market_excess_return": 0.01,
            "holding_days_elapsed": 8,
        }
        for i in range(N_GATE_THRESHOLD)
    ]
    out = build_excess_telemetry(rows)
    assert out["status"] == "ok"
    assert len(out["trend"]) == 1  # one distinct date
    assert out["trend"][0]["date"] == "2026-06-01"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-django && uv run pytest edge/tests/test_excess_telemetry.py -v`
Expected: FAIL — `ImportError: cannot import name '_trailing_trend'`.

- [ ] **Step 3: Write minimal implementation**

```python
# in apps/alphalens-django/edge/api/excess_telemetry.py
# add near the constants:
import random  # noqa: E402  (top-of-file import in the real edit)

BOOTSTRAP_ITERS = 500
BOOTSTRAP_SEED = 12345


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = pct / 100.0 * (len(sorted_vals) - 1)
    lo_i = int(math.floor(rank))
    hi_i = int(math.ceil(rank))
    if lo_i == hi_i:
        return sorted_vals[lo_i]
    frac = rank - lo_i
    return sorted_vals[lo_i] * (1 - frac) + sorted_vals[hi_i] * frac


def _cluster_bootstrap_ci(
    slice_points: Sequence[dict[str, Any]], *, iters: int, rng: random.Random
) -> tuple[float, float]:
    """95% CI of the slice mean, resampling by TICKER cluster (not by row).

    Groups the slice's excesses by ticker, then each iteration draws len(clusters)
    tickers WITH replacement and pools their rows. Fewer unique tickers -> higher
    between-draw variance -> wider CI (repeated episodes of one ticker cannot fake
    precision). A single-cluster slice yields a zero-width CI (undefined from one
    cluster) — the display gate + n_effective cover that degenerate case.
    """
    clusters: dict[str, list[float]] = {}
    for p in slice_points:
        clusters.setdefault(p["ticker"], []).append(p["excess"])
    keys = list(clusters.keys())
    if not keys:
        return (0.0, 0.0)
    means: list[float] = []
    for _ in range(iters):
        pooled: list[float] = []
        for _ in range(len(keys)):
            pooled.extend(clusters[keys[rng.randrange(len(keys))]])
        means.append(sum(pooled) / len(pooled))
    means.sort()
    return (_percentile(means, 2.5), _percentile(means, 97.5))


def _trailing_trend(
    points: Sequence[dict[str, Any]],
    *,
    window: int = SMOOTHER_WINDOW,
    iters: int = BOOTSTRAP_ITERS,
    seed: int = BOOTSTRAP_SEED,
) -> list[dict[str, Any]]:
    """One {date, mean, lo, hi} per distinct date — trailing-window over ordered pts.

    ``points`` MUST already be sorted by (date, ticker) (as ``_collect_points``
    returns them). For each distinct date d we take the last ``window`` points up to
    and including the last point on d, take their mean, and a ticker-clustered
    bootstrap CI. Deterministic: a single ``random.Random(seed)`` walks all dates.
    """
    rng = random.Random(seed)
    trend: list[dict[str, Any]] = []
    last_index_by_date: dict[str, int] = {}
    for i, p in enumerate(points):
        last_index_by_date[p["date"]] = i
    for date, last_i in last_index_by_date.items():
        window_slice = points[max(0, last_i - window + 1) : last_i + 1]
        excesses = [p["excess"] for p in window_slice]
        mean = sum(excesses) / len(excesses)
        lo, hi = _cluster_bootstrap_ci(window_slice, iters=iters, rng=rng)
        trend.append({"date": date, "mean": mean, "lo": lo, "hi": hi})
    return trend
```

Then wire it into `build_excess_telemetry` — replace the `"trend": []` line:

```python
        "trend": [] if gated else _trailing_trend(points),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-django && uv run pytest edge/tests/test_excess_telemetry.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-django/edge/api/excess_telemetry.py apps/alphalens-django/edge/tests/test_excess_telemetry.py
git commit -m "feat(edge): trailing-mean smoother + ticker-clustered bootstrap band"
```

---

## Task 3: Serializer + view + URL

**Files:**
- Modify: `apps/alphalens-django/edge/api/serializers.py`
- Modify: `apps/alphalens-django/edge/api/views.py`
- Modify: `apps/alphalens-django/edge/api/urls.py`
- Test: `apps/alphalens-django/edge/tests/test_api.py`

**Interfaces:**
- Consumes: `build_excess_telemetry` (Task 1/2); the `EdgeSummaryView` window-parse pattern (`_parse_window`, `_window_floor`, `_LADDER_FIELD_NAMES`).
- Produces: `GET /v1/edge/excess-telemetry?window=N` returning the JSON contract; `EdgeExcessTelemetrySerializer`.

- [ ] **Step 1: Write the failing test**

```python
# append to apps/alphalens-django/edge/tests/test_api.py
def test_excess_telemetry_endpoint_shape(tmp_path, settings):
    # Reuse the module's parquet-ingest helpers (_write_parquet/_terminal). Enough
    # terminal rows to clear the N-gate so trend is populated.
    rows = [
        _terminal(f"T{i}", excess=0.01 * ((i % 5) - 2), realized_r=0.5)
        for i in range(N_GATE_THRESHOLD)
    ]
    settings.EDGE_LADDER_STORE_DIR = str(tmp_path)
    _write_parquet(tmp_path, "2026-05-27", rows)
    rebuild_from_parquet(tmp_path)

    resp = APIClient().get("/api/v1/edge/excess-telemetry")
    assert resp.status_code == 200
    body = resp.json()
    assert body["benchmark"] == "SPY"
    assert body["status"] == "ok"
    assert body["n_total"] == N_GATE_THRESHOLD
    assert body["points"] and {"date", "excess", "ticker", "episode_repeat"} <= set(body["points"][0])
    assert body["trend"] and {"date", "mean", "lo", "hi"} <= set(body["trend"][0])
```

> NOTE: match the exact ingest/settings fixture the sibling tests in `test_api.py` already use (the file sets the store dir + calls `rebuild_from_parquet`); copy that setup verbatim rather than the sketch above if it differs.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-django && uv run pytest edge/tests/test_api.py -k excess_telemetry -v`
Expected: FAIL — 404 (route not registered).

- [ ] **Step 3: Write minimal implementation**

Add serializers to `serializers.py`:

```python
class EdgeExcessPointSerializer(serializers.Serializer):
    date = serializers.CharField()
    excess = serializers.FloatField()
    ticker = serializers.CharField(allow_blank=True)
    holding_days = serializers.IntegerField(allow_null=True)
    episode_repeat = serializers.BooleanField()


class EdgeExcessTrendSerializer(serializers.Serializer):
    date = serializers.CharField()
    mean = serializers.FloatField()
    lo = serializers.FloatField()
    hi = serializers.FloatField()


class EdgeExcessTelemetrySerializer(serializers.Serializer):
    """``/v1/edge/excess-telemetry`` — per-trade SPY-excess scatter + gated trend."""

    benchmark = serializers.CharField()
    status = serializers.ChoiceField(choices=["accumulating", "ok"])
    gate_threshold = serializers.IntegerField()
    n_total = serializers.IntegerField()
    n_effective = serializers.IntegerField()
    median_holding_days = serializers.FloatField(allow_null=True)
    smoother_window = serializers.IntegerField()
    metric_note = serializers.CharField()
    benchmark_note = serializers.CharField()
    points = EdgeExcessPointSerializer(many=True)
    trend = EdgeExcessTrendSerializer(many=True)
```

Add the view to `views.py` (import `build_excess_telemetry` + `EdgeExcessTelemetrySerializer`):

```python
class EdgeExcessTelemetryView(APIView):
    """``/v1/edge/excess-telemetry`` — per-trade SPY-excess scatter + gated trend."""

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "window",
                OpenApiTypes.INT,
                description="Calendar days back from the latest brief date (default: all).",
            ),
        ],
        responses=EdgeExcessTelemetrySerializer,
    )
    def get(self, request: Request) -> Response:
        window = _parse_window(request)
        qs = LadderOutcome.objects.all()
        floor = _window_floor(window)
        if floor is not None:
            qs = qs.filter(brief_date__gte=floor)
        rows = list(qs.values(*_LADDER_FIELD_NAMES))
        payload = build_excess_telemetry(rows)
        return Response(EdgeExcessTelemetrySerializer(payload).data)
```

Add `EdgeExcessTelemetryView` to `views.py` `__all__`, then register the route in `urls.py`:

```python
from edge.api.views import EdgeExcessTelemetryView, EdgeOutcomesView, EdgeSummaryView
# ...
    path("v1/edge/excess-telemetry", EdgeExcessTelemetryView.as_view(), name="edge-excess-telemetry"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-django && uv run pytest edge/tests/test_api.py -k excess_telemetry -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-django/edge/api/serializers.py apps/alphalens-django/edge/api/views.py apps/alphalens-django/edge/api/urls.py apps/alphalens-django/edge/tests/test_api.py
git commit -m "feat(edge): /v1/edge/excess-telemetry endpoint"
```

---

## Task 4: SPA types + pure SVG geometry module

**Files:**
- Modify: `apps/web/src/lib/types.ts`
- Create: `apps/web/src/lib/excessScatter.ts`
- Test: `apps/web/tests/unit/excessScatter.test.ts`

**Interfaces:**
- Consumes: the JSON contract (Task 3).
- Produces: TS types `EdgeExcessPoint` / `EdgeExcessTrend` / `EdgeExcessTelemetry`; pure fns `dateToMs`, `makeLinScale`, `buildScales`, `pointCircles`, `trendPolyline`, `bandPath`.

- [ ] **Step 1: Write the failing test**

```ts
// apps/web/tests/unit/excessScatter.test.ts
import { describe, it, expect } from 'vitest';
import {
	dateToMs,
	makeLinScale,
	buildScales,
	pointCircles,
	trendPolyline,
	bandPath
} from '$lib/excessScatter';
import type { EdgeExcessPoint, EdgeExcessTrend } from '$lib/types';

const BOX = { width: 400, height: 200, padLeft: 40, padRight: 10, padTop: 10, padBottom: 20 };

const POINTS: EdgeExcessPoint[] = [
	{ date: '2026-06-01', excess: -0.02, ticker: 'A', holding_days: 5, episode_repeat: false },
	{ date: '2026-06-03', excess: 0.04, ticker: 'B', holding_days: 9, episode_repeat: true }
];
const TREND: EdgeExcessTrend[] = [
	{ date: '2026-06-01', mean: -0.01, lo: -0.03, hi: 0.01 },
	{ date: '2026-06-03', mean: 0.02, lo: 0.0, hi: 0.05 }
];

describe('excessScatter geometry', () => {
	it('makeLinScale maps domain ends to range ends', () => {
		const s = makeLinScale(0, 10, 100, 200);
		expect(s(0)).toBe(100);
		expect(s(10)).toBe(200);
		expect(s(5)).toBe(150);
	});

	it('xScale is monotonic and equal dates map to equal x', () => {
		const { x } = buildScales(POINTS, TREND, BOX);
		expect(x(dateToMs('2026-06-01'))).toBeLessThan(x(dateToMs('2026-06-03')));
		expect(x(dateToMs('2026-06-01'))).toBe(x(dateToMs('2026-06-01')));
	});

	it('zeroY equals yScale(0) and sits inside the plot box', () => {
		const { y, zeroY } = buildScales(POINTS, TREND, BOX);
		expect(zeroY).toBe(y(0));
		expect(zeroY).toBeGreaterThanOrEqual(BOX.padTop);
		expect(zeroY).toBeLessThanOrEqual(BOX.height - BOX.padBottom);
	});

	it('pointCircles returns one entry per point carrying the repeat flag', () => {
		const { x, y } = buildScales(POINTS, TREND, BOX);
		const circles = pointCircles(POINTS, x, y);
		expect(circles).toHaveLength(2);
		expect(circles[1].repeat).toBe(true);
		expect(Number.isFinite(circles[0].cx)).toBe(true);
	});

	it('trendPolyline yields an M/L path with one vertex per trend point', () => {
		const { x, y } = buildScales(POINTS, TREND, BOX);
		const d = trendPolyline(TREND, x, y);
		expect(d.startsWith('M')).toBe(true);
		expect((d.match(/L/g) ?? []).length).toBe(1); // 2 points -> 1 L
	});

	it('bandPath is a closed polygon (starts M, ends Z)', () => {
		const { x, y } = buildScales(POINTS, TREND, BOX);
		const d = bandPath(TREND, x, y);
		expect(d.startsWith('M')).toBe(true);
		expect(d.trim().endsWith('Z')).toBe(true);
	});
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --filter web test:unit excessScatter`
Expected: FAIL — cannot resolve `$lib/excessScatter`.

- [ ] **Step 3: Write minimal implementation**

Append to `types.ts`:

```ts
export interface EdgeExcessPoint {
	date: string;
	excess: number;
	ticker: string;
	holding_days: number | null;
	episode_repeat: boolean;
}

export interface EdgeExcessTrend {
	date: string;
	mean: number;
	lo: number;
	hi: number;
}

export interface EdgeExcessTelemetry {
	benchmark: string;
	status: 'accumulating' | 'ok';
	gate_threshold: number;
	n_total: number;
	n_effective: number;
	median_holding_days: number | null;
	smoother_window: number;
	metric_note: string;
	benchmark_note: string;
	points: EdgeExcessPoint[];
	trend: EdgeExcessTrend[];
}
```

Create `excessScatter.ts`:

```ts
// Pure SVG geometry for the SPY-excess telemetry scatter. No DOM, no chart lib
// (Lightweight Charts needs unique ascending timestamps and cannot draw multiple
// points on one date). All statistics are computed server-side; this module only
// maps prepared numbers to pixels + path strings, so it is fully unit-testable.
import type { EdgeExcessPoint, EdgeExcessTrend } from './types';

export interface Box {
	width: number;
	height: number;
	padLeft: number;
	padRight: number;
	padTop: number;
	padBottom: number;
}

export type Scale = (v: number) => number;

export function dateToMs(iso: string): number {
	return Date.parse(iso);
}

export function makeLinScale(d0: number, d1: number, r0: number, r1: number): Scale {
	if (d1 === d0) return () => (r0 + r1) / 2; // degenerate domain -> mid-range
	const m = (r1 - r0) / (d1 - d0);
	return (v: number) => r0 + (v - d0) * m;
}

export function buildScales(
	points: EdgeExcessPoint[],
	trend: EdgeExcessTrend[],
	box: Box
): { x: Scale; y: Scale; zeroY: number } {
	const xs = points.map((p) => dateToMs(p.date));
	const trendXs = trend.map((t) => dateToMs(t.date));
	const allX = [...xs, ...trendXs];
	const ys = [
		...points.map((p) => p.excess),
		...trend.flatMap((t) => [t.lo, t.hi]),
		0 // always include the parity line in the y-domain
	];
	const xMin = allX.length ? Math.min(...allX) : 0;
	const xMax = allX.length ? Math.max(...allX) : 1;
	const yMin = ys.length ? Math.min(...ys) : -0.01;
	const yMax = ys.length ? Math.max(...ys) : 0.01;
	const x = makeLinScale(xMin, xMax, box.padLeft, box.width - box.padRight);
	// y is inverted: larger excess -> smaller pixel (top of the box).
	const y = makeLinScale(yMin, yMax, box.height - box.padBottom, box.padTop);
	return { x, y, zeroY: y(0) };
}

export function pointCircles(
	points: EdgeExcessPoint[],
	x: Scale,
	y: Scale
): { cx: number; cy: number; repeat: boolean }[] {
	return points.map((p) => ({ cx: x(dateToMs(p.date)), cy: y(p.excess), repeat: p.episode_repeat }));
}

export function trendPolyline(trend: EdgeExcessTrend[], x: Scale, y: Scale): string {
	return trend
		.map((t, i) => `${i === 0 ? 'M' : 'L'} ${x(dateToMs(t.date))} ${y(t.mean)}`)
		.join(' ');
}

export function bandPath(trend: EdgeExcessTrend[], x: Scale, y: Scale): string {
	if (trend.length === 0) return '';
	const upper = trend.map((t) => `${x(dateToMs(t.date))} ${y(t.hi)}`);
	const lower = [...trend].reverse().map((t) => `${x(dateToMs(t.date))} ${y(t.lo)}`);
	return `M ${upper.join(' L ')} L ${lower.join(' L ')} Z`;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm --filter web test:unit excessScatter`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/lib/types.ts apps/web/src/lib/excessScatter.ts apps/web/tests/unit/excessScatter.test.ts
git commit -m "feat(web): types + pure SVG geometry for SPY-excess scatter"
```

---

## Task 5: API helper `getEdgeExcessTelemetry`

**Files:**
- Modify: `apps/web/src/lib/api.ts`
- Test: `apps/web/tests/unit/getEdgeExcessTelemetry.test.ts`

**Interfaces:**
- Consumes: `apiFetch`, `EdgeExcessTelemetry`.
- Produces: `getEdgeExcessTelemetry(windowDays?: number, fetcher?: typeof fetch): Promise<EdgeExcessTelemetry | null>`.

- [ ] **Step 1: Write the failing test**

```ts
// apps/web/tests/unit/getEdgeExcessTelemetry.test.ts
import { describe, it, expect, vi } from 'vitest';
import { getEdgeExcessTelemetry } from '$lib/api';

function jsonResponse(body: unknown): Response {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
}

describe('getEdgeExcessTelemetry', () => {
	it('requests the windowed endpoint and returns the parsed body', async () => {
		const payload = { benchmark: 'SPY', status: 'ok', points: [], trend: [] };
		const fetcher = vi.fn().mockResolvedValue(jsonResponse(payload));
		const out = await getEdgeExcessTelemetry(90, fetcher as unknown as typeof fetch);
		expect(fetcher).toHaveBeenCalledOnce();
		const url = (fetcher.mock.calls[0][0] as string) ?? '';
		expect(url).toContain('/v1/edge/excess-telemetry');
		expect(url).toContain('window=90');
		expect(out?.benchmark).toBe('SPY');
	});

	it('returns null on a non-ok response', async () => {
		const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 500 }));
		const out = await getEdgeExcessTelemetry(90, fetcher as unknown as typeof fetch);
		expect(out).toBeNull();
	});
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --filter web test:unit getEdgeExcessTelemetry`
Expected: FAIL — `getEdgeExcessTelemetry` is not exported.

- [ ] **Step 3: Write minimal implementation**

Append to `api.ts` (import the type at the call site with `import('./types')` like `getEdgeChart` does):

```ts
/**
 * Fetch the SPY-relative signal telemetry (per-trade excess scatter + gated
 * smoother/band). Lazy-called from the /edge panel on first expand. Returns the
 * parsed payload or `null` on any failure (offline, 401, 5xx, malformed) so the
 * caller renders a clean empty state rather than throwing.
 */
export async function getEdgeExcessTelemetry(
	windowDays = 90,
	fetcher: typeof fetch = fetch
): Promise<import('./types').EdgeExcessTelemetry | null> {
	try {
		const res = await apiFetch(`/v1/edge/excess-telemetry?window=${windowDays}`, {}, fetcher);
		if (!res.ok) return null;
		return (await res.json()) as import('./types').EdgeExcessTelemetry;
	} catch {
		return null;
	}
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm --filter web test:unit getEdgeExcessTelemetry`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/lib/api.ts apps/web/tests/unit/getEdgeExcessTelemetry.test.ts
git commit -m "feat(web): getEdgeExcessTelemetry API helper"
```

---

## Task 6: `ExcessScatter.svelte` component

**Files:**
- Create: `apps/web/src/lib/components/ExcessScatter.svelte`

**Interfaces:**
- Consumes: `EdgeExcessTelemetry`, the `excessScatter.ts` geometry fns.
- Produces: `<ExcessScatter telemetry={…} />`. Renders an inline SVG: band `<path>` + smoother `<polyline>` when `status === 'ok'`; scatter `<circle>`s always; a dashed zero line always; the accumulating caption when gated. `data-testid="excess-scatter"` on the root and `data-testid="excess-scatter-trend"` on the smoother polyline (for the Playwright smoke).

- [ ] **Step 1: Write the component** (verified by the Task 7 Playwright smoke — no separate vitest render test because the vitest env is `node`, no DOM)

```svelte
<!-- apps/web/src/lib/components/ExcessScatter.svelte -->
<script lang="ts">
	// SPY-relative signal telemetry — per-trade excess scatter + trailing-mean
	// smoother + ticker-clustered bootstrap band. Hand-rolled inline SVG (no chart
	// lib: multiple trades share a matured_at date, which Lightweight Charts cannot
	// represent). HONESTY (load-bearing): telemetry over the surfaced-candidate
	// population, gross of cost, in-sample — NOT a portfolio track record. The
	// smoother/band are withheld below the N-gate; raw points are always shown.
	import type { EdgeExcessTelemetry } from '$lib/types';
	import { buildScales, pointCircles, trendPolyline, bandPath } from '$lib/excessScatter';

	let { telemetry }: { telemetry: EdgeExcessTelemetry } = $props();

	const BOX = { width: 640, height: 260, padLeft: 44, padRight: 12, padTop: 14, padBottom: 24 };
	const COLOR = { cyan: '#41d8ff', green: '#6dffb1', muted: '#7d8498', grid: '#1f2430' } as const;

	const showTrend = $derived(telemetry.status === 'ok' && telemetry.trend.length > 0);
	const scales = $derived(buildScales(telemetry.points, telemetry.trend, BOX));
	const circles = $derived(pointCircles(telemetry.points, scales.x, scales.y));
	const smoother = $derived(showTrend ? trendPolyline(telemetry.trend, scales.x, scales.y) : '');
	const band = $derived(showTrend ? bandPath(telemetry.trend, scales.x, scales.y) : '');
	const pctZero = $derived(`${(0).toFixed(1)}%`);
</script>

<div data-testid="excess-scatter" class="relative">
	<div class="mb-2 flex flex-wrap items-center gap-2">
		<span class="text-[10px] uppercase tracking-widest text-cyan"
			>spy-relative signal telemetry</span
		>
		<span
			class="inline-flex items-center border border-amber/40 bg-amber/15 px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-amber whitespace-nowrap"
			>in-sample</span
		>
		<span class="text-[10px] tracking-wide text-fg-muted whitespace-nowrap"
			>N={telemetry.n_total} · unique tickers={telemetry.n_effective}</span
		>
	</div>

	<svg
		viewBox={`0 0 ${BOX.width} ${BOX.height}`}
		class="w-full"
		role="img"
		aria-labelledby="excess-scatter-title excess-scatter-desc"
	>
		<title id="excess-scatter-title">Per-trade excess over SPY by exit date</title>
		<desc id="excess-scatter-desc"
			>Scatter of each surfaced candidate's excess return over SPY across its holding window, with a
			trailing-mean smoother and a bootstrap uncertainty band. Telemetry only, not a portfolio.</desc
		>

		<!-- Uncertainty band (drawn first, under everything). -->
		{#if band}
			<path d={band} fill={COLOR.cyan} fill-opacity="0.12" stroke="none" />
		{/if}

		<!-- Zero / SPY-parity reference — DASHED so it reads distinct from the solid
		     smoother without relying on colour alone. -->
		<line
			x1={BOX.padLeft}
			x2={BOX.width - BOX.padRight}
			y1={scales.zeroY}
			y2={scales.zeroY}
			stroke={COLOR.muted}
			stroke-width="1"
			stroke-dasharray="4 3"
		/>
		<text x={BOX.padLeft} y={scales.zeroY - 3} font-size="9" fill={COLOR.muted}>
			SPY parity ({pctZero})
		</text>

		<!-- Scatter points. Repeat-ticker episodes render hollow so pseudo-
		     replication is visible. -->
		{#each circles as c (c.cx + ':' + c.cy)}
			<circle
				cx={c.cx}
				cy={c.cy}
				r="2.5"
				fill={c.repeat ? 'none' : COLOR.muted}
				stroke={COLOR.muted}
				stroke-width="1"
				fill-opacity="0.7"
			/>
		{/each}

		<!-- Trailing-mean smoother (solid). -->
		{#if smoother}
			<polyline
				data-testid="excess-scatter-trend"
				points=""
				d={smoother}
				fill="none"
				stroke={COLOR.green}
				stroke-width="1.5"
			/>
			<path d={smoother} fill="none" stroke={COLOR.green} stroke-width="1.5" />
		{/if}
	</svg>

	{#if !showTrend}
		<p class="mt-1 text-[10px] tracking-wide text-fg-muted normal-case">
			trend hidden — accumulating <span class="whitespace-nowrap"
				>{telemetry.n_total}/{telemetry.gate_threshold}</span
			> matured ({telemetry.n_effective} unique tickers). Points shown are raw observations.
		</p>
	{/if}

	<p class="mt-2 text-[10px] leading-relaxed text-fg-dim normal-case">
		{telemetry.metric_note}
		{telemetry.benchmark_note}
	</p>
</div>
```

> NOTE: `<polyline>` does not take a `d` attribute — the smoother is drawn by the `<path d={smoother}>` line. The `<polyline data-testid=...>` element exists ONLY as the stable smoke-test hook; keep it with `points=""` and no visual role, OR move the `data-testid` onto the `<path>` and delete the polyline. Pick one during implementation and keep the test hook consistent with Task 7.

- [ ] **Step 2: Type-check**

Run: `pnpm --filter web check`
Expected: no new svelte-check errors.

- [ ] **Step 3: Commit**

```bash
git add apps/web/src/lib/components/ExcessScatter.svelte
git commit -m "feat(web): ExcessScatter SVG component (points + smoother + band)"
```

---

## Task 7: Wire the collapsible panel into `/edge` + Playwright smoke

**Files:**
- Modify: `apps/web/src/routes/edge/+page.svelte`
- Create: `apps/web/tests/edge-telemetry.test.ts`

**Interfaces:**
- Consumes: `getEdgeExcessTelemetry`, `ExcessScatter.svelte`.
- Produces: a collapsed panel in `/edge` that fetches once on first expand and renders `<ExcessScatter>`; a Playwright smoke asserting the panel toggles and the trend renders when the mocked endpoint returns `status: "ok"`.

- [ ] **Step 1: Write the failing smoke test**

```ts
// apps/web/tests/edge-telemetry.test.ts
import { test, expect } from '@playwright/test';

const OK_PAYLOAD = {
	benchmark: 'SPY',
	status: 'ok',
	gate_threshold: 30,
	n_total: 42,
	n_effective: 17,
	median_holding_days: 8,
	smoother_window: 20,
	metric_note: 'telemetry only — not confirmatory.',
	benchmark_note: 'SPY is a broad-market proxy.',
	points: [
		{ date: '2026-06-01', excess: -0.02, ticker: 'AAA', holding_days: 5, episode_repeat: false },
		{ date: '2026-06-03', excess: 0.04, ticker: 'BBB', holding_days: 9, episode_repeat: true }
	],
	trend: [
		{ date: '2026-06-01', mean: -0.01, lo: -0.03, hi: 0.01 },
		{ date: '2026-06-03', mean: 0.02, lo: 0.0, hi: 0.05 }
	]
};

test('excess-telemetry panel expands and renders the trend', async ({ page }) => {
	// Let the page's own loader endpoints degrade to their empty states; only the
	// telemetry endpoint needs a body for this smoke.
	await page.route('**/v1/edge/excess-telemetry**', (route) =>
		route.fulfill({ json: OK_PAYLOAD })
	);
	await page.route('**/v1/edge/summary**', (route) => route.fulfill({ json: {} }));
	await page.route('**/v1/edge/outcomes**', (route) => route.fulfill({ json: { data: [] } }));

	await page.goto('/edge');
	await page.getByRole('button', { name: /spy-relative|trend vs spy|effectiveness/i }).click();
	await expect(page.getByTestId('excess-scatter')).toBeVisible();
	await expect(page.getByTestId('excess-scatter-trend')).toBeVisible();
});
```

- [ ] **Step 2: Run smoke to verify it fails**

Run: `pnpm --filter web test edge-telemetry.test.ts`
Expected: FAIL — no such button / panel.

- [ ] **Step 3: Wire the panel into `+page.svelte`**

In the `<script>` block add the lazy-fetch state (Svelte 5 runes), mirroring the existing chart lazy-load cache:

```ts
	import { getEdgeExcessTelemetry } from '$lib/api';
	import ExcessScatter from '$lib/components/ExcessScatter.svelte';
	import type { EdgeExcessTelemetry } from '$lib/types';

	let telemetryOpen = $state(false);
	let telemetry = $state<EdgeExcessTelemetry | null>(null);
	let telemetryLoading = $state(false);
	let telemetryLoaded = $state(false);

	async function toggleTelemetry() {
		telemetryOpen = !telemetryOpen;
		if (telemetryOpen && !telemetryLoaded && !telemetryLoading) {
			telemetryLoading = true;
			telemetry = await getEdgeExcessTelemetry(90);
			telemetryLoaded = true;
			telemetryLoading = false;
		}
	}
```

In the markup, add the collapsible panel next to the outcomes table (match the surrounding panel styling — reuse `SectionPanel` if the page already uses it):

```svelte
	<section class="mt-6 border border-grid">
		<button
			type="button"
			class="flex w-full items-center justify-between px-3 py-2 text-left"
			onclick={toggleTelemetry}
			aria-expanded={telemetryOpen}
		>
			<span class="text-[11px] uppercase tracking-widest text-cyan">
				SPY-relative signal telemetry (not investable performance)
			</span>
			<span class="text-[10px] text-fg-muted">{telemetryOpen ? '−' : '+'}</span>
		</button>
		{#if telemetryOpen}
			<div class="px-3 pb-4">
				{#if telemetryLoading}
					<p class="text-[11px] text-fg-muted">loading telemetry…</p>
				{:else if telemetry}
					<ExcessScatter {telemetry} />
				{:else}
					<div class="border border-dashed border-grid-strong px-3 py-4 text-[11px] text-fg-dim">
						Telemetry unavailable right now.
					</div>
				{/if}
			</div>
		{/if}
	</section>
```

> The Playwright button matcher is `/spy-relative|trend vs spy|effectiveness/i`; the button copy above ("SPY-relative signal telemetry…") satisfies the `spy-relative` arm. Keep the two in sync.

- [ ] **Step 4: Run smoke to verify it passes**

Run: `pnpm --filter web test edge-telemetry.test.ts`
Expected: PASS.

- [ ] **Step 5: Full regression + commit**

Run: `pnpm --filter web test:unit && pnpm --filter web test:smoke && (cd apps/alphalens-django && uv run pytest)`
Expected: all green.

```bash
git add apps/web/src/routes/edge/+page.svelte apps/web/tests/edge-telemetry.test.ts
git commit -m "feat(web): SPY-relative telemetry panel on /edge (lazy-loaded)"
```

---

## Task 8: Zen pre-merge review + docs

**Files:**
- Modify: `CLAUDE.md` (own small PR/commit — VPS-backfills is unaffected, but note the new endpoint under the API surface if an endpoint list exists) — OPTIONAL, only if an endpoint catalogue exists to update.
- Move: `docs/superpowers/specs/2026-07-07-edge-effectiveness-vs-spy-chart-design.md` reference into the PR body.

- [ ] **Step 1: Push branch + open PR** (What/Why/How body; Links: none/Jira n/a; Test plan naming the pytest + vitest + Playwright files; Known issues: horizon heterogeneity un-normalized, single-cluster CI degeneracy, forward-only telemetry).

- [ ] **Step 2: Zen codereview** — `mcp__zen__codereview` with `deepseek/deepseek-v4-pro`, `thinking_mode="high"`, over the full diff (mixed Python + Svelte, one combined pass).

- [ ] **Step 3:** Apply findings as ADDITIONAL commits on the open PR (never amend+force). Wait for CI green on the latest commit. Merge.

---

## Self-Review

**Spec coverage:**
- Estimand reframe + per-trade excess → Task 1 (`METRIC_NOTE`, `_collect_points`). ✓
- Scatter + trailing smoother + ticker-clustered bootstrap band → Task 2 (`_trailing_trend`, `_cluster_bootstrap_ci`) + Task 4 (`bandPath`, `trendPolyline`) + Task 6 (SVG). ✓
- N-gate as display gate, points always shown, effective-N + median holding days → Tasks 1, 3, 6. ✓
- Endpoint `/v1/edge/excess-telemetry` → Task 3. ✓
- Hand-rolled SVG (no LWC), pure testable geometry → Tasks 4, 6 (Perplexity-validated). ✓
- Panel on `/edge`, lazy fetch, telemetry title, in-sample chip, benchmark note → Task 7 + Task 6. ✓
- Honesty guardrails (mean-not-sum handled by design; zero line dashed; no "return" wording; a11y role/title/desc) → Task 6. ✓
- Non-goals (equity curve, realized_r overlay, per-day normalization, horizon stratification, alt-benchmark) → not implemented, listed in PR Known-issues (Task 8). ✓
- Zen pre-merge review → Task 8. ✓

**Placeholder scan:** No TBD/TODO; every code step carries complete code. The two implementation choices flagged with `> NOTE` (parquet fixture parity in Task 3; polyline-vs-path smoke hook in Task 6) are explicit disambiguations with a concrete default, not deferred work. ✓

**Type consistency:** `build_excess_telemetry` / `_trailing_trend` / `_cluster_bootstrap_ci` names consistent across Tasks 1-3. JSON keys (`n_total`, `n_effective`, `median_holding_days`, `episode_repeat`, `trend[].mean/lo/hi`) identical across backend (Tasks 1-3), TS types (Task 4), component (Task 6), and smoke payload (Task 7). `getEdgeExcessTelemetry` signature identical in Tasks 5 and 7. Geometry fn names (`buildScales`, `pointCircles`, `trendPolyline`, `bandPath`, `dateToMs`, `makeLinScale`) identical in Tasks 4 and 6. ✓
