"""Smoke tests for `alphalens guru` CLI."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import yaml
from typer.testing import CliRunner

from alphalens.guru.llm_scorer import ConvictionResult
from alphalens.guru.pilot_runner import SingleYearResult

_SAMPLE_PROMPT = "You are a value investor. Score the company 0-100.\n"

# Pilot CLI checks GOOGLE_API_KEY before delegating to _build_pilot_years; tests
# that mock the build step still need the key set so the env-precondition gate
# doesn't intercept. In CI these are unset; locally they come from .env.
_FAKE_API_ENV = {
    "GOOGLE_API_KEY": "fake-google-key-for-test",
    "POLYGON_API_KEY": "fake-polygon-key-for-test",
}


def _stub_year(year: int, outperf: float = 0.10) -> SingleYearResult:
    return SingleYearResult(
        year=year,
        asof=pd.Timestamp(f"{year}-01-01"),
        picks=[
            ConvictionResult(
                ticker="AAPL",
                asof=pd.Timestamp(f"{year}-01-01"),
                conviction=85.0,
                rationale="x",
                prompt_sha="a" * 64,
                raw_response="{}",
                input_tokens=1000,
                output_tokens=50,
                cost_usd=0.001,
            )
        ],
        portfolio_return=0.15,
        benchmark_return=0.15 - outperf,
        outperformance=outperf,
        total_cost_usd=0.005,
        n_scored=30,
        n_skipped=0,
    )


class TestGuruStatus(unittest.TestCase):
    def test_status_prints_prompt_fingerprint(self):
        from alphalens_cli.main import app

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / "prompt.txt"
            prompt_path.write_text(_SAMPLE_PROMPT)

            with patch(
                "alphalens.guru.prompt.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="d" * 40 + "\n", stderr=""),
            ):
                result = runner.invoke(
                    app,
                    ["guru", "status", "--prompt", str(prompt_path)],
                )

        self.assertEqual(result.exit_code, 0, msg=result.stdout)
        self.assertIn("dddddddd", result.stdout)  # git SHA fragment
        self.assertIn("prompt.txt", result.stdout)


class TestGuruPilot(unittest.TestCase):
    def test_pilot_command_runs_and_returns_exit_code_by_verdict(self):
        from alphalens_cli.main import app

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            prompt_path = tmp_path / "prompt.txt"
            prompt_path.write_text(_SAMPLE_PROMPT)

            # Fake PIT universe YAMLs
            data_dir = tmp_path / "sp500_pit"
            data_dir.mkdir()
            for year in (2018, 2020, 2022, 2024):
                (data_dir / f"{year}.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "year": year,
                            "as_of": f"{year}-01-01",
                            "tickers": [f"T{i:03d}" for i in range(60)],
                        }
                    )
                )
            output_path = tmp_path / "report.md"

            stub_years = [
                _stub_year(2018, 0.13),
                _stub_year(2020, 0.01),
                _stub_year(2022, 0.15),
                _stub_year(2024, 0.02),
            ]

            with (
                patch.dict(os.environ, _FAKE_API_ENV, clear=False),
                patch(
                    "alphalens_cli.commands.guru._build_pilot_years",
                    return_value=stub_years,
                ),
                patch(
                    "alphalens.guru.prompt.subprocess.run",
                    return_value=MagicMock(returncode=0, stdout="e" * 40 + "\n", stderr=""),
                ),
            ):
                result = runner.invoke(
                    app,
                    [
                        "guru",
                        "pilot",
                        "--prompt",
                        str(prompt_path),
                        "--data-dir",
                        str(data_dir),
                        "--output",
                        str(output_path),
                        "--years",
                        "2018,2020,2022,2024",
                        "--sample-size",
                        "30",
                        "--top-n",
                        "10",
                        "--seed",
                        "42",
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=f"stdout: {result.stdout}")
            self.assertTrue(output_path.exists())
            content = output_path.read_text()
            self.assertIn("Pilot v2", content)
            self.assertIn("Verdict", content)

    def test_pilot_exits_nonzero_on_kill(self):
        from alphalens_cli.main import app

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            prompt_path = tmp_path / "prompt.txt"
            prompt_path.write_text(_SAMPLE_PROMPT)

            data_dir = tmp_path / "sp500_pit"
            data_dir.mkdir()
            for year in (2018, 2020, 2022, 2024):
                (data_dir / f"{year}.yaml").write_text(
                    yaml.safe_dump({"year": year, "as_of": f"{year}-01-01", "tickers": ["A"]})
                )

            # All years UNDERPERFORM → KILL
            stub_years = [
                _stub_year(2018, -0.05),
                _stub_year(2020, 0.01),
                _stub_year(2022, 0.02),
                _stub_year(2024, 0.01),
            ]

            with (
                patch.dict(os.environ, _FAKE_API_ENV, clear=False),
                patch(
                    "alphalens_cli.commands.guru._build_pilot_years",
                    return_value=stub_years,
                ),
                patch(
                    "alphalens.guru.prompt.subprocess.run",
                    return_value=MagicMock(returncode=0, stdout="f" * 40 + "\n", stderr=""),
                ),
            ):
                result = runner.invoke(
                    app,
                    [
                        "guru",
                        "pilot",
                        "--prompt",
                        str(prompt_path),
                        "--data-dir",
                        str(data_dir),
                        "--output",
                        str(tmp_path / "r.md"),
                        "--years",
                        "2018,2020,2022,2024",
                    ],
                )

            self.assertEqual(result.exit_code, 1)
            self.assertIn("KILL", result.stdout)


class TestGuruImports(unittest.TestCase):
    def test_real_llm_client_import_path_is_valid(self):
        """Regression: the CLI uses lowercase `tradingagents.llm_clients`,
        not `TradingAgents.tradingagents.llm_clients`. Import must resolve."""
        import importlib

        mod = importlib.import_module("tradingagents.llm_clients.google_client")
        self.assertTrue(hasattr(mod, "GoogleClient"))


if __name__ == "__main__":
    unittest.main()
