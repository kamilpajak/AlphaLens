"""Test the advisory flock arbitrates multi-process write contention.

Spawns a child process that opens the ledger inside a context manager
and sleeps; the parent then tries to acquire the same lock via a
non-blocking ``LOCK_NB`` and confirms it would have blocked.
"""

from __future__ import annotations

import fcntl
import tempfile
import time
import unittest
from multiprocessing import Event, Process
from pathlib import Path

from alphalens_pipeline.paper.ledger import open_ledger


def _hold_lock(ledger_path: Path, started, releasable) -> None:  # type: ignore[no-untyped-def]
    """Child target: open the ledger (acquires the flock) and wait."""
    with open_ledger(ledger_path) as _:
        started.set()
        # Wait until the parent finishes its non-blocking attempt.
        releasable.wait(timeout=5.0)


class TestAdvisoryLockBlocksSecondProcess(unittest.TestCase):
    def test_concurrent_writer_would_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"

            started = Event()
            releasable = Event()
            child = Process(target=_hold_lock, args=(ledger, started, releasable))
            child.start()
            try:
                self.assertTrue(
                    started.wait(timeout=5.0),
                    msg="child failed to acquire lock within 5s",
                )

                # While the child holds the lock, a non-blocking
                # acquisition from the parent must fail with BlockingIOError.
                lock_path = ledger.with_name(ledger.name + ".lock")
                with open(lock_path, "w") as fh:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                releasable.set()
                child.join(timeout=5.0)
                if child.is_alive():
                    child.terminate()


class TestSequentialAccessUnblocked(unittest.TestCase):
    def test_sequential_open_close_reacquires_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.db"

            # Five successive open/close cycles — the lock must be released
            # cleanly each time so the next iteration can acquire.
            for _ in range(5):
                with open_ledger(ledger) as conn:
                    conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
                time.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
