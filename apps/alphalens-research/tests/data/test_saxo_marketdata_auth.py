from __future__ import annotations

import datetime as dt
import json
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
        the session is lost."""
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
            ):
                self.assertEqual(LiveTokenProvider(cfg).access_token(), "new")
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


if __name__ == "__main__":
    unittest.main()
