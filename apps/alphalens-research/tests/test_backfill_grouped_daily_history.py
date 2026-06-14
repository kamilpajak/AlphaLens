"""The grouped-daily-history backfill/top-up script — session enumeration + the
fetch loop (skip-existing, gap-on-empty/error, stop-on-entitlement, self-sizing top-up).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alphalens_pipeline.data import rs_history
from alphalens_pipeline.data.alt_data.polygon_client import PolygonError

# The script lives under scripts/ (not an importable package) — load it by path.
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_grouped_daily_history.py"
_spec = importlib.util.spec_from_file_location("backfill_grouped_daily_history", _SCRIPT)
assert _spec and _spec.loader
backfill = importlib.util.module_from_spec(_spec)
sys.modules["backfill_grouped_daily_history"] = backfill
_spec.loader.exec_module(backfill)


def _bar(c: float) -> dict:
    return {"t": 0, "o": c, "h": c, "l": c, "c": c, "v": 1, "vw": c}


class TestSessionEnumeration(unittest.TestCase):
    def test_one_time_sessions_count_and_order(self):
        sessions = backfill.one_time_sessions(dt.date(2026, 6, 15), 5, "XNYS")
        self.assertEqual(len(sessions), 5)
        self.assertEqual(sessions, sorted(sessions))  # ascending
        # ends on the last session on-or-before yesterday (2026-06-14 Sun -> Fri 6-12)
        self.assertEqual(sessions[-1], dt.date(2026, 6, 12))

    def test_topup_self_sizing_from_newest_on_disk(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rs_history.write_grouped_day_atomic(root, dt.date(2026, 6, 9), {"AAA": _bar(1.0)})
            # newest on disk = 6-09; top-up should target 6-10, 6-11, 6-12 (trading days up to yesterday)
            targets = backfill.topup_sessions(root, dt.date(2026, 6, 15), 400, "XNYS")
            self.assertTrue(all(d > dt.date(2026, 6, 9) for d in targets))
            self.assertIn(dt.date(2026, 6, 12), targets)

    def test_newest_on_disk_empty(self):
        with TemporaryDirectory() as tmp:
            self.assertIsNone(backfill._newest_on_disk(Path(tmp)))


class TestRunBackfill(unittest.TestCase):
    def test_fetch_writes_skip_existing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            d1, d2 = dt.date(2026, 6, 11), dt.date(2026, 6, 12)
            rs_history.write_grouped_day_atomic(root, d1, {"AAA": _bar(1.0)})  # pre-existing
            calls: list[dt.date] = []

            def fetch(date):
                calls.append(date)
                return {"AAA": _bar(2.0)}

            res = backfill.run_backfill(
                root, [d1, d2], grouped_fetch=fetch, stop_on_entitlement=False
            )
            self.assertEqual(res.skipped_existing, 1)  # d1 skipped, not re-fetched
            self.assertEqual(res.fetched, 1)  # d2 fetched
            self.assertEqual(calls, [d2])  # d1 never fetched (idempotent/resumable)

    def test_empty_payload_is_a_gap_not_written(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = dt.date(2026, 6, 12)
            res = backfill.run_backfill(
                root, [d], grouped_fetch=lambda x: {}, stop_on_entitlement=False
            )
            self.assertEqual(res.gaps, 1)
            self.assertEqual(res.fetched, 0)
            self.assertIsNone(rs_history.read_grouped_day(root, d))  # no empty parquet written

    def test_transient_error_is_a_gap_continues(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            d1, d2 = dt.date(2026, 6, 11), dt.date(2026, 6, 12)

            def fetch(date):
                if date == d1:
                    raise PolygonError("Polygon 500: transient")
                return {"AAA": _bar(1.0)}

            res = backfill.run_backfill(
                root, [d1, d2], grouped_fetch=fetch, stop_on_entitlement=False
            )
            self.assertEqual(res.gaps, 1)  # d1 gap
            self.assertEqual(res.fetched, 1)  # d2 still fetched (did not abort)

    def test_entitlement_cliff_stops_descent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            newest, oldest = dt.date(2026, 6, 12), dt.date(2024, 6, 12)

            def fetch(date):
                if date == oldest:
                    raise PolygonError("Polygon 403: NOT_AUTHORIZED past historical entitlements")
                return {"AAA": _bar(1.0)}

            # newest→oldest order; the old date trips the cliff and stops the loop.
            res = backfill.run_backfill(
                root, [newest, oldest], grouped_fetch=fetch, stop_on_entitlement=True
            )
            self.assertTrue(res.stopped_on_entitlement)
            self.assertEqual(res.fetched, 1)  # only the recent date

    def test_entitlement_error_classifier(self):
        self.assertTrue(backfill._is_entitlement_error(PolygonError("403: NOT_AUTHORIZED")))
        self.assertFalse(backfill._is_entitlement_error(PolygonError("500: transient")))


if __name__ == "__main__":
    unittest.main()
