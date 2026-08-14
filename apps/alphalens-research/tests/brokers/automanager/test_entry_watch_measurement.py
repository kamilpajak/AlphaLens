"""Measurement stamps on entry-trail terminal journal lines (PR-T1d).

Every terminal line (``fired``/``suspended``/``expired``/``cancelled``) carries a
``measurement`` blob — the offline exec_quality join reads it later to compute
concession / implied ΔR / fill-rate loss (memo §5 Measurement, first-class). The
order id is NULL in the dry run (no order placed); ``entry_mode`` is the cohort
tag so fills never pool across execution policies (T8). The ``touched`` line
additionally carries the touch price/ts inline for offline-join durability.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import entry_trails

# Shared hermetic fixtures live next to the T1c wiring tests.
from tests.brokers.automanager.test_entry_watch_wiring import (
    _FakeFeed,
    _journal,
    _lines,
    _watch_deps,
)

_ENV = entry_trails.ENTRY_TRAIL_BPS_ENV


def _seed_watch(
    path: Path,
    *,
    crid: str = "KO-2026-07-20-entry-t0",
    limit: float = 10.0,
    next_tier_limit: float | None = None,
    window_end: str = "2099-01-01T21:00:00+00:00",
) -> None:
    entry_trails.append_entry_trail_line(
        {
            "kind": entry_trails.KIND_WATCH_OPEN,
            "crid": crid,
            "limit": limit,
            "qty": 100.0,
            "d_bps": 50,
            "window_end": window_end,
            "fx_rate": None,
            "uic": 307,
            "ticker": "KO",
            "exchange_mic": "XNYS",
            "next_tier_limit": next_tier_limit,
            "pick_key": "KO:2026-07-20",
            "entry_mode": "entry-trail-native-d50-testcfg",
        }
    )


def _run(deps: cl.LoopDeps, price: float | None, feed: dict[int, float | None]) -> None:
    feed[307] = price
    with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
        cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())


def _terminal(path: Path, kind: str) -> dict:
    matches = [line for line in _lines(path) if line["kind"] == kind]
    assert len(matches) == 1, f"expected exactly one {kind} line, got {len(matches)}"
    return matches[0]


class TestNativeModeNoFabricatedFire(unittest.TestCase):
    def test_native_touch_tracking_writes_no_fired_line(self) -> None:
        # PR-T2b: with an object() broker the arm is a no-op (not trailing-
        # capable), so a bounce that WOULD have fired in the dry run leaves NO
        # fabricated `fired` line — the server, not the bot, is the fire event.
        # The touch/trough are still journaled (measurement source).
        path = _journal(self)
        _seed_watch(path)
        prices: dict[int, float | None] = {}
        deps = _watch_deps(_FakeFeed(prices), [])
        _run(deps, 10.0, prices)  # touch @ limit, trough=10.0
        _run(deps, 9.90, prices)  # new low, trough=9.90
        _run(deps, 9.95, prices)  # bounce — would have fired in dry run

        kinds = [line["kind"] for line in _lines(path)]
        self.assertIn(entry_trails.KIND_TOUCHED, kinds)
        self.assertIn(entry_trails.KIND_TROUGH, kinds)
        self.assertNotIn(entry_trails.KIND_FIRED, kinds)
        self.assertNotIn(entry_trails.KIND_TRAIL_ARMED, kinds)


class TestTouchedLineCarriesTouchPrice(unittest.TestCase):
    def test_touched_line_journals_touch_price_and_ts(self) -> None:
        path = _journal(self)
        _seed_watch(path)
        prices: dict[int, float | None] = {}
        deps = _watch_deps(_FakeFeed(prices), [])
        _run(deps, 9.80, prices)  # <= limit -> touched at 9.80
        touched = _terminal(path, entry_trails.KIND_TOUCHED)
        self.assertEqual(touched["touch_price"], 9.80)
        self.assertIn("touch_ts", touched)


class TestSuspendedMeasurementBlob(unittest.TestCase):
    def test_suspend_terminal_carries_measurement(self) -> None:
        path = _journal(self)
        _seed_watch(path, next_tier_limit=9.5)
        prices: dict[int, float | None] = {}
        deps = _watch_deps(_FakeFeed(prices), [])
        _run(deps, 10.0, prices)  # touched, trough=10.0
        _run(deps, 9.40, prices)  # below next tier 9.5 -> suspended
        blob = _terminal(path, entry_trails.KIND_SUSPENDED)["measurement"]
        self.assertEqual(blob["tier_limit"], 10.0)
        self.assertEqual(blob["final_trough"], 9.40)
        self.assertIsNone(blob["order_id"])


class TestExpiredMeasurementBlob(unittest.TestCase):
    def test_expiry_terminal_carries_measurement(self) -> None:
        path = _journal(self)
        _seed_watch(path, window_end="2000-01-01T00:00:00+00:00")  # already past
        prices: dict[int, float | None] = {307: None}
        deps = _watch_deps(_FakeFeed(prices), [])
        with mock.patch.dict("os.environ", {_ENV: "50"}, clear=True):
            cl._run_entry_watch_pass(deps, kill=False, report=cl.TickReport())
        blob = _terminal(path, entry_trails.KIND_EXPIRED)["measurement"]
        self.assertEqual(blob["tier_limit"], 10.0)
        self.assertIsNone(blob["order_id"])
        self.assertEqual(blob["entry_mode"], "entry-trail-native-d50-testcfg")


if __name__ == "__main__":
    unittest.main()
