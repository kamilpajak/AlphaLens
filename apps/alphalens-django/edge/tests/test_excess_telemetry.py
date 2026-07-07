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
