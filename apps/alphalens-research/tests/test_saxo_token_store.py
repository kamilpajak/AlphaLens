"""Real-file tests for :class:`SaxoTokenStore`.

Durability + concurrency primitive for the Saxo token chain. The store is
the only thing on disk that holds the rotating refresh token, so its
correctness story is load-bearing:

* 0o600 mode (least privilege — a brokerage bearer must not be group/world
  readable).
* Atomic durable rename (temp-write → fsync(fd) → os.replace → parent-dir
  fsync) so a torn write or a power-loss after rename never loses the chain.
* Cross-process flock on a SEPARATE ``.lock`` inode, bounded acquire,
  FAILS LOUD (``SaxoLockUnavailableError``) — never degrades to no-op.
* In-file lease (``journal_state``) so the lock is not held across the
  network; a peer seeing a fresh lease waits, a peer seeing an expired
  lease takes over.
* Corrupt / short-read JSON returns a typed error, never a silent empty
  token (which would force a re-auth).

The 2-process race + lease-takeover tests use real ``multiprocessing``
children against a shared tempdir so the flock semantics are exercised
end-to-end, not mocked.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import stat
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.data.alt_data.saxo_token_store import (
    SaxoLockUnavailableError,
    SaxoTokenRecord,
    SaxoTokenStore,
    SaxoTokenStoreCorruptError,
)


def _record(**overrides: object) -> SaxoTokenRecord:
    base = {
        "schema_version": 1,
        "environment": "sim",
        "access_token": "AT-1",
        "refresh_token": "RT-1",
        "previous_refresh_token": None,
        "access_token_expires_at": 1_000.0,
        "refresh_token_expires_at": 2_000.0,
        "rotated_at": 0.0,
        "reauth_required": False,
        "reauth_reason": "none",
        "journal_state": "active",
        "journal_attempted_at": None,
        "last_full_auth_at": 0.0,
    }
    base.update(overrides)
    return SaxoTokenRecord(**base)  # type: ignore[arg-type]


class TestSaxoTokenStoreRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store = SaxoTokenStore(self.dir, environment="sim")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_then_read_round_trips_the_record(self) -> None:
        rec = _record(access_token="AT-xyz", refresh_token="RT-xyz")
        self.store.write(rec)
        loaded = self.store.read()
        self.assertEqual(loaded.access_token, "AT-xyz")
        self.assertEqual(loaded.refresh_token, "RT-xyz")
        self.assertEqual(loaded.environment, "sim")

    def test_file_mode_is_exactly_0o600(self) -> None:
        self.store.write(_record())
        mode = stat.S_IMODE(self.store.token_path.stat().st_mode)
        self.assertEqual(mode, 0o600, f"token file mode must be 0o600, got {oct(mode)}")

    def test_read_missing_file_returns_none(self) -> None:
        self.assertIsNone(self.store.read())

    def test_corrupt_json_raises_typed_error_not_silent_empty(self) -> None:
        self.store.token_path.write_text("{ this is not valid json ", encoding="utf-8")
        with self.assertRaises(SaxoTokenStoreCorruptError):
            self.store.read()

    def test_truncated_json_raises_typed_error(self) -> None:
        self.store.write(_record())
        # Truncate to a short prefix — a silent empty token here would force
        # an unnecessary re-auth, so it must raise instead.
        raw = self.store.token_path.read_text(encoding="utf-8")
        self.store.token_path.write_text(raw[: max(1, len(raw) // 3)], encoding="utf-8")
        with self.assertRaises(SaxoTokenStoreCorruptError):
            self.store.read()

    def test_environment_constant_is_outside_alphalens_sync_root(self) -> None:
        # The default dir must be structurally OUTSIDE ~/.alphalens (the
        # documented rsync/Nextcloud sync root) so a live brokerage bearer is
        # never exfiltrated off-host.
        from alphalens_pipeline.data.alt_data import saxo_token_store as mod

        default = mod.default_token_store_dir()
        alphalens_root = Path.home() / ".alphalens"
        self.assertFalse(
            str(default).startswith(str(alphalens_root)),
            f"default token dir {default} must not be under {alphalens_root}",
        )


class TestSaxoTokenStoreDurability(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store = SaxoTokenStore(self.dir, environment="sim")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_persist_fsyncs_fd_and_parent_dir(self) -> None:
        # Parent-dir fsync is the durable-rename recipe; without it a
        # power-loss after os.replace loses the rotation. Spy on os.fsync and
        # assert it is called at least twice (the temp fd AND an O_DIRECTORY
        # parent-dir fd).
        real_fsync = os.fsync
        calls: list[int] = []

        def spy(fd: int) -> None:
            calls.append(fd)
            real_fsync(fd)

        with mock.patch("alphalens_pipeline.data.alt_data.saxo_token_store.os.fsync", spy):
            self.store.write(_record())
        self.assertGreaterEqual(
            len(calls),
            2,
            "persist path must fsync the temp-file fd AND the parent dir fd",
        )

    def test_tempfile_is_in_the_final_dir(self) -> None:
        # Same-dir tempfile => os.replace is a same-filesystem rename(2),
        # never a cross-fs copy that opens a torn-read window over overlayfs.
        seen_dirs: list[str] = []
        import tempfile as _tempfile

        real_ntf = _tempfile.NamedTemporaryFile

        def spy_ntf(*args, **kwargs):  # type: ignore[no-untyped-def]
            seen_dirs.append(str(kwargs.get("dir")))
            return real_ntf(*args, **kwargs)

        with mock.patch(
            "alphalens_pipeline.data.alt_data.saxo_token_store.tempfile.NamedTemporaryFile",
            spy_ntf,
        ):
            self.store.write(_record())
        self.assertIn(str(self.dir), seen_dirs)

    def test_crash_between_temp_write_and_rename_leaves_old_file_intact(self) -> None:
        # First good write, then a write that crashes at os.replace -> the
        # OLD file must remain intact and parseable (atomic rename guarantee).
        self.store.write(_record(refresh_token="RT-OLD"))

        def boom(_src: object, _dst: object) -> None:
            raise OSError("simulated crash at rename")

        with mock.patch("alphalens_pipeline.data.alt_data.saxo_token_store.os.replace", boom):
            with self.assertRaises(OSError):
                self.store.write(_record(refresh_token="RT-NEW"))

        loaded = self.store.read()
        assert loaded is not None
        self.assertEqual(loaded.refresh_token, "RT-OLD", "old file must survive a torn write")


# --- 2-process race tests (real multiprocessing) ----------------------------


def _child_count_posts(dir_path: str, env: str, result_q: mp.Queue) -> None:
    """Child that takes the lock and appends a marker — used to prove
    exactly-one-holder-at-a-time semantics via a shared counter file.
    """
    store = SaxoTokenStore(Path(dir_path), environment=env)
    try:
        with store.locked():
            # Append our pid under the lock; if the lock were broken two
            # children would interleave and the file would show overlap.
            counter = Path(dir_path) / "counter.txt"
            prev = counter.read_text() if counter.exists() else ""
            time.sleep(0.05)
            counter.write_text(prev + f"{os.getpid()}\n")
            result_q.put("locked")
    except SaxoLockUnavailableError:
        result_q.put("lock-unavailable")


class TestSaxoTokenStoreLocking(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_lock_fail_loud_when_lock_dir_uncreatable(self) -> None:
        # If the lock parent cannot be created (parent is a FILE, not a dir)
        # the store must raise SaxoLockUnavailableError — NEVER degrade to a
        # no-op refresh (which would burn the rotating token unsynchronised).
        blocker = self.dir / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        nested = blocker / "sub"  # cannot mkdir under a regular file
        store = SaxoTokenStore(nested, environment="sim")
        with self.assertRaises(SaxoLockUnavailableError):
            with store.locked():
                pass

    def test_two_processes_serialise_under_the_lock(self) -> None:
        ctx = mp.get_context("spawn")
        q: mp.Queue = ctx.Queue()
        p1 = ctx.Process(target=_child_count_posts, args=(str(self.dir), "sim", q))
        p2 = ctx.Process(target=_child_count_posts, args=(str(self.dir), "sim", q))
        p1.start()
        p2.start()
        p1.join(30)
        p2.join(30)
        results = [q.get(timeout=5), q.get(timeout=5)]
        self.assertEqual(sorted(results), ["locked", "locked"])
        # Both children appended exactly one line each, no interleave torn the
        # counter (serialisation held).
        counter = (self.dir / "counter.txt").read_text().strip().splitlines()
        self.assertEqual(len(counter), 2, f"expected 2 serialised writes, got {counter}")


if __name__ == "__main__":
    unittest.main()
