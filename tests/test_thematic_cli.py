"""CLI integration tests for `alphalens thematic map-themes` output rendering."""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from typer.testing import CliRunner

from alphalens_cli.main import app

_FAKE_CANDIDATES = pd.DataFrame(
    [
        {
            "theme": "quantum_computing",
            "ticker": "QUBT",
            "company_name": "Quantum Computing Inc",
            "rationale": "Pure-play quantum hardware",
            "gemini_confidence": 0.85,
            "market_cap": 1_780_000_000.0,
            "gates_passed": ["tenk", "press"],
            "gates_passed_str": "tenk,press",
            "n_gates_passed": 2,
            "gates_failed": ["etf", "insider"],
            "gates_failed_str": "etf,insider",
            "n_gates_failed": 2,
            "gates_unknown": [],
            "gates_unknown_str": "",
            "n_gates_unknown": 0,
            "verified": True,
        }
    ]
)


class TestMapThemesCLI(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def _env(self, **overrides):
        base = {"GOOGLE_API_KEY": "fake", "POLYGON_API_KEY": "fake"}
        base.update(overrides)
        return base

    def test_map_themes_renders_summary_with_drop_counts(self):
        df = _FAKE_CANDIDATES.copy()
        df.attrs["dropped_total"] = 3
        df.attrs["dropped_all_unknown"] = 1

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, self._env(), clear=False),
            patch(
                "alphalens_cli.commands.thematic.themes_mod.roll_up",
                return_value=pd.DataFrame(
                    [
                        {
                            "theme": "quantum_computing",
                            "novelty_score": 5.0,
                            "count_recent": 3,
                            "count_baseline": 0,
                            "count_window": 3,
                            "first_seen": dt.date(2026, 5, 1),
                            "latest_seen": dt.date(2026, 5, 15),
                        }
                    ]
                ),
            ),
            patch(
                "alphalens_cli.commands.thematic.themes_mod.flag_novel",
                return_value=pd.DataFrame(
                    [
                        {
                            "theme": "quantum_computing",
                            "novelty_score": 5.0,
                            "count_recent": 3,
                            "count_baseline": 0,
                            "count_window": 3,
                            "first_seen": dt.date(2026, 5, 1),
                            "latest_seen": dt.date(2026, 5, 15),
                        }
                    ]
                ),
            ),
            patch(
                "alphalens_cli.commands.thematic.orchestrator.map_themes",
                return_value=df,
            ),
        ):
            result = self.runner.invoke(
                app,
                [
                    "thematic",
                    "map-themes",
                    "--date",
                    "2026-05-15",
                    "--output-dir",
                    tmpdir,
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Wrote 1 candidate rows", result.output)
        self.assertIn("dropped 3 unverified", result.output)
        self.assertIn("1 were all-unknown", result.output)
        # Table header + candidate row rendered
        self.assertIn("QUBT", result.output)
        self.assertIn("tenk,press", result.output)

    def test_map_themes_no_novel_themes_short_circuits(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, self._env(), clear=False),
            patch(
                "alphalens_cli.commands.thematic.themes_mod.roll_up",
                return_value=pd.DataFrame(),
            ),
            patch(
                "alphalens_cli.commands.thematic.themes_mod.flag_novel",
                return_value=pd.DataFrame(),
            ),
            patch(
                "alphalens_cli.commands.thematic.orchestrator.map_themes",
                side_effect=AssertionError("must not be called"),
            ),
        ):
            result = self.runner.invoke(
                app,
                ["thematic", "map-themes", "--date", "2026-05-15", "--output-dir", tmpdir],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("No novel themes", result.output)

    def test_map_themes_missing_api_key_raises(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, {}, clear=True),
        ):
            result = self.runner.invoke(
                app,
                ["thematic", "map-themes", "--date", "2026-05-15", "--output-dir", tmpdir],
            )
        # typer.BadParameter raises non-zero
        self.assertNotEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
