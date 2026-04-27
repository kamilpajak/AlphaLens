"""CLI integration tests for `alphalens literature`."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from alphalens_cli.main import app


class TestLiteratureCLI(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def _env(self, **overrides):
        base = {
            "PERPLEXITY_API_KEY": "pplx-test",
            "TELEGRAM_BOT_TOKEN": "bot",
            "TELEGRAM_CHAT_ID": "chat",
        }
        base.update(overrides)
        return base

    @patch("alphalens_cli.commands.literature.run_monthly")
    def test_monthly_invokes_runner_with_resolved_period(self, mock_run):
        with patch.dict(os.environ, self._env(), clear=False):
            result = self.runner.invoke(app, ["literature", "monthly", "--period", "2026-05"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["period"], "2026-05")
        self.assertEqual(kwargs["perplexity_api_key"], "pplx-test")

    @patch("alphalens_cli.commands.literature.run_weekly")
    def test_weekly_invokes_runner(self, mock_run):
        with patch.dict(os.environ, self._env(), clear=False):
            result = self.runner.invoke(app, ["literature", "weekly", "--period", "2026-W18"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["period"], "2026-W18")

    def test_monthly_fails_without_perplexity_key(self):
        env = self._env()
        env.pop("PERPLEXITY_API_KEY")
        with patch.dict(os.environ, env, clear=True):
            result = self.runner.invoke(app, ["literature", "monthly"])
        self.assertNotEqual(result.exit_code, 0)

    @patch("alphalens_cli.commands.literature.run_monthly")
    def test_monthly_passes_custom_output_dir(self, mock_run):
        with patch.dict(os.environ, self._env(), clear=False):
            result = self.runner.invoke(
                app,
                ["literature", "monthly", "--period", "2026-05", "--output-dir", "/tmp/out"],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(mock_run.call_args.kwargs["output_dir"], Path("/tmp/out"))


if __name__ == "__main__":
    unittest.main()
