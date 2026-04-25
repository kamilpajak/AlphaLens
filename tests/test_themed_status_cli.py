"""Smoke tests for `alphalens themed status` covering empty + populated DB paths."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from typer.testing import CliRunner


class TestThemedStatusCLI(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name) / "themed_history.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_status_empty_store_reports_no_runs(self):
        from alphalens_cli.main import app

        with patch(
            "alphalens.screeners.themed.history_store.default_history_path",
            return_value=self.tmp_path,
        ):
            result = self.runner.invoke(app, ["themed", "status", "--days", "30"])

        self.assertEqual(result.exit_code, 0, msg=result.stdout)
        self.assertIn("No runs in history", result.stdout)

    def test_status_populated_store_renders_dashboard_sections(self):
        from alphalens.screeners.themed.history_store import ThemedHistoryStore
        from alphalens_cli.main import app

        store = ThemedHistoryStore(path=self.tmp_path)
        picks = pd.DataFrame(
            [
                {"ticker": "AAA", "momentum_score": 0.9, "themes": ["quantum"]},
                {"ticker": "BBB", "momentum_score": 0.8, "themes": ["ai"]},
                {"ticker": "CCC", "momentum_score": 0.7, "themes": ["semis"]},
            ]
        )
        store.record_run(
            picks_df=picks,
            config={"top_n": 3},
            universe_size=100,
            weighting_scheme="equal",
        )
        store.record_run(
            picks_df=picks,
            config={"top_n": 3},
            universe_size=100,
            weighting_scheme="equal",
        )

        with patch(
            "alphalens.screeners.themed.history_store.default_history_path",
            return_value=self.tmp_path,
        ):
            result = self.runner.invoke(app, ["themed", "status", "--days", "30", "--top-n", "3"])

        self.assertEqual(result.exit_code, 0, msg=result.stdout)
        self.assertIn("Layer 2b Monitoring", result.stdout)
        self.assertIn("Theme concentration", result.stdout)
        self.assertIn("Turnover", result.stdout)
        self.assertIn("Staleness", result.stdout)


if __name__ == "__main__":
    unittest.main()
