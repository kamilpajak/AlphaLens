"""End-to-end smoke tests for `alphalens backtest` CLI.

The backtest command crashed in production (`TypeError: BacktestEngine.__init__()
got an unexpected keyword argument 'portfolio_value'`) and slipped past 715
pre-existing tests because nothing invoked the CLI end-to-end. These tests
close that gap: they use the **real** `BacktestEngine`, `write_markdown_report`,
and `cost_sensitivity_table`, so any signature drift between CLI wiring and
backing API gets caught on the next test run.

Strategy (per zen review):
- Patch only at I/O boundaries: the data loaders (`load_lean_histories`,
  universe loaders) and the scorer (replaced with a deterministic fake
  returning `DataFrame({"ticker": [...], "score": [...]})`).
- OHLCV is a trivial constant frame satisfying `MIN_BARS_REQUIRED = 220`
  plus the lowercase column convention. The scorer is faked, so prices
  never flow into real indicators — zero stockstats / guardrails
  brittleness.
- `--no-attrib` skips Fama-French factor loading (no FF files in test env).
- Each end-to-end test writes the report to a tempdir.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
from typer.testing import CliRunner


def _minimal_ohlcv(n_bars: int = 500) -> pd.DataFrame:
    """Trivial constant OHLCV satisfying MIN_BARS_REQUIRED=220.

    Starts in 2022 so that any 2023+ backtest window has enough truncated
    history on the first simulated day. The scorer is faked in the
    end-to-end tests, so prices never reach real indicators — values just
    need to be finite and the shape must match the DataFrame convention
    (DatetimeIndex, lowercase OHLCV columns).
    """
    idx = pd.date_range("2022-01-03", periods=n_bars, freq="B")
    return pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1_000_000,
        },
        index=idx,
    )


def _fake_scorer(histories, config):
    """Deterministic scorer returning a fixed ranking regardless of input.

    Matches the `Scorer` protocol: `(histories, config) -> DataFrame[ticker, score]`.
    """
    tickers = [t for t in histories.keys() if t != config.get("benchmark", "SPY")]
    if not tickers:
        return pd.DataFrame(columns=["ticker", "score"])
    scores = np.linspace(1.0, 0.1, len(tickers))
    return pd.DataFrame({"ticker": tickers, "score": scores})


class TestBacktestCLIHelp(unittest.TestCase):
    """Catches import errors and Typer registration regressions."""

    def setUp(self):
        self.runner = CliRunner()

    def test_backtest_help_renders(self):
        from alphalens_cli.main import app

        result = self.runner.invoke(app, ["backtest", "--help"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("--scorer", result.output)
        self.assertIn("--weighting", result.output)
        self.assertIn("--start", result.output)
        self.assertIn("--end", result.output)
        self.assertIn("--no-attrib", result.output)


class TestBacktestCLIEndToEnd(unittest.TestCase):
    """Exercises the full CLI → engine → report path with a faked scorer.

    What would have caught the `portfolio_value` bug: the CLI passes every
    kwarg it declares to real `BacktestEngine` and real `write_markdown_report`.
    If either API changes upstream without updating the CLI, these tests fail
    immediately with a TypeError at invocation time.
    """

    def setUp(self):
        self.runner = CliRunner()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.report_path = Path(self.tmp.name) / "report.md"

    def _histories_for(self, tickers):
        return {t: _minimal_ohlcv() for t in tickers}

    def test_momentum_scorer_end_to_end(self):
        """`--scorer momentum` executes engine + report without touching
        real OHLCV data or factor files.
        """
        from alphalens_cli.main import app

        universe_patch = {"ai": ["NVDA", "AMD"]}

        with patch(
            "alphalens.screeners.themed.universe.load_universe",
            return_value=universe_patch,
        ), patch(
            "alphalens.screeners.themed.universe.flatten_universe",
            return_value={"NVDA": ["ai"], "AMD": ["ai"]},
        ), patch(
            "alphalens.screeners.themed.backtest_adapter.momentum_scorer_adapter",
            new=_fake_scorer,
        ), patch(
            "alphalens.screeners.lean.lean_csv_loader.load_lean_histories",
            return_value=self._histories_for(["NVDA", "AMD", "SPY"]),
        ):
            result = self.runner.invoke(
                app,
                [
                    "backtest",
                    "--start", "2023-07-03",
                    "--end", "2023-07-10",
                    "--no-attrib",
                    "--top-n", "1",
                    "--report", str(self.report_path),
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("HEADLINE", result.output)
        self.assertIn("sharpe_gross", result.output)
        self.assertTrue(self.report_path.exists(), msg=result.output)

    def test_lean_scorer_end_to_end(self):
        """`--scorer lean` executes the archived Layer 2c path the same way."""
        from alphalens_cli.main import app

        with patch(
            "alphalens.screeners.lean.universe.all_tickers",
            return_value=["NVDA", "AMD"],
        ), patch(
            "alphalens.screeners.lean.lean_project.scorer.rank_universe",
            new=_fake_scorer,
        ), patch(
            "alphalens.screeners.lean.lean_csv_loader.load_lean_histories",
            return_value=self._histories_for(["NVDA", "AMD", "SPY", "QQQ", "IWM"]),
        ):
            result = self.runner.invoke(
                app,
                [
                    "backtest",
                    "--scorer", "lean",
                    "--start", "2023-07-03",
                    "--end", "2023-07-10",
                    "--no-attrib",
                    "--top-n", "1",
                    "--report", str(self.report_path),
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("HEADLINE", result.output)
        self.assertTrue(self.report_path.exists(), msg=result.output)


class TestBacktestCLIArgValidation(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_invalid_scorer_exits_cleanly(self):
        """Typer should intercept `BadParameter` and exit with a non-zero code
        instead of dumping a raw stack trace.
        """
        from alphalens_cli.main import app

        result = self.runner.invoke(
            app,
            [
                "backtest",
                "--scorer", "bogus",
                "--start", "2023-07-03",
                "--end", "2023-07-10",
                "--no-attrib",
            ],
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Unknown --scorer", result.output)


if __name__ == "__main__":
    unittest.main()
