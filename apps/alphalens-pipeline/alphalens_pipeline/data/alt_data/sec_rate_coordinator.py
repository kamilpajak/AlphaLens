"""Cross-process SEC EDGAR rate coordinator (file-lock next-slot timestamp).

SEC's 10 req/s fair-access limit is enforced PER-IP. On the VPS three processes
share one residential IP (``edgar-detect`` every 15 min, ``thematic-build``
ingest, the companyfacts preload burst), each in its own container with its own
in-process throttle, so the aggregate exceeds 10 req/s and SEC returns a
traffic-threshold 403 (valid-UA, not 429, not auth). See epic #379.

This serialises the *next-allowed-call* wall timestamp through one small file
under ``~/.alphalens/`` (shared via the HOME bind-mount). The flock is held ONLY
for read-reserve-write (microseconds) — NEVER across the sleep — so processes
stack reservations and wait concurrently instead of serialising on backoff.

Design decisions driven by adversarial review (#381):
- NO ``fsync`` per call: durability across container restart is not needed — a
  stale past reservation collapses to ``now`` via ``max(now, prior)``. flock +
  page cache already guarantees visibility to concurrent live processes. A
  per-call fsync would add iowait against the same disk that holds thematic
  parquet / feedback.db.
- BOUNDED ``LOCK_NB`` spin, not blocking ``LOCK_EX``: a live-but-stuck holder
  (overlayfs/NFS stall, SIGSTOP) must NOT stall all SEC traffic. If the lock
  cannot be acquired within ``_LOCK_ACQUIRE_TIMEOUT_S`` the call degrades to
  no-op (returns 0.0) — never worse than the 403 it prevents.
- Wall clock in the file (comparable across processes); the client's own
  in-process smoothing stays on monotonic.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX is out of scope
    _HAVE_FCNTL = False

SEC_COORD_PATH_ENV = "SEC_RATE_COORD_PATH"

# A reservation farther in the future than this (relative to now) is treated as
# corrupt / clock-jumped and reset to "no prior call". Caps worst-case wait if a
# bad writer ever persists a garbage future value. Generous vs the realistic
# stacked-reservation depth (3 AlphaLens processes * 0.1s = 0.3s).
_MAX_PLAUSIBLE_RESERVATION_S = 5.0

# Bounded LOCK_NB spin: total time we try to acquire before degrading to no-op.
_LOCK_ACQUIRE_TIMEOUT_S = 1.0
_LOCK_RETRY_SLEEP_S = 0.005


def default_coord_path() -> Path:
    """Resolve the shared coordinator file path.

    ``SEC_RATE_COORD_PATH`` overrides (tests, ops); default lives next to the
    other ``~/.alphalens`` runtime state so every container bind-mounting HOME
    sees the same inode.
    """
    override = os.environ.get(SEC_COORD_PATH_ENV)
    if override:
        return Path(override)
    return Path.home() / ".alphalens" / "sec_rate_coord.lock"


class _LockUnavailableError(RuntimeError):
    """Could not acquire the advisory lock within the bounded spin."""


class SecRateCoordinator:
    """File-lock cross-process minimum-interval gate for SEC requests.

    One instance per default :class:`SecEdgarClient`. ``min_interval_s`` should
    match the client throttle interval (``1 / rate_limit_per_sec``). ``sleep`` /
    ``clock`` are injected so tests run hermetically in milliseconds. ``clock``
    MUST be a wall clock (shared across processes), never ``time.monotonic``.
    """

    def __init__(
        self,
        path: Path,
        min_interval_s: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,  # WALL clock, shared across procs
    ) -> None:
        self._path = path
        self._min_interval_s = max(min_interval_s, 0.0)
        self._sleep = sleep
        self._clock = clock
        self._enabled = self._probe_enabled()
        self._warned_io = False

    def _probe_enabled(self) -> bool:
        if not _HAVE_FCNTL:
            return False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a"):  # create if absent; confirm writability
                pass
            return True
        except OSError as exc:
            logger.warning(
                "sec rate coordinator disabled — lock path %s not writable: %s; "
                "falling back to per-process throttle only",
                self._path,
                exc,
            )
            return False

    def wait_for_slot(self) -> float:
        """Block until this process may issue the next SEC request.

        Returns seconds slept (0.0 if no wait / disabled / degraded). The flock
        is held only for read-reserve-write, never across the sleep.
        """
        if not self._enabled or self._min_interval_s <= 0.0:
            return 0.0
        try:
            wait_s = self._reserve_slot()
        except (_LockUnavailableError, OSError) as exc:
            if not self._warned_io:
                logger.warning(
                    "sec rate coordinator degraded (%s); skipping cross-process gate for this call",
                    exc,
                )
                self._warned_io = True
            return 0.0
        if wait_s > 0.0:
            self._sleep(wait_s)
        return wait_s

    def _acquire(self, fd: int) -> None:
        """Bounded non-blocking flock acquire.

        Raises :class:`_LockUnavailableError` on timeout so a stuck holder degrades
        to no-op instead of an unbounded stall.
        """
        deadline = self._clock() + _LOCK_ACQUIRE_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if self._clock() >= deadline:
                    raise _LockUnavailableError(
                        f"lock {self._path} busy > {_LOCK_ACQUIRE_TIMEOUT_S}s"
                    ) from None
                self._sleep(_LOCK_RETRY_SLEEP_S)

    def _reserve_slot(self) -> float:
        """Critical section: read prior reservation, reserve the next slot,
        release. Returns seconds to sleep AFTER lock release.
        """
        now = self._clock()
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            self._acquire(fd)
            try:
                prior = self._read_reservation(fd, now)
                go_at = max(now, prior)
                self._write_reservation(fd, go_at + self._min_interval_s)
                return max(0.0, go_at - now)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _read_reservation(self, fd: int, now: float) -> float:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 64).strip()
        if not raw:
            return now  # first-ever call: empty file
        try:
            value = float(raw)
        except ValueError:
            logger.warning("sec rate coordinator: corrupt timestamp %r; resetting", raw)
            return now
        if value > now + _MAX_PLAUSIBLE_RESERVATION_S:
            logger.warning(
                "sec rate coordinator: implausible future reservation %.3f (now=%.3f); resetting",
                value,
                now,
            )
            return now
        return value

    def _write_reservation(self, fd: int, value: float) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"{value:.6f}".encode("ascii"))
        # No fsync: see module docstring (#381 review) — visibility to live
        # concurrent processes is via flock + page cache; restart durability is
        # not needed because stale past values collapse to `now`.
