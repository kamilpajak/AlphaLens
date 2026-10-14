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
    """The fetch loop's skip/gap/cliff semantics.

    These pass ``min_rows=0`` because their one-ticker payloads are about the
    LOOP, not about snapshot size; the size floor has its own class below.
    """

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
                root, [d1, d2], grouped_fetch=fetch, stop_on_entitlement=False, min_rows=0
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
                root, [d1, d2], grouped_fetch=fetch, stop_on_entitlement=False, min_rows=0
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
                root, [newest, oldest], grouped_fetch=fetch, stop_on_entitlement=True, min_rows=0
            )
            self.assertTrue(res.stopped_on_entitlement)
            self.assertEqual(res.fetched, 1)  # only the recent date

    def test_entitlement_error_classifier(self):
        self.assertTrue(backfill._is_entitlement_error(PolygonError("403: NOT_AUTHORIZED")))
        self.assertFalse(backfill._is_entitlement_error(PolygonError("500: transient")))


if __name__ == "__main__":
    unittest.main()


class TestThinPayloadFloor(unittest.TestCase):
    """A truncated whole-market snapshot must be refused, not frozen on disk.

    Until the top-up fired past Polygon's Eastern day boundary this state was
    UNREACHABLE: a call placed before the boundary answered 403, so the only
    payloads that ever arrived were complete ones. Moving the fire to 00:05 ET
    makes a partial end-of-day aggregate reachable, and a partial one is worse
    than a gap, because nothing re-asks for a date already on disk
    (``skipped_existing``) and ``rs_history.was_listed_on_or_before`` treats a
    present snapshot as "the ENTIRE market for that session" — so every absent
    ticker reads as ``NOT_LISTED`` and is dropped from the candidate funnel
    with no yfinance fetch (``verification/mcap_filter.classify_by_mcap``).
    """

    def test_thin_payload_is_a_gap_not_written(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = dt.date(2026, 6, 12)
            thin = {f"T{i:04d}": _bar(1.0) for i in range(10)}
            res = backfill.run_backfill(
                root, [d], grouped_fetch=lambda x: thin, stop_on_entitlement=False
            )
            self.assertEqual(res.gaps, 1)
            self.assertEqual(res.fetched, 0)
            # Not written, so the next fire re-asks for the same session.
            self.assertIsNone(rs_history.read_grouped_day(root, d))

    def test_the_floor_is_ON_by_default(self):
        # The guard must not be a parameter a caller has to remember: main()
        # wires no floor explicitly, so the default is what protects production.
        import inspect

        default = inspect.signature(backfill.run_backfill).parameters["min_rows"].default
        self.assertEqual(default, backfill.MIN_PLAUSIBLE_ROWS)
        self.assertGreater(backfill.MIN_PLAUSIBLE_ROWS, 0)

    def test_the_floor_sits_below_every_session_ever_stored(self):
        # Measured 2026-10-14 over all 523 snapshots in the production store
        # (~/.alphalens/grouped_daily_history, pyarrow num_rows): min 10579
        # (2024-09-11), p01 10613, median 11484, max 12624. A floor at or above
        # the real minimum would reject healthy sessions, which is the failure
        # mode that teaches an operator to switch the guard off.
        #
        # The half-days are the discriminating case, because a floor set from
        # full sessions alone could reject them: 2024-11-29 10662, 2024-12-24
        # 10695, 2025-07-03 11105, 2025-11-28 11604, 2025-12-24 11626. A short
        # session trades less, it does not list less.
        self.assertLess(backfill.MIN_PLAUSIBLE_ROWS, 10579)
        self.assertLess(backfill.MIN_PLAUSIBLE_ROWS, 10662)  # thinnest half-day

    def test_a_full_payload_still_writes(self):
        # Positive control: the floor must not be satisfiable only by rejecting.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = dt.date(2026, 6, 12)
            full = {f"T{i:05d}": _bar(1.0) for i in range(backfill.MIN_PLAUSIBLE_ROWS)}
            res = backfill.run_backfill(
                root, [d], grouped_fetch=lambda x: full, stop_on_entitlement=False
            )
            self.assertEqual(res.fetched, 1)
            self.assertEqual(res.gaps, 0)
            self.assertIsNotNone(rs_history.read_grouped_day(root, d))
