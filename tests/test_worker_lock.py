"""Tests for fcntl-based worker file-lock.

Prevents race condition when launchd `com.alphalens.watchdog.worker` fires
at StartInterval while a manual `process-queue` is running — both workers
claim candidates and hit Gemini rate limits simultaneously.

Lock uses `fcntl.flock(LOCK_EX | LOCK_NB)` — kernel auto-releases on process
death, no stale-PID cleanup needed.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class TestWorkerLock(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".lock", delete=False)
        tmp.close()
        self.lock_path = Path(tmp.name)

    def tearDown(self):
        if self.lock_path.exists():
            self.lock_path.unlink()

    def test_acquire_releases_context_manager(self):
        from alphalens.watchdog_lock import worker_lock

        with worker_lock(self.lock_path) as pid:
            self.assertEqual(pid, os.getpid())
            # After acquire, the file should contain our PID.
            self.assertEqual(self.lock_path.read_text().strip(), str(os.getpid()))

        # After release, we should be able to re-acquire.
        with worker_lock(self.lock_path):
            pass

    def test_second_acquire_in_same_process_raises(self):
        from alphalens.watchdog_lock import WorkerLockBusy, worker_lock

        with worker_lock(self.lock_path), self.assertRaises(WorkerLockBusy):
            with worker_lock(self.lock_path):
                pass

    def test_second_acquire_in_different_process_raises(self):
        """Kernel-level flock prevents subprocess from stealing the lock."""
        from alphalens.watchdog_lock import worker_lock

        repo_root = str(Path(__file__).resolve().parent.parent)
        code = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {repo_root!r})
            from pathlib import Path
            from alphalens.watchdog_lock import worker_lock, WorkerLockBusy
            try:
                with worker_lock(Path({str(self.lock_path)!r})):
                    sys.exit(0)
            except WorkerLockBusy:
                sys.exit(42)
        """)

        with worker_lock(self.lock_path):
            result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10)
            self.assertEqual(
                result.returncode,
                42,
                f"expected exit 42 (lock busy), got {result.returncode}; "
                f"stderr={result.stderr.decode()}",
            )

    def test_lock_released_on_subprocess_exit(self):
        """When the subprocess holding the lock dies, we can reacquire."""
        from alphalens.watchdog_lock import worker_lock

        repo_root = str(Path(__file__).resolve().parent.parent)
        code = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {repo_root!r})
            from pathlib import Path
            from alphalens.watchdog_lock import worker_lock
            with worker_lock(Path({str(self.lock_path)!r})):
                pass
        """)
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, f"subprocess failed: {result.stderr.decode()}")

        # Now we can acquire.
        with worker_lock(self.lock_path):
            pass

    def test_lock_file_parent_is_created(self):
        from alphalens.watchdog_lock import worker_lock

        nested = self.lock_path.parent / "subdir" / "nested.lock"
        self.assertFalse(nested.parent.exists())
        try:
            with worker_lock(nested):
                pass
            self.assertTrue(nested.parent.exists())
        finally:
            if nested.exists():
                nested.unlink()
            if nested.parent.exists():
                nested.parent.rmdir()

    def test_default_lock_path(self):
        from alphalens.watchdog_lock import default_worker_lock_path

        p = default_worker_lock_path()
        self.assertEqual(p, Path.home() / ".alphalens" / "watchdog" / "worker.lock")


if __name__ == "__main__":
    unittest.main()
