"""Property-based tests for the /edge benchmark-excess headline metric.

``feedback/benchmark_excess.py`` computes ``market_excess_return =
forward_return − benchmark_window_return`` — the number the dashboard leads with.
Both legs are raw close-to-close over the SAME window; the subtraction must be
exact and the None-propagation total (a fudged value here silently misstates
every candidate's edge).

Two levels: the pure VWAP-anchored ``_benchmark_window_return`` (exactness vs an
independent VWAP, empty → None, scale-invariance), and ``compute_market_excess_for_row``
(the exact subtraction, constant-shift, and None/NaN propagation with a stub fetch).
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from alphalens_pipeline.feedback.bar_window import ARRIVAL_VWAP_WINDOW_MIN
from alphalens_pipeline.feedback.benchmark_excess import (
    _benchmark_window_return,
    compute_market_excess_for_row,
)
from hypothesis import given
from hypothesis import strategies as st

from .base import PropertyTestCase
from .strategies import finite_prices

UTC = dt.UTC
_ARRIVAL_OPEN = dt.datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
# Sessions used by the integration tests (all real XNYS trading days).
_BRIEF_DATE = dt.date(2026, 6, 1)
_MATURED_AT = dt.date(2026, 6, 8)
_LAST_CLOSED = dt.date(2026, 7, 1)


def _close_vol_pairs() -> st.SearchStrategy[list[tuple[float, float]]]:
    return st.lists(
        st.tuples(
            finite_prices(1.0, 1e5),
            st.floats(0.0, 1e7, allow_nan=False, allow_infinity=False, allow_subnormal=False),
        ),
        min_size=1,
        max_size=6,
    )


def _bars_in_window(pairs: list[tuple[float, float]], anchor: dt.datetime) -> list[dict[str, Any]]:
    """Bars whose ``t`` all fall inside the arrival VWAP window (<= 6 * 60s < 30min)."""
    base = int(anchor.timestamp() * 1000)
    return [{"t": base + i * 60_000, "c": c, "v": v} for i, (c, v) in enumerate(pairs)]


def _independent_window_return(pairs: list[tuple[float, float]]) -> float:
    total_vol = sum(v for _, v in pairs)
    if total_vol == 0:
        vwap = sum(c for c, _ in pairs) / len(pairs)
    else:
        vwap = sum(c * v for c, v in pairs) / total_vol
    return (pairs[-1][0] - vwap) / vwap


class TestBenchmarkWindowReturn(PropertyTestCase):
    @given(_close_vol_pairs())
    def test_equals_independent_vwap_return(self, pairs: list[tuple[float, float]]) -> None:
        bars = _bars_in_window(pairs, _ARRIVAL_OPEN)
        result = _benchmark_window_return(bars, arrival_open=_ARRIVAL_OPEN)
        assert result is not None
        self.assert_close(result, _independent_window_return(pairs), rel_tol=1e-9, abs_tol=1e-9)

    def test_empty_bars_is_none(self) -> None:
        self.assertIsNone(_benchmark_window_return([], arrival_open=_ARRIVAL_OPEN))

    def test_zero_reference_price_is_none(self) -> None:
        # All-zero closes -> VWAP reference is 0; the return must be None (no
        # divide-by-zero, no NaN leaking into the excess), not 0.0.
        zeros = _bars_in_window([(0.0, 1.0), (0.0, 2.0)], _ARRIVAL_OPEN)
        self.assertIsNone(_benchmark_window_return(zeros, arrival_open=_ARRIVAL_OPEN))

    @given(_close_vol_pairs())
    def test_no_bar_in_window_is_none(self, pairs: list[tuple[float, float]]) -> None:
        # Shift every bar an hour past the 30-min window -> nothing anchors the VWAP.
        outside = _bars_in_window(pairs, _ARRIVAL_OPEN + dt.timedelta(hours=1))
        self.assertIsNone(_benchmark_window_return(outside, arrival_open=_ARRIVAL_OPEN))

    @given(pairs=_close_vol_pairs(), k=finite_prices(1e-3, 1e3))
    def test_scale_invariant_in_price(self, pairs: list[tuple[float, float]], k: float) -> None:
        bars = _bars_in_window(pairs, _ARRIVAL_OPEN)
        scaled = [{**b, "c": b["c"] * k} for b in bars]
        r1 = _benchmark_window_return(bars, arrival_open=_ARRIVAL_OPEN)
        r2 = _benchmark_window_return(scaled, arrival_open=_ARRIVAL_OPEN)
        assert r1 is not None and r2 is not None
        self.assert_close(r1, r2, rel_tol=1e-9, abs_tol=1e-9)


class TestComputeMarketExcess(PropertyTestCase):
    @staticmethod
    def _fetch_for(pairs: list[tuple[float, float]]):
        def _fetch(_ticker: str, start: dt.datetime, _end: dt.datetime) -> list[dict[str, Any]]:
            return _bars_in_window(pairs, start)

        return _fetch

    @given(pairs=_close_vol_pairs(), fwd=finite_prices(1e-4, 1.0))
    def test_excess_is_exactly_forward_minus_benchmark(
        self, pairs: list[tuple[float, float]], fwd: float
    ) -> None:
        row = {"forward_return": fwd, "brief_date": _BRIEF_DATE, "matured_at": _MATURED_AT}
        bench, excess = compute_market_excess_for_row(
            row, bar_fetch=self._fetch_for(pairs), last_closed_session=_LAST_CLOSED
        )
        assert bench is not None and excess is not None
        # Independent oracle: the fetch stub anchors the bars at the arrival open,
        # so the benchmark leg must equal the VWAP return computed WITHOUT the
        # production window math. Pinning both the benchmark leg and the
        # subtraction against it closes the co-mutation escape hatch (a mutant in
        # the window arithmetic AND the subtraction can no longer cancel).
        expected_bench = _independent_window_return(pairs)
        self.assert_close(bench, expected_bench, rel_tol=1e-9, abs_tol=1e-12)
        self.assert_close(excess, fwd - expected_bench, rel_tol=1e-9, abs_tol=1e-12)

    @given(pairs=_close_vol_pairs(), fwd=finite_prices(1e-4, 1.0), k=finite_prices(1e-4, 1.0))
    def test_constant_shift_of_forward_shifts_excess_by_k(
        self, pairs: list[tuple[float, float]], fwd: float, k: float
    ) -> None:
        """Same bars -> same benchmark; shifting forward by k shifts excess by exactly k."""
        fetch = self._fetch_for(pairs)
        base = {"brief_date": _BRIEF_DATE, "matured_at": _MATURED_AT}
        _, e1 = compute_market_excess_for_row(
            {**base, "forward_return": fwd}, bar_fetch=fetch, last_closed_session=_LAST_CLOSED
        )
        _, e2 = compute_market_excess_for_row(
            {**base, "forward_return": fwd + k}, bar_fetch=fetch, last_closed_session=_LAST_CLOSED
        )
        assert e1 is not None and e2 is not None
        self.assert_close(e2 - e1, k, rel_tol=1e-9, abs_tol=1e-9)

    @given(pairs=_close_vol_pairs())
    def test_none_and_nan_forward_return_propagate(self, pairs: list[tuple[float, float]]) -> None:
        fetch = self._fetch_for(pairs)
        base = {"brief_date": _BRIEF_DATE, "matured_at": _MATURED_AT}
        for bad in (None, float("nan")):
            b, e = compute_market_excess_for_row(
                {**base, "forward_return": bad}, bar_fetch=fetch, last_closed_session=_LAST_CLOSED
            )
            self.assertIsNone(b)
            self.assertIsNone(e)

    @given(fwd=finite_prices(1e-4, 1.0))
    def test_empty_benchmark_fetch_propagates_none(self, fwd: float) -> None:
        row = {"forward_return": fwd, "brief_date": _BRIEF_DATE, "matured_at": _MATURED_AT}
        b, e = compute_market_excess_for_row(
            row, bar_fetch=lambda *_a: [], last_closed_session=_LAST_CLOSED
        )
        self.assertIsNone(b)
        self.assertIsNone(e)

    def test_window_constant_used_is_the_module_constant(self) -> None:
        # Guard: the in-window bar helper must sit inside ARRIVAL_VWAP_WINDOW_MIN.
        self.assertGreater(ARRIVAL_VWAP_WINDOW_MIN, 6)  # 6 bars * 60s spacing stays in window
        self.assertTrue(math.isfinite(float(ARRIVAL_VWAP_WINDOW_MIN)))
