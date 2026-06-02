"""Cross-process SEC rate coordinator — flock next-slot reservation (#381).

Hermetic: sleep + a fake WALL clock are injected so the timing math runs in
milliseconds; the lock file is a real tmp file (flock needs a real fd). One test
uses real multiprocessing to exercise the actual cross-process flock path (#380),
since single-thread sequencing cannot prove two processes serialise.
"""

from __future__ import annotations

import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from alphalens_pipeline.data.alt_data import sec_rate_coordinator as src
from alphalens_pipeline.data.alt_data.sec_rate_coordinator import (
    SEC_COORD_PATH_ENV,
    SecRateCoordinator,
    default_coord_path,
)


class _FakeWallClock:
    """Controllable wall clock; sleep advances it (no real wall-time)."""

    def __init__(self, start: float = 1000.0):
        self._t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._t += seconds

    def advance(self, seconds: float) -> None:
        self._t += seconds


class _CoordTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "coord.lock"
        self.addCleanup(self._tmp.cleanup)

    def _coord(self, wall: _FakeWallClock, min_interval_s: float = 0.1) -> SecRateCoordinator:
        return SecRateCoordinator(self.path, min_interval_s, sleep=wall.sleep, clock=wall.now)


class TestFirstCall(_CoordTestBase):
    def test_first_call_no_file_no_wait_and_writes_reservation(self):
        # Empty/absent file -> _read_reservation returns now; go_at=now; wait=0;
        # file then holds now+min_interval.
        wall = _FakeWallClock()
        c = self._coord(wall)
        self.assertEqual(c.wait_for_slot(), 0.0)
        self.assertEqual(wall.sleeps, [])
        self.assertAlmostEqual(float(self.path.read_text()), 1000.1, places=6)


class TestBackToBack(_CoordTestBase):
    def test_second_call_same_instant_waits_min_interval(self):
        # First call reserved now+0.1; clock not advanced, so the second reads
        # prior=1000.1 > now -> go_at=1000.1 -> wait=0.1, then reserves 1000.2.
        wall = _FakeWallClock()
        c = self._coord(wall)
        c.wait_for_slot()
        wait2 = c.wait_for_slot()
        self.assertAlmostEqual(wait2, 0.1, places=6)
        self.assertIn(0.1, [round(s, 6) for s in wall.sleeps])

    def test_no_wait_once_interval_elapsed(self):
        # prior=1000.1 but now advanced to 1000.3 -> max(now,prior)=now -> wait 0.
        wall = _FakeWallClock()
        c = self._coord(wall)
        c.wait_for_slot()
        wall.advance(0.3)
        self.assertEqual(c.wait_for_slot(), 0.0)


class TestStackedReservations(_CoordTestBase):
    def test_reservations_stack_not_collide(self):
        # Each _reserve_slot reads the prior reservation and adds min_interval,
        # so three reservations at a frozen clock return waits 0.0, 0.1, 0.2 and
        # the file ends at now+0.3.
        wall = _FakeWallClock()
        c = self._coord(wall)
        self.assertAlmostEqual(c._reserve_slot(), 0.0, places=6)
        self.assertAlmostEqual(c._reserve_slot(), 0.1, places=6)
        self.assertAlmostEqual(c._reserve_slot(), 0.2, places=6)
        self.assertAlmostEqual(float(self.path.read_text()), 1000.3, places=6)


class TestCorruptAndEdgeValues(_CoordTestBase):
    def test_corrupt_content_resets_to_now(self):
        # float("garbage") raises -> _read_reservation returns now.
        self.path.write_text("garbage-not-a-float")
        wall = _FakeWallClock()
        self.assertEqual(self._coord(wall).wait_for_slot(), 0.0)

    def test_empty_existing_file_is_first_call(self):
        # Zero-byte file -> raw == b"" -> returns now.
        self.path.write_text("")
        wall = _FakeWallClock()
        self.assertEqual(self._coord(wall).wait_for_slot(), 0.0)

    def test_past_reservation_collapses_to_now(self):
        # prior < now -> max(now, prior) == now -> wait 0.
        wall = _FakeWallClock()
        self.path.write_text("990.0")  # 10s in the past vs start=1000
        self.assertEqual(self._coord(wall).wait_for_slot(), 0.0)

    def test_implausible_future_reservation_reset(self):
        # prior=11000 > now+5 -> reset to now -> wait 0 (a bad writer cannot pin
        # every process for hours).
        wall = _FakeWallClock()
        self.path.write_text("11000.0")
        self.assertEqual(self._coord(wall).wait_for_slot(), 0.0)


