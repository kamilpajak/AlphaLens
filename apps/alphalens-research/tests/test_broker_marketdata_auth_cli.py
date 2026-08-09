"""CLI tests for ``alphalens broker marketdata-auth`` — the LIVE MARKET-DATA
OAuth bootstrap (app ``bracket-keeper``, market-data only).

Mirrors ``TestAuthCommand`` in ``test_broker_cli.py`` but targets the LIVE
market-data auth path in ``data/alt_data/saxo_marketdata_auth.py`` (a SEPARATE
token store, a SEPARATE app registration, the LIVE logon host). Money-safe: the
command never imports the SIM-only order rail under ``brokers/saxo/`` and never
places an order.

The OAuth exchange is mocked at ``saxo_marketdata_auth.requests.post`` and the
localhost redirect at ``_wait_for_oauth_callback`` — no network is hit. The
CSRF ``state`` is generated with ``secrets.token_urlsafe`` inside the command,
so we patch that to make the round-trip deterministic.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner


class _Resp:
    """``requests`` response stand-in — Saxo answers the token endpoint 201."""

    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


_POST_TARGET = "alphalens_pipeline.data.alt_data.saxo_marketdata_auth.requests.post"
_STATE_TARGET = "secrets.token_urlsafe"


class TestMarketDataAuthCommand(unittest.TestCase):
    _REDIRECT = "http://localhost:8766/callback"  # NOSONAR — OAuth loopback, never fetched

    def setUp(self):
        self.runner = CliRunner()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = Path(self._tmp.name) / "token_store.json"
        self.env = {
            "SAXO_LIVE_TOKEN_STORE_PATH": str(self.store_path),
            "SAXO_LIVE_APP_KEY": "live-key-x",
            "SAXO_LIVE_APP_SECRET": "live-secret-x",
            "SAXO_LIVE_AUTH_REDIRECT_URL": self._REDIRECT,
        }

    def _invoke(self, *args: str):
        from alphalens_cli.commands.broker import broker_app

        return self.runner.invoke(broker_app, ["marketdata-auth", *args])

    def _seed_store(self, *, access_ttl_s: int = 900, refresh_token: str = "rt-seed") -> None:
        expiry = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=access_ttl_s)
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(
            json.dumps(
                {
                    "access_token": "acc-seed-token",
                    "refresh_token": refresh_token,
                    "expires_at": expiry.isoformat(),
                }
            )
        )

    # -- attended flow ----------------------------------------------------

    def test_attended_flow_exchanges_persists_and_never_prints_tokens(self):
        payload = {
            "access_token": "acc-secret-token",
            "refresh_token": "ref-secret-token",
            "expires_in": 1200,
        }
        with (
            mock.patch.dict("os.environ", self.env, clear=True),
            mock.patch(_STATE_TARGET, return_value="state-1"),
            mock.patch(_POST_TARGET, return_value=_Resp(201, payload)) as mock_post,
            mock.patch(
                "alphalens_cli.commands.broker._wait_for_oauth_callback",
                return_value=("the-code", "state-1"),
            ) as listener,
            mock.patch("webbrowser.open") as browser,
        ):
            result = self._invoke()

        self.assertEqual(result.exit_code, 0, result.output)
        # The redirect listener bound the port from SAXO_LIVE_AUTH_REDIRECT_URL.
        self.assertEqual(listener.call_args.args[0], 8766)
        self.assertTrue(browser.called)
        # The exchange carried the authorization_code just received.
        self.assertEqual(mock_post.call_args.kwargs["data"]["grant_type"], "authorization_code")
        self.assertEqual(mock_post.call_args.kwargs["data"]["code"], "the-code")
        # Store written in the CORRECT format to the LIVE store path.
        self.assertTrue(self.store_path.is_file())
        stored = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.assertEqual(set(stored), {"access_token", "refresh_token", "expires_at"})
        self.assertEqual(stored["refresh_token"], "ref-secret-token")
        dt.datetime.fromisoformat(stored["expires_at"])  # parseable
        # Tokens + secret NEVER printed.
        self.assertNotIn("acc-secret-token", result.output)
        self.assertNotIn("ref-secret-token", result.output)
        self.assertNotIn("live-secret-x", result.output)
        self.assertIn(str(self.store_path), result.output)

    def test_state_mismatch_aborts_before_exchange(self):
        with (
            mock.patch.dict("os.environ", self.env, clear=True),
            mock.patch(_STATE_TARGET, return_value="state-1"),
            mock.patch(_POST_TARGET) as mock_post,
            mock.patch(
                "alphalens_cli.commands.broker._wait_for_oauth_callback",
                return_value=("the-code", "evil-state"),
            ),
            mock.patch("webbrowser.open"),
        ):
            result = self._invoke()

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(mock_post.called, "mismatched state must not exchange")
        self.assertFalse(self.store_path.is_file(), "nothing persisted on CSRF mismatch")

    def test_no_browser_prints_url_only_and_still_exchanges(self):
        payload = {"access_token": "at", "refresh_token": "rt", "expires_in": 1200}
        with (
            mock.patch.dict("os.environ", self.env, clear=True),
            mock.patch(_STATE_TARGET, return_value="state-1"),
            mock.patch(_POST_TARGET, return_value=_Resp(201, payload)),
            mock.patch(
                "alphalens_cli.commands.broker._wait_for_oauth_callback",
                return_value=("the-code", "state-1"),
            ),
            mock.patch("webbrowser.open") as browser,
        ):
            result = self._invoke("--no-browser")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(browser.called, "--no-browser must not open a browser")
        self.assertIn("live.logonvalidation.net", result.output, "LIVE authorize URL printed")
        self.assertTrue(self.store_path.is_file())

    def test_missing_live_app_key_fails_red_naming_the_var(self):
        env = dict(self.env)
        env.pop("SAXO_LIVE_APP_KEY")
        with mock.patch.dict("os.environ", env, clear=True):
            result = self._invoke()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("SAXO_LIVE_APP_KEY", result.output)

    def test_non_localhost_redirect_is_refused(self):
        env = dict(
            self.env, SAXO_LIVE_AUTH_REDIRECT_URL="http://127.0.0.1:8766/callback"
        )  # NOSONAR
        with mock.patch.dict("os.environ", env, clear=True):
            result = self._invoke()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("localhost", result.output)

    # -- offline --status -------------------------------------------------

    def test_status_alive_is_offline_and_exits_zero(self):
        self._seed_store()
        with (
            mock.patch.dict("os.environ", self.env, clear=True),
            mock.patch(_POST_TARGET, side_effect=AssertionError("--status must be offline")),
        ):
            result = self._invoke("--status")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("ALIVE", result.output)
        self.assertNotIn("acc-seed-token", result.output)
        self.assertNotIn("rt-seed", result.output)
        self.assertIn(str(self.store_path), result.output)

    def test_status_needs_no_app_creds_offline_parity(self):
        """``--status`` inspects the store with ONLY SAXO_LIVE_TOKEN_STORE_PATH —
        no app key/secret/redirect. A monitoring probe on a host that carries
        the store path but not the LIVE app creds must still report the store
        state, not exit 1 on 'missing LIVE market-data env' (SIM ``--status``
        parity: store inspection is zero-network AND zero-app-config)."""
        self._seed_store()
        env = {"SAXO_LIVE_TOKEN_STORE_PATH": str(self.store_path)}
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch(_POST_TARGET, side_effect=AssertionError("--status must be offline")),
        ):
            result = self._invoke("--status")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("ALIVE", result.output)
        self.assertIn(str(self.store_path), result.output)

    def test_status_absent_store_exits_one(self):
        with mock.patch.dict("os.environ", self.env, clear=True):
            result = self._invoke("--status")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("ABSENT", result.output)

    def test_status_corrupt_store_exits_one_without_leaking_content(self):
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text("{not json, SECRET_TOKEN inside")
        with mock.patch.dict("os.environ", self.env, clear=True):
            result = self._invoke("--status")
        self.assertEqual(result.exit_code, 1)
        self.assertNotIn("SECRET_TOKEN", result.output)

    # -- silent --refresh -------------------------------------------------

    def test_refresh_flag_rotates_the_stored_pair_silently(self):
        self._seed_store(refresh_token="rt-old")
        payload = {"access_token": "acc-new", "refresh_token": "ref-new", "expires_in": 1200}
        with (
            mock.patch.dict("os.environ", self.env, clear=True),
            mock.patch(_POST_TARGET, return_value=_Resp(201, payload)) as mock_post,
        ):
            result = self._invoke("--refresh")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(mock_post.call_args.kwargs["data"]["grant_type"], "refresh_token")
        self.assertEqual(mock_post.call_args.kwargs["data"]["refresh_token"], "rt-old")
        stored = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["refresh_token"], "ref-new")
        self.assertNotIn("acc-new", result.output)
        self.assertNotIn("ref-new", result.output)


class TestMarketDataAuthMoneySafety(unittest.TestCase):
    """The LIVE market-data command must never reach into the SIM-only order
    rail: its command body imports only from ``data/alt_data`` (the LIVE auth
    module), never from ``alphalens_pipeline.brokers``."""

    def test_marketdata_auth_command_imports_no_brokers_module(self):
        import alphalens_cli.commands.broker as broker_cmd

        tree = ast.parse(Path(broker_cmd.__file__).read_text())
        fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "marketdata_auth_command"
        )
        imported: list[str] = []
        for node in ast.walk(fn):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        offenders = [m for m in imported if "brokers" in m]
        self.assertEqual(offenders, [], "LIVE market-data auth must never import the order rail")


if __name__ == "__main__":
    unittest.main()
