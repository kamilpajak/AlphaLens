# apps/alphalens-django/edge/tests/test_excess_telemetry.py
"""Unit tests for the SPY-relative telemetry aggregation (pure functions)."""

from __future__ import annotations

from edge.api.excess_telemetry import build_excess_telemetry
from edge.api.summary import N_GATE_THRESHOLD


def _row(ticker, matured, excess, *, terminal=True, plannable=True, holding=8):
    return {
        "ticker": ticker,
        "brief_date": "2026-05-27",
        "matured_at": matured,  # ISO str or datetime.date or None
        "plannable": plannable,
        "terminal": terminal,
        "market_excess_return": excess,
        "holding_days_elapsed": holding,
    }


def test_points_include_only_terminal_plannable_finite_excess_with_matured_at():
    rows = [
        _row("AAA", "2026-06-01", 0.02),
        _row("BBB", "2026-06-02", None),  # null excess -> excluded
        _row("CCC", "2026-06-02", 0.01, terminal=False),  # ongoing -> excluded
        _row("DDD", "2026-06-03", 0.03, plannable=False),  # not plannable -> excluded
        _row("EEE", None, 0.04),  # no matured_at -> excluded from points
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
        _row("AAA", "2026-06-05", -0.01),  # AAA repeats -> both AAA points flagged
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
    vals = [(i % 3 == 0) * 0.2 for i in range(30)]  # identical value multiset both ways
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
