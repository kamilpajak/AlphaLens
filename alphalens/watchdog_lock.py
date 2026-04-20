"""Kernel-level file lock for the Layer 3 worker.

Prevents the race condition where `launchctl com.alphalens.watchdog.worker`
fires at its `StartInterval` while a manual `alphalens queue process` is
already running. Both workers would `claim_next()` from the same SQLite
queue and hammer the Gemini 1M-tokens/min quota in parallel, triggering
cumulative 429 `RESOURCE_EXHAUSTED` and stalling both indefinitely.

Uses `fcntl.flock(LOCK_EX | LOCK_NB)` — advisory file lock that the kernel
auto-releases when the holding process dies. No stale-PID cleanup needed.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


class WorkerLockBusy(RuntimeError):
    """Another worker is already running."""


def default_worker_lock_path() -> Path:
    return Path.home() / ".alphalens" / "watchdog" / "worker.lock"


@contextmanager
def worker_lock(lock_path: Path) -> Iterator[int]:
    """Acquire an exclusive, non-blocking flock on `lock_path`.

    Raises `WorkerLockBusy` if another process holds the lock. Releases
    automatically on context exit (or process death via kernel).

    Writes our PID into the file on acquire so ops can see who holds it.
    Yields the PID (int).
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Open with O_RDWR|O_CREAT so multiple callers share the file, each gets
    # their own fd, and flock operates on that fd.
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise WorkerLockBusy(
                    f"another worker holds {lock_path} (see contents for pid)"
                ) from None
            raise

        pid = os.getpid()
        # Truncate + write PID so anyone can cat the file to see who holds it.
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{pid}\n".encode())
        os.fsync(fd)
        logger.info("worker lock acquired: %s (pid=%d)", lock_path, pid)
        yield pid
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        logger.info("worker lock released: %s", lock_path)
