"""Tests for ``alphalens api serve`` CLI argument precedence.

Specifically: ``--root-path`` flag vs ``ALPHALENS_ROOT_PATH`` env var.

The flag default is ``None`` (not ``""``) so callers can explicitly pass
``--root-path ""`` to disable the prefix even when the env var is set.
This matters for diagnostic / direct-port debugging where the operator
wants to bypass the reverse-proxy prefix without unsetting the env var.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from alphalens.api.app import ENV_ROOT_PATH
from alphalens_cli.commands.api import api_app


class ApiServeRootPathPrecedenceTests(unittest.TestCase):
    """``--root-path`` and ``ALPHALENS_ROOT_PATH`` interplay."""

    def setUp(self) -> None:
        self.runner = CliRunner()
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "briefs.db"
        self.db.touch()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _invoke(self, env: dict[str, str], extra_args: list[str]) -> dict[str, str]:
        """Run ``serve`` with mocked uvicorn; return os.environ after the call."""
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch("uvicorn.run") as _,
        ):
            os.environ.pop(ENV_ROOT_PATH, None)
            for k, v in env.items():
                os.environ[k] = v
            result = self.runner.invoke(
                api_app,
                ["serve", "--db", str(self.db), *extra_args],
            )
            self.assertEqual(result.exit_code, 0, msg=result.output)
            return dict(os.environ)

    def test_no_env_no_flag_leaves_env_unset(self):
        env_after = self._invoke({}, [])
        self.assertNotIn(ENV_ROOT_PATH, env_after)

    def test_env_set_no_flag_preserves_env(self):
        env_after = self._invoke({ENV_ROOT_PATH: "/api"}, [])
        self.assertEqual(env_after[ENV_ROOT_PATH], "/api")

    def test_flag_value_overrides_env(self):
        env_after = self._invoke(
            {ENV_ROOT_PATH: "/api"},
            ["--root-path", "/v1"],
        )
        self.assertEqual(env_after[ENV_ROOT_PATH], "/v1")

    def test_flag_empty_string_overrides_env(self):
        # The point of the ``str | None`` default — an explicit empty
        # string from the CLI must clear the env value, not be ignored.
        env_after = self._invoke(
            {ENV_ROOT_PATH: "/api"},
            ["--root-path", ""],
        )
        self.assertEqual(env_after[ENV_ROOT_PATH], "")

    def test_flag_value_with_no_env(self):
        env_after = self._invoke({}, ["--root-path", "/api"])
        self.assertEqual(env_after[ENV_ROOT_PATH], "/api")


if __name__ == "__main__":
    unittest.main()
