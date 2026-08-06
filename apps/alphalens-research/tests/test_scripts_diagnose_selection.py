"""Unit tests for the beta-estimation glue in ``scripts/diagnose_selection.py``.

The CAR maths itself lives in ``alphalens_research.diagnostics.fixed_horizon``
and is covered by ``test_fixed_horizon.py``. What is covered here is the part
the script owns: turning an anchor session into the pre-event window of closes
that the beta estimate is regressed on.
"""

from __future__ import annotations

import datetime as dt
import importlib
import unittest


def _import_script():
    return importlib.import_module("scripts.diagnose_selection")


class _FakeGrouped:
    """Stand-in for ``edge_stores.GroupedDailyCache`` over a date -> snapshot map."""

    def __init__(self, by_session: dict[dt.date, dict]):
        self._by_session = by_session

    def get(self, session: dt.date) -> dict | None:
        return self._by_session.get(session)


class TestPreEventSessions(unittest.TestCase):
    def test_window_ends_at_the_anchor_and_runs_oldest_first(self):
        mod = _import_script()

        sessions = mod._pre_event_sessions(dt.date(2026, 6, 12), 5, "XNYS")

        self.assertEqual(len(sessions), 5)
        self.assertEqual(sessions[-1], dt.date(2026, 6, 12))
        self.assertEqual(list(sessions), sorted(sessions))

    def test_weekends_are_skipped(self):
        mod = _import_script()

        # 2026-06-12 is a Friday; five sessions back reaches the prior Monday.
        sessions = mod._pre_event_sessions(dt.date(2026, 6, 12), 5, "XNYS")

        self.assertEqual(sessions[0], dt.date(2026, 6, 8))
        for s in sessions:
            self.assertLess(s.weekday(), 5)


class TestPreEventCloses(unittest.TestCase):
    def test_returns_aligned_stock_and_market_series(self):
        mod = _import_script()
        anchor = dt.date(2026, 6, 12)
        sessions = mod._pre_event_sessions(anchor, 3, "XNYS")
        grouped = _FakeGrouped(
            {
                sessions[0]: {"ABC": {"c": 10.0}, "SPY": {"c": 500.0}},
                sessions[1]: {"ABC": {"c": 11.0}, "SPY": {"c": 505.0}},
                sessions[2]: {"ABC": {"c": 12.0}, "SPY": {"c": 510.0}},
            }
        )

        stock, market = mod._pre_event_closes(grouped, anchor, "ABC", window=3, exchange="XNYS")

        self.assertEqual(stock, [10.0, 11.0, 12.0])
        self.assertEqual(market, [500.0, 505.0, 510.0])

    def test_a_session_missing_from_the_store_becomes_none_not_a_shortened_series(self):
        mod = _import_script()
        anchor = dt.date(2026, 6, 12)
        sessions = mod._pre_event_sessions(anchor, 3, "XNYS")
        grouped = _FakeGrouped(
            {
                sessions[0]: {"ABC": {"c": 10.0}, "SPY": {"c": 500.0}},
                # sessions[1] absent entirely
                sessions[2]: {"ABC": {"c": 12.0}, "SPY": {"c": 510.0}},
            }
        )

        stock, market = mod._pre_event_closes(grouped, anchor, "ABC", window=3, exchange="XNYS")

        self.assertEqual(stock, [10.0, None, 12.0])
        self.assertEqual(market, [500.0, None, 510.0])

    def test_a_ticker_absent_from_one_snapshot_only_blanks_the_stock_leg(self):
        mod = _import_script()
        anchor = dt.date(2026, 6, 12)
        sessions = mod._pre_event_sessions(anchor, 2, "XNYS")
        grouped = _FakeGrouped(
            {
                sessions[0]: {"SPY": {"c": 500.0}},
                sessions[1]: {"ABC": {"c": 12.0}, "SPY": {"c": 510.0}},
            }
        )

        stock, market = mod._pre_event_closes(grouped, anchor, "ABC", window=2, exchange="XNYS")

        self.assertEqual(stock, [None, 12.0])
        self.assertEqual(market, [500.0, 510.0])


if __name__ == "__main__":
    unittest.main()
