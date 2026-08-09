from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from alphalens_pipeline.data.alt_data.saxo_marketdata_auth import (
    LIVE_AUTH_BASE_URL,
    LiveAuthConfig,
    LiveTokenProvider,
    build_authorize_url,
    exchange_code,
    inspect_store,
    save_bundle,
)


def _cfg(tmp: Path) -> LiveAuthConfig:
    return LiveAuthConfig(
        app_key="key123",
        app_secret="secret456",
        redirect_url="http://localhost:8765/callback",
        store_path=tmp / "token_store.json",
    )


class _Resp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class TestAuthorizeUrl(unittest.TestCase):
    def test_targets_the_live_host_and_carries_state(self):
        with tempfile.TemporaryDirectory() as d:
            url = build_authorize_url(_cfg(Path(d)), state="st8")
        self.assertTrue(url.startswith(f"{LIVE_AUTH_BASE_URL}/authorize?"))
        self.assertIn("client_id=key123", url)
        self.assertIn("state=st8", url)
        self.assertIn("response_type=code", url)


class TestExchangeCode(unittest.TestCase):
    def test_accepts_http_201(self):
        """Saxo answers the code exchange with 201, not 200. A `== 200` check
        reads success as failure."""
        payload = {"access_token": "at", "refresh_token": "rt", "expires_in": 1200}
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch(
                "alphalens_pipeline.data.alt_data.saxo_marketdata_auth.requests.post",
                return_value=_Resp(201, payload),
            ),
        ):
            got = exchange_code(_cfg(Path(d)), code="abc")
        self.assertEqual(got["access_token"], "at")

    def test_raises_without_leaking_the_body(self):
        """On failure the body may still contain a token; only the code and the
        error description may surface."""
        payload = {
            "error": "invalid_grant",
            "error_description": "bad code",
            "access_token": "SECRET",
        }
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch(
                "alphalens_pipeline.data.alt_data.saxo_marketdata_auth.requests.post",
                return_value=_Resp(400, payload),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                exchange_code(_cfg(Path(d)), code="abc")
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertIn("bad code", str(ctx.exception))


class TestLiveTokenProvider(unittest.TestCase):
    def test_returns_stored_token_while_valid(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            expiry = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=900)
            cfg.store_path.write_text(
                json.dumps(
                    {
                        "access_token": "live-tok",
                        "refresh_token": "rt",
                        "expires_at": expiry.isoformat(),
                    }
                )
            )
            self.assertEqual(LiveTokenProvider(cfg).access_token(), "live-tok")

    def test_refreshes_when_close_to_expiry_and_persists_rotation(self):
        """The refresh token is single-use; the replacement must land on disk or
        the session is lost. The outbound request must carry the refresh token
        that was actually on disk — a future edit that threaded the wrong token
        must fail this."""
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            expiry = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)
            cfg.store_path.write_text(
                json.dumps(
                    {
                        "access_token": "old",
                        "refresh_token": "rt-old",
                        "expires_at": expiry.isoformat(),
                    }
                )
            )
            payload = {"access_token": "new", "refresh_token": "rt-new", "expires_in": 1200}
            with mock.patch(
                "alphalens_pipeline.data.alt_data.saxo_marketdata_auth.requests.post",
                return_value=_Resp(201, payload),
            ) as mock_post:
                self.assertEqual(LiveTokenProvider(cfg).access_token(), "new")
            self.assertEqual(mock_post.call_args.kwargs["data"]["refresh_token"], "rt-old")
            on_disk = json.loads(cfg.store_path.read_text())
            self.assertEqual(on_disk["refresh_token"], "rt-new")

    def test_store_is_written_0600(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            payload = {"access_token": "at", "refresh_token": "rt", "expires_in": 1200}
            with mock.patch(
                "alphalens_pipeline.data.alt_data.saxo_marketdata_auth.requests.post",
                return_value=_Resp(201, payload),
            ):
                exchange_code(cfg, code="abc")
            self.assertEqual(cfg.store_path.stat().st_mode & 0o777, 0o600)


class TestAtomicPersistence(unittest.TestCase):
    """A crash mid-write must never leave a truncated store, and must never
    burn the (already-rotated, already-invalid) old refresh token without the
    new one landing on disk."""

    def test_crashed_write_leaves_original_store_intact_and_no_tmp_leftovers(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            expiry = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)
            original = {
                "access_token": "old",
                "refresh_token": "rt-old",
                "expires_at": expiry.isoformat(),
            }
            cfg.store_path.write_text(json.dumps(original))
            payload = {"access_token": "new", "refresh_token": "rt-new", "expires_in": 1200}
            with (
                mock.patch(
                    "alphalens_pipeline.data.alt_data.saxo_marketdata_auth.requests.post",
                    return_value=_Resp(201, payload),
                ),
                mock.patch("os.replace", side_effect=OSError("simulated crash")),
            ):
                with self.assertRaises(OSError):
                    LiveTokenProvider(cfg).access_token()
            self.assertEqual(json.loads(cfg.store_path.read_text()), original)
            leftovers = [p for p in cfg.store_path.parent.iterdir() if p.suffix == ".tmp"]
            self.assertEqual(leftovers, [], "a failed replace must unlink its temp file")


class TestExclusiveLock(unittest.TestCase):
    """Single-holder enforcement: two processes racing near expiry must not
    both refresh the same (single-use) refresh token."""

    def test_stuck_holder_raises_an_actionable_error_within_the_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            expiry = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)
            cfg.store_path.write_text(
                json.dumps(
                    {
                        "access_token": "old",
                        "refresh_token": "rt-old",
                        "expires_at": expiry.isoformat(),
                    }
                )
            )
            lock_path = cfg.store_path.with_name(cfg.store_path.stem + ".lock")
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                with (
                    mock.patch(
                        "alphalens_pipeline.data.alt_data.saxo_marketdata_auth._LOCK_TIMEOUT_S",
                        0.05,
                    ),
                    mock.patch(
                        "alphalens_pipeline.data.alt_data.saxo_marketdata_auth._LOCK_POLL_INTERVAL_S",
                        0.01,
                    ),
                ):
                    with self.assertRaises(RuntimeError) as ctx:
                        LiveTokenProvider(cfg).access_token()
                self.assertIn("stuck", str(ctx.exception))
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def test_lock_file_is_a_sibling_of_the_store_not_its_inode(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            lock_path = cfg.store_path.with_name(cfg.store_path.stem + ".lock")
            self.assertEqual(lock_path, cfg.store_path.parent / "token_store.lock")


class TestRefreshExpiryPersistence(unittest.TestCase):
    """The LIVE store must record the refresh token's own expiry so ``--status``
    can tell a live refresh chain from a dead one (a bare refresh-token STRING
    is present in both cases)."""

    def test_save_bundle_persists_refresh_expiry_when_claim_present(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            save_bundle(
                cfg,
                {
                    "access_token": "at",
                    "refresh_token": "rt",
                    "expires_in": 1200,
                    "refresh_token_expires_in": 3600,
                },
            )
            on_disk = json.loads(cfg.store_path.read_text())
            self.assertIn("refresh_token_expires_at", on_disk)
            parsed = dt.datetime.fromisoformat(on_disk["refresh_token_expires_at"])
            self.assertIsNotNone(parsed.tzinfo, "refresh expiry must be tz-aware")
            self.assertGreater(parsed, dt.datetime.now(dt.UTC))

    def test_save_bundle_omits_refresh_expiry_when_claim_absent(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            save_bundle(cfg, {"access_token": "at", "refresh_token": "rt", "expires_in": 1200})
            on_disk = json.loads(cfg.store_path.read_text())
            self.assertNotIn("refresh_token_expires_at", on_disk)
            # A store without the field must still load and inspect cleanly.
            status = inspect_store(cfg.store_path)
            self.assertTrue(status.present)
            self.assertIsNone(status.refresh_expires_at)
            self.assertTrue(status.alive, "unknown refresh expiry falls back to alive")

    def test_inspect_reads_future_refresh_expiry_alive(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            future = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=3600)
            access = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=900)
            cfg.store_path.write_text(
                json.dumps(
                    {
                        "access_token": "at",
                        "refresh_token": "rt",
                        "expires_at": access.isoformat(),
                        "refresh_token_expires_at": future.isoformat(),
                    }
                )
            )
            status = inspect_store(cfg.store_path)
            self.assertEqual(status.refresh_expires_at, future)
            self.assertTrue(status.alive)

    def test_inspect_reads_past_refresh_expiry_not_alive(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            past = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=60)
            access = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=900)
            cfg.store_path.write_text(
                json.dumps(
                    {
                        "access_token": "at",
                        "refresh_token": "rt",
                        "expires_at": access.isoformat(),
                        "refresh_token_expires_at": past.isoformat(),
                    }
                )
            )
            status = inspect_store(cfg.store_path)
            self.assertEqual(status.refresh_expires_at, past)
            self.assertFalse(status.alive, "a past refresh expiry means the chain is dead")


class TestNaiveDatetimeIsCorrupt(unittest.TestCase):
    """A tz-naive ``expires_at`` currently slips past ``_load_store`` and later
    crashes with a naive-vs-aware ``TypeError``; it must be rejected up front as
    a corrupt store."""

    def test_naive_access_expiry_is_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            naive = dt.datetime.now() + dt.timedelta(seconds=900)  # tz-naive on purpose
            cfg.store_path.write_text(
                json.dumps(
                    {"access_token": "at", "refresh_token": "rt", "expires_at": naive.isoformat()}
                )
            )
            with self.assertRaises(RuntimeError):
                inspect_store(cfg.store_path)

    def test_naive_refresh_expiry_is_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            access = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=900)
            naive = dt.datetime.now() + dt.timedelta(seconds=3600)  # tz-naive on purpose
            cfg.store_path.write_text(
                json.dumps(
                    {
                        "access_token": "at",
                        "refresh_token": "rt",
                        "expires_at": access.isoformat(),
                        "refresh_token_expires_at": naive.isoformat(),
                    }
                )
            )
            with self.assertRaises(RuntimeError):
                inspect_store(cfg.store_path)


class TestCorruptStore(unittest.TestCase):
    """A corrupt store must raise an actionable domain error instead of a raw
    ``json.JSONDecodeError``/``KeyError`` — and never echo the file content,
    which may contain a live token."""

    def test_garbage_json_raises_without_leaking_the_content(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            cfg.store_path.write_text("{not json, but has SECRET_TOKEN embedded")
            with self.assertRaises(RuntimeError) as ctx:
                LiveTokenProvider(cfg).access_token()
            message = str(ctx.exception)
            self.assertIn(str(cfg.store_path), message)
            self.assertNotIn("SECRET_TOKEN", message)

    def test_missing_required_key_is_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            cfg.store_path.write_text(json.dumps({"access_token": "at"}))
            with self.assertRaises(RuntimeError):
                LiveTokenProvider(cfg).access_token()

    def test_unparsable_expiry_is_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            cfg.store_path.write_text(
                json.dumps(
                    {"access_token": "at", "refresh_token": "rt", "expires_at": "not-a-date"}
                )
            )
            with self.assertRaises(RuntimeError):
                LiveTokenProvider(cfg).access_token()


if __name__ == "__main__":
    unittest.main()