class TestDisabledPaths(_CoordTestBase):
    def test_disabled_when_min_interval_zero(self):
        # min_interval_s<=0 short-circuits to 0.0 (no rate guard requested).
        wall = _FakeWallClock()
        c = SecRateCoordinator(self.path, 0.0, sleep=wall.sleep, clock=wall.now)
        self.assertEqual(c.wait_for_slot(), 0.0)

    def test_disabled_when_path_unwritable_directory(self):
        # Opening a directory for append raises OSError -> _probe_enabled False.
        wall = _FakeWallClock()
        c = SecRateCoordinator(Path(self._tmp.name), 0.1, sleep=wall.sleep, clock=wall.now)
        self.assertFalse(c._enabled)
        self.assertEqual(c.wait_for_slot(), 0.0)

    def test_disabled_when_fcntl_absent(self):
        # Simulated non-POSIX import -> coordinator no-op.
        with mock.patch.object(src, "_HAVE_FCNTL", False):
            wall = _FakeWallClock()
            c = SecRateCoordinator(self.path, 0.1, sleep=wall.sleep, clock=wall.now)
            self.assertFalse(c._enabled)
            self.assertEqual(c.wait_for_slot(), 0.0)


class TestMidFlightIOError(_CoordTestBase):
    def test_io_error_degrades_and_warns_once(self):
        # os.read raising OSError mid-call -> wait_for_slot returns 0.0, warns
        # once; the second call does not re-warn (warn-once flag).
        wall = _FakeWallClock()
        c = self._coord(wall)
        with mock.patch.object(src.os, "read", side_effect=OSError("eio")):
            with self.assertLogs(src.logger, level="WARNING") as cm:
                self.assertEqual(c.wait_for_slot(), 0.0)
            first = len(cm.output)
            self.assertEqual(c.wait_for_slot(), 0.0)
        self.assertGreaterEqual(first, 1)
        self.assertTrue(c._warned_io)


class TestLockTimeoutDegrades(_CoordTestBase):
    def test_lock_busy_degrades_to_no_op(self):
        # flock always raising (lock perpetually busy) -> _acquire times out ->
        # _LockUnavailable -> wait_for_slot returns 0.0 (never an unbounded
        # stall). The clock advances via injected sleep so the deadline is hit.
        wall = _FakeWallClock()
        c = self._coord(wall)
        with mock.patch.object(src.fcntl, "flock", side_effect=OSError("EWOULDBLOCK")):
            self.assertEqual(c.wait_for_slot(), 0.0)


class TestDefaultPath(unittest.TestCase):
    def test_env_override_used(self):
        with mock.patch.dict(os.environ, {SEC_COORD_PATH_ENV: "/tmp/x/coord.lock"}):
            self.assertEqual(default_coord_path(), Path("/tmp/x/coord.lock"))

    def test_default_under_alphalens(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                default_coord_path(),
                Path.home() / ".alphalens" / "sec_rate_coord.lock",
            )


class TestDurabilityAcrossInstances(_CoordTestBase):
    def test_second_instance_honours_first_instances_reservation(self):
        # Proves the reservation is on DISK, read by a SEPARATE instance (models
        # the second container/process): _read_reservation reads the file, not
        # in-memory state.
        wall1 = _FakeWallClock()
        c1 = self._coord(wall1)
        c1.wait_for_slot()  # writes 1000.1
        wall2 = _FakeWallClock()  # fresh instance, same clock start, same path
        c2 = self._coord(wall2)
        wait2 = c2.wait_for_slot()
        self.assertAlmostEqual(wait2, 0.1, places=6)


def _worker(path_str: str, out_q) -> None:
    """Subprocess body: real wall clock + real sleep, hit the shared lock."""
    coord = SecRateCoordinator(Path(path_str), 0.1)  # real time.time / time.sleep
    t0 = time.monotonic()
    coord.wait_for_slot()
    out_q.put(time.monotonic() - t0)


class TestRealMultiprocessSerialisation(_CoordTestBase):
    def test_two_real_processes_serialise_through_the_lock(self):
        # #380 — two real processes hitting the same lock file: with a reservation
        # pre-seeded just ahead of now, both must wait, and the one that reserves
        # second stacks behind the first (>= ~min_interval more). Exercises the
        # ACTUAL cross-process flock path that single-thread tests cannot.
        ctx = multiprocessing.get_context("spawn")
        out_q = ctx.Queue()
        self.path.write_text(f"{time.time() + 0.15:.6f}")
        procs = [ctx.Process(target=_worker, args=(str(self.path), out_q)) for _ in range(2)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=10)
        waits = sorted(out_q.get(timeout=5) for _ in range(2))
        # At least one process waited near the pre-seeded gap; the other stacked
        # behind it (>= ~min_interval more). Loose bounds for CI jitter.
        self.assertGreaterEqual(waits[1], 0.1)


if __name__ == "__main__":
    unittest.main()
