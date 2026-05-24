"""CLI integration tests for `alphalens literature scan`."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from alphalens_cli.main import app
from typer.testing import CliRunner


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
    def test_monthly_window_invokes_runner_with_resolved_period(self, mock_run):
        with patch.dict(os.environ, self._env(), clear=False):
            result = self.runner.invoke(
                app, ["literature", "scan", "--window", "monthly", "--period", "2026-05"]
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["period"], "2026-05")
        self.assertEqual(kwargs["perplexity_api_key"], "pplx-test")

    @patch("alphalens_cli.commands.literature.run_weekly")
    def test_weekly_window_invokes_runner(self, mock_run):
        with patch.dict(os.environ, self._env(), clear=False):
            result = self.runner.invoke(
                app, ["literature", "scan", "--window", "weekly", "--period", "2026-W18"]
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["period"], "2026-W18")

    def test_scan_fails_without_perplexity_key(self):
        env = self._env()
        env.pop("PERPLEXITY_API_KEY")
        with patch.dict(os.environ, env, clear=True):
            result = self.runner.invoke(app, ["literature", "scan", "--window", "monthly"])
        self.assertNotEqual(result.exit_code, 0)

    @patch("alphalens_cli.commands.literature.run_monthly")
    def test_scan_passes_custom_output_dir(self, mock_run):
        with patch.dict(os.environ, self._env(), clear=False):
            result = self.runner.invoke(
                app,
                [
                    "literature",
                    "scan",
                    "--window",
                    "monthly",
                    "--period",
                    "2026-05",
                    "--output-dir",
                    "/tmp/out",
                ],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(mock_run.call_args.kwargs["output_dir"], Path("/tmp/out"))


class TestLiteratureOutputDir(unittest.TestCase):
    def test_default_output_dir_resolves_to_repo_root(self):
        # Regression: after the workspace split (ADR 0011) the literature CLI
        # lives at apps/alphalens-pipeline/alphalens_cli/commands/literature.py,
        # so Path(__file__).parents[2] now lands on apps/alphalens-pipeline,
        # not the repo root. The default output dir must still resolve to the
        # canonical docs/research/literature_review/ at the repo root, not
        # to a stray subtree under apps/alphalens-pipeline/.
        from alphalens_cli.commands.literature import _resolve_output_dir

        resolved = _resolve_output_dir(None)
        # Repo root contains an `apps/` directory and a `pyproject.toml`.
        repo_root = resolved.parents[2]  # docs/research/literature_review → repo_root
        self.assertTrue(
            (repo_root / "apps").is_dir(),
            f"Resolved output dir {resolved} does not sit under a repo root with apps/ "
            "(parents[N] indexing in _resolve_output_dir has regressed).",
        )
        self.assertTrue(
            (repo_root / "pyproject.toml").is_file(),
            f"Resolved output dir {resolved} does not sit under a repo root with pyproject.toml.",
        )
        self.assertEqual(resolved.name, "literature_review")


if __name__ == "__main__":
    unittest.main()
