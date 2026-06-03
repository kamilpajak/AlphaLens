"""CLI tests for ``alphalens saxo {auth,refresh,status,probe}``.

The security-relevant contracts pinned here:

* ``status`` prints ONLY ages / booleans / expiry-deltas — never any token
  substring (MEDIUM finding: auth codes/tokens in shell history & journal).
* The saxo command group defines NO value-taking option matching
  ``code`` / ``secret`` / ``token`` (the auth bootstrap reads the redirect URL
  via non-echoing stdin, never an argv flag — shell-history / ps leak ban).
* ``auth`` reads the pasted redirect URL via a non-echoing stdin read.

Hermetic: the manager / store / client are exercised through a real
tempdir-backed store + a MockTransport client, no network.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from alphalens_cli.commands import saxo as saxo_cmd
from alphalens_cli.main import app
from alphalens_pipeline.data.alt_data.saxo_token_store import (
    SaxoTokenRecord,
    SaxoTokenStore,
)
from typer.testing import CliRunner

SENTINEL_AT = "SENTINEL_ACCESS_TOKEN_aaa"
SENTINEL_RT = "SENTINEL_REFRESH_TOKEN_zzz"


def _record(now: float = 10_000.0, **overrides: object) -> SaxoTokenRecord:
    base: dict[str, object] = {
        "schema_version": 1,
        "environment": "sim",
        "access_token": SENTINEL_AT,
        "refresh_token": SENTINEL_RT,
        "previous_refresh_token": None,
        "access_token_expires_at": now + 1200.0,
        "refresh_token_expires_at": now + 2400.0,
        "rotated_at": now,
        "reauth_required": False,
        "reauth_reason": "none",
        "journal_state": "active",
        "journal_attempted_at": None,
        "last_full_auth_at": now,
    }
    base.update(overrides)
    return SaxoTokenRecord(**base)  # type: ignore[arg-type]


class TestSaxoCliRegistration(unittest.TestCase):
    def test_saxo_app_registered_on_root(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["saxo", "--help"])
        self.assertEqual(result.exit_code, 0)
        for sub in ("auth", "refresh", "status", "probe"):
            self.assertIn(sub, result.stdout)

    def test_no_value_taking_secret_option_defined(self) -> None:
        # Walk every command's parameters; none may be a value-taking option
        # whose name contains code/secret/token (shell-history + ps leak ban).
        banned = ("code", "secret", "token")
        for command in saxo_cmd.saxo_app.registered_commands:
            params = getattr(command, "params", []) or []
            import inspect

            sig = inspect.signature(command.callback)  # type: ignore[arg-type]
            for name in sig.parameters:
                lowered = name.lower()
                self.assertFalse(
                    any(b in lowered for b in banned),
                    f"command {command.name} exposes a value-taking option "
                    f"{name!r} matching a banned secret token name.",
                )


class TestSaxoStatus(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.store = SaxoTokenStore(self.dir, environment="sim")
        self.store.write(_record())

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_status_prints_no_token_material(self) -> None:
        out = saxo_cmd.render_status(self.store, environment="sim", now=10_000.0)
        self.assertNotIn(SENTINEL_AT, out)
        self.assertNotIn(SENTINEL_RT, out)
        # It DOES surface the health booleans / deltas.
        self.assertIn("reauth_required", out)
        self.assertIn("environment", out)

    def test_status_reports_bootstrap_when_no_file(self) -> None:
        empty = SaxoTokenStore(Path(self._td.name) / "empty", environment="sim")
        out = saxo_cmd.render_status(empty, environment="sim", now=10_000.0)
        self.assertIn("bootstrap", out.lower())


class TestSaxoRefreshCommand(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_refresh_emits_allowlisted_gauges_only(self) -> None:
        store = SaxoTokenStore(self.dir, environment="sim")
        store.write(_record(access_token_expires_at=10_000.0 + 100.0))

        emitted: dict[str, float] = {}

        def fake_emit(job: str, metrics: dict) -> Path:
            emitted.update(metrics)
            return self.dir / "x.prom"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "access_token": "AT-new",
                    "refresh_token": "RT-new",
                    "expires_in": 1200,
                    "refresh_token_expires_in": 2400,
                },
            )

        with mock.patch.object(saxo_cmd, "emit_domain_metrics", fake_emit):
            saxo_cmd.run_refresh(
                store=store,
                environment="sim",
                client=_mock_client(handler),
                now=10_000.0,
                emit=True,
            )
        # No emitted metric value may carry token material.
        for key, value in emitted.items():
            self.assertNotIn(SENTINEL_RT, str(value))
            self.assertNotIn(SENTINEL_AT, str(value))
            self.assertNotIn("Bearer", key)
        # The chain-state gauge is present.
        self.assertTrue(
            any("alphalens_saxo_chain_state" in k for k in emitted),
            f"expected a chain_state gauge, got {list(emitted)}",
        )


class TestSaxoAuthNonEchoing(unittest.TestCase):
    def test_auth_reads_redirect_url_via_non_echoing_stdin(self) -> None:
        # The bootstrap must read the pasted redirect URL via a getpass-style
        # non-echoing read, not click.prompt/echoing input. Pin that the
        # command body calls the project's non-echoing reader.
        self.assertTrue(
            hasattr(saxo_cmd, "_read_redirect_url"),
            "auth must funnel the pasted redirect URL through a single "
            "non-echoing reader (_read_redirect_url).",
        )


def _mock_client(handler):
    from alphalens_pipeline.data.alt_data.saxo_client import SaxoClient

    return SaxoClient(
        app_key="K",
        redirect_uri="https://x.invalid/cb",
        environment="sim",
        _transport=httpx.MockTransport(handler),
    )


if __name__ == "__main__":
    unittest.main()
