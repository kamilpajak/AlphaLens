"""Durability + concurrency primitive for the Saxo OAuth token chain.

The store owns the single small JSON record per environment that holds the
rotating refresh token. Because losing that token forces a manual browser
re-login (OAuth design), every write goes through a full durable rename and
every read either parses cleanly or raises a typed error — never a silent
empty token.

Key invariants (locked design 2026-06-03 §Persistence & locking):

* **0o600** via ``os.open(O_RDWR|O_CREAT, 0o600)`` — a live brokerage bearer
  must never be group/world readable.
* **Atomic durable rename**: ``NamedTemporaryFile(dir=final_dir)`` →
  ``write`` → ``flush`` → ``os.fsync(fd)`` → ``os.replace`` →
  ``os.fsync(parent_dir_fd)``. The same-dir tempfile keeps ``os.replace`` a
  same-filesystem ``rename(2)``; the parent-dir fsync makes the rename
  durable across a hard power-loss (the bare fd fsync alone does not).
* **Cross-process flock** on a SEPARATE ``.lock`` inode, bounded
  non-blocking acquire. CRITICAL DEPARTURE from
  :mod:`sec_rate_coordinator`: this lock **FAILS LOUD**
  (:class:`SaxoLockUnavailableError`) — it does NOT degrade to a no-op,
  because an unsynchronised refresh BURNS the rotating token (a missed
  refresh is recoverable within the window; a double-rotation is not).
* The lock is **not held across the network**. The manager acquires, writes
  a short-TTL lease, RELEASES, does the POST, then re-acquires to commit —
  see :mod:`saxo_token_manager`. The store only provides the ``locked()``
  context manager + the durable write/read; the lease-vs-network discipline
  lives in the manager.

Location: NOT under ``~/.alphalens/`` (the documented rsync/Nextcloud sync
root). Default ``~/.config/alphalens-saxo/`` for local dev; the VPS deploy
config pins ``/etc/alphalens/saxo/`` via ``SAXO_TOKEN_STORE_DIR``. Either way
structurally outside the sync root so a live bearer is never exfiltrated.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

try:
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX is out of scope
    _HAVE_FCNTL = False

SAXO_TOKEN_STORE_DIR_ENV = "SAXO_TOKEN_STORE_DIR"

SCHEMA_VERSION = 1

# Bounded LOCK_NB spin (mirrors sec_rate_coordinator) before FAILING LOUD.
# Generous vs realistic contention (single-writer keep-alive + an occasional
# manual `alphalens saxo refresh`); a stuck holder must surface, not stall
# forever.
_LOCK_ACQUIRE_TIMEOUT_S = 5.0
_LOCK_RETRY_SLEEP_S = 0.01

# A persisted record JSON shorter than this is treated as torn / truncated.
# The smallest valid record (all short tokens) is comfortably longer.
_MIN_PLAUSIBLE_RECORD_BYTES = 40

JournalState = Literal["active", "refreshing"]
ReauthReason = Literal["none", "expired_locally", "server_rejected", "lost_rotation"]


class SaxoTokenStoreError(RuntimeError):
    """Base class for token-store failures."""


class SaxoTokenStoreCorruptError(SaxoTokenStoreError):
    """The on-disk record is truncated / unparseable.

    Raised instead of returning a silent empty token: an empty token would
    force an unnecessary manual re-auth, whereas a typed error lets the
    manager surface a distinct ``chain_state=corrupt`` signal.
    """


class SaxoLockUnavailableError(SaxoTokenStoreError):
    """The cross-process lock could not be acquired / created.

    FAILS LOUD by design — the caller MUST NOT proceed to refresh, because an
    unsynchronised ``/token`` call burns the rotating refresh token.
    """


@dataclass
class SaxoTokenRecord:
    """One token-chain record per environment.

    Absolute WALL epochs (``*_expires_at``) are stored, not relative TTLs: a
    oneshot keep-alive can fire minutes after the last write and a relative
    TTL is meaningless across restarts.
    """

    schema_version: int
    environment: str
    access_token: str
    refresh_token: str
    # The prior (rotated-out) refresh token. Retained for forensic history and
    # a possible future double-crash ring-buffer recovery — the current
    # single-attempt ``recover()`` only ever retries the active ``refresh_token``
    # (one attempt is the minimal strictly-cannot-lose choice), so this field is
    # NOT consulted on the recovery path today.
    previous_refresh_token: str | None
    access_token_expires_at: float
    refresh_token_expires_at: float
    rotated_at: float
    reauth_required: bool
    reauth_reason: ReauthReason
    journal_state: JournalState
    journal_attempted_at: float | None
    last_full_auth_at: float

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SaxoTokenRecord:
        # Strict field set — an unknown/missing key is a contract break, not a
        # silent default. Let KeyError/TypeError propagate to the corrupt path.
        return cls(
            schema_version=int(data["schema_version"]),  # type: ignore[arg-type]
            environment=str(data["environment"]),
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]),
            previous_refresh_token=(
                None
                if data.get("previous_refresh_token") is None
                else str(data["previous_refresh_token"])
            ),
            access_token_expires_at=float(data["access_token_expires_at"]),  # type: ignore[arg-type]
            refresh_token_expires_at=float(data["refresh_token_expires_at"]),  # type: ignore[arg-type]
            rotated_at=float(data["rotated_at"]),  # type: ignore[arg-type]
            reauth_required=bool(data["reauth_required"]),
            reauth_reason=str(data["reauth_reason"]),  # type: ignore[assignment]
            journal_state=str(data["journal_state"]),  # type: ignore[assignment]
            journal_attempted_at=(
                None
                if data.get("journal_attempted_at") is None
                else float(data["journal_attempted_at"])  # type: ignore[arg-type]
            ),
            last_full_auth_at=float(data["last_full_auth_at"]),  # type: ignore[arg-type]
        )


def default_token_store_dir() -> Path:
    """Resolve the token-store directory.

    ``SAXO_TOKEN_STORE_DIR`` overrides (the VPS deploy config pins
    ``/etc/alphalens/saxo/``); the code default is the user-writable
    ``~/.config/alphalens-saxo/`` so tests + local dev work without root.
    Either path is structurally OUTSIDE ``~/.alphalens/`` (the sync root).
    """
    override = os.environ.get(SAXO_TOKEN_STORE_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".config" / "alphalens-saxo"


class SaxoTokenStore:
    """Flat-file + flock token store for one environment.

    ``clock`` is injectable for tests; production uses wall ``time.time``.
    """

    def __init__(
        self,
        directory: Path,
        *,
        environment: str,
        clock: Callable[[], float] = time.time,  # injectable wall clock
        sleep: Callable[[float], None] = time.sleep,  # injectable for hermetic lock tests
    ) -> None:
        self._dir = Path(directory)
        self._environment = environment
        self._clock = clock
        self._sleep = sleep
        self.token_path = self._dir / f"token_{environment}.json"
        self.lock_path = self._dir / f"token_{environment}.lock"

    # --- read / write -------------------------------------------------------

    def read(self) -> SaxoTokenRecord | None:
        """Return the persisted record, or ``None`` if no file exists.

        Raises :class:`SaxoTokenStoreCorruptError` on a truncated / unparseable
        file — never a silent empty token.
        """
        try:
            raw = self.token_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        if len(raw.encode("utf-8")) < _MIN_PLAUSIBLE_RECORD_BYTES:
            raise SaxoTokenStoreCorruptError(
                f"token record at {self.token_path} is implausibly short "
                f"({len(raw)} chars) — treating as torn/truncated"
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SaxoTokenStoreCorruptError(
                f"token record at {self.token_path} is not valid JSON: {exc}"
            ) from exc
        try:
            return SaxoTokenRecord.from_dict(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise SaxoTokenStoreCorruptError(
                f"token record at {self.token_path} is missing/invalid fields: {exc}"
            ) from exc

    def write(self, record: SaxoTokenRecord) -> None:
        """Durably + atomically persist ``record``.

        temp-write → fsync(fd) → os.replace → fsync(parent_dir). EVERY write
        (including the sticky reauth flag and the journal lease) goes through
        this full path — no in-place field mutation (a crash mid-in-place
        write tears the file).
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = record.to_json()

        # Same-dir tempfile so os.replace is a same-fs rename(2). delete=False
        # because we hand the file to os.replace ourselves.
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=self._dir,
            delete=False,
            prefix=f".token_{self._environment}.",
            suffix=".tmp",
            encoding="utf-8",
        ) as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)

        # 0o600 BEFORE the rename so the live file is never briefly world/group
        # readable. tempfile already creates 0o600, but pin it explicitly.
        os.chmod(tmp_path, 0o600)
        try:
            os.replace(tmp_path, self.token_path)
        except OSError:
            # Clean up the orphan tempfile so a retry does not litter the dir,
            # then re-raise so the manager's journal/recovery path sees the
            # crash. The OLD token file is untouched (atomic rename guarantee).
            with suppress(OSError):  # pragma: no cover - best-effort cleanup
                tmp_path.unlink(missing_ok=True)
            raise

        self._fsync_parent_dir()

    def _fsync_parent_dir(self) -> None:
        """fsync the parent directory so the rename itself is durable.

        Wrapped log-never-raise: some filesystems (notably certain overlay /
        network mounts) reject an O_DIRECTORY fsync. Durability degrades there
        but the operation must not fail.
        """
        try:
            dir_fd = os.open(str(self._dir), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError as exc:  # pragma: no cover - platform dependent
            logger.warning("saxo token store: cannot open parent dir for fsync: %s", exc)
            return
        try:
            os.fsync(dir_fd)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            logger.warning("saxo token store: parent-dir fsync rejected: %s", exc)
        finally:
            os.close(dir_fd)

    # --- locking ------------------------------------------------------------

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the cross-process flock for the duration of the block.

        FAILS LOUD: raises :class:`SaxoLockUnavailableError` if the lock dir /
        file cannot be created or the lock cannot be acquired within the
        bounded spin. The caller MUST NOT refresh without this lock.

        The lock is on a SEPARATE ``.lock`` inode so a reader opening the token
        file never contends with the writer's advisory lock.
        """
        if not _HAVE_FCNTL:  # pragma: no cover - non-POSIX out of scope
            raise SaxoLockUnavailableError("fcntl unavailable; cannot serialise Saxo refresh")
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise SaxoLockUnavailableError(
                f"saxo lock {self.lock_path} not creatable: {exc}"
            ) from exc
        try:
            self._acquire(fd)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _acquire(self, fd: int) -> None:
        deadline = self._clock() + _LOCK_ACQUIRE_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if self._clock() >= deadline:
                    raise SaxoLockUnavailableError(
                        f"saxo lock {self.lock_path} busy > {_LOCK_ACQUIRE_TIMEOUT_S}s"
                    ) from None
                self._sleep(_LOCK_RETRY_SLEEP_S)


__all__ = [
    "SAXO_TOKEN_STORE_DIR_ENV",
    "SCHEMA_VERSION",
    "JournalState",
    "ReauthReason",
    "SaxoLockUnavailableError",
    "SaxoTokenRecord",
    "SaxoTokenStore",
    "SaxoTokenStoreCorruptError",
    "SaxoTokenStoreError",
    "default_token_store_dir",
]
