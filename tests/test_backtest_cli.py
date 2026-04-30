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
- Each end-to-end test writes the report to a tempdir and asserts on
  structural markers of the rendered markdown (section headers, table
  rows) rather than on ephemeral stdout formatting.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
from typer.testing import CliRunner

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[mK]")


def _normalize_cli_output(text: str) -> str:
    """Strip ANSI escape codes + collapse whitespace.

    Typer renders --help and BadParameter messages with terminal-width-aware
    wrapping that breaks ``--scorer`` across two lines as ``-`` + ``-scorer`` on
    the narrow GitHub Actions terminal (no TTY). Tests assert presence of flag
    names and error keywords without depending on rendering width.
    """
    return re.sub(r"\s+", " ", _ANSI_ESCAPE.sub("", text))


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


def _assert_report_has_structural_markers(test_case: unittest.TestCase, report_path: Path) -> None:
    """Assert the rendered markdown report contains the stable section/table
    anchors produced by `alphalens.backtest.report.write_markdown_report`.

    Structural asserts (not string-match on stdout) guard against silent
    truncation of the report while tolerating cosmetic wording changes in
    the HEADLINE echo.
    """
    test_case.assertTrue(report_path.exists(), msg=f"report not written: {report_path}")
    body = report_path.read_text()
    for marker in ("Sharpe (gross)", "## Decision criteria"):
        test_case.assertIn(marker, body, msg=f"missing {marker!r} in report body")


class TestBacktestCLIHelp(unittest.TestCase):
    """Catches import errors and Typer registration regressions."""

    def setUp(self):
        self.runner = CliRunner()

    def test_backtest_help_renders(self):
        """Assertions check each flag's presence in normalized output —
        Typer wraps long flag names mid-line on narrow CI terminals, so we
        strip ANSI escapes + collapse whitespace before substring search.
        """
        from alphalens_cli.main import app

        result = self.runner.invoke(app, ["backtest", "--help"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        normalized = _normalize_cli_output(result.output)
        for flag in ("--scorer", "--weighting", "--start", "--end", "--no-attrib", "--diagnose"):
            self.assertIn(flag, normalized, msg=f"flag {flag} missing from help output")


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

    def _run_momentum(self, extra_args: list[str] | None = None):
        from alphalens_cli.main import app

        with (
            patch(
                "alphalens.archive.screeners.themed.universe.load_universe",
                return_value={"ai": ["NVDA", "AMD"]},
            ),
            patch(
                "alphalens.archive.screeners.themed.universe.flatten_universe",
                return_value={"NVDA": ["ai"], "AMD": ["ai"]},
            ),
            patch(
                "alphalens.archive.screeners.themed.backtest_adapter.momentum_scorer_adapter",
                new=_fake_scorer,
            ),
            patch(
                "alphalens.archive.screeners.lean.lean_csv_loader.load_lean_histories",
                return_value=self._histories_for(["NVDA", "AMD", "SPY"]),
            ),
        ):
            return self.runner.invoke(
                app,
                [
                    "backtest",
                    "--start",
                    "2023-07-03",
                    "--end",
                    "2023-07-10",
                    "--no-attrib",
                    "--top-n",
                    "1",
                    "--report",
                    str(self.report_path),
                    *(extra_args or []),
                ],
            )

    def test_momentum_scorer_end_to_end(self):
        """`--scorer momentum` executes engine + report without touching
        real OHLCV data or factor files.
        """
        result = self._run_momentum()
        self.assertEqual(result.exit_code, 0, msg=result.output)
        _assert_report_has_structural_markers(self, self.report_path)

    def test_momentum_scorer_end_to_end_with_diagnose(self):
        """`--diagnose` pulls in a separate lazy-import chain
        (`diagnostics.format_vol_decomposition`, `ic_by_decile_from_scored_frames`,
        `tail_concentration_score`, `vol_decomposition_by_regime`). Exercise it
        with the same fake scorer so API drift in that chain is caught too.
        """
        result = self._run_momentum(extra_args=["--diagnose"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        _assert_report_has_structural_markers(self, self.report_path)

    def test_early_stage_scorer_end_to_end(self):
        """`--scorer early-stage` executes the Layer 2b themed pipeline with
        EarlyStageScorer (same universe as momentum, different scoring logic).
        """
        from alphalens_cli.main import app

        with (
            patch(
                "alphalens.archive.screeners.themed.universe.load_universe",
                return_value={"ai": ["NVDA", "AMD"]},
            ),
            patch(
                "alphalens.archive.screeners.themed.universe.flatten_universe",
                return_value={"NVDA": ["ai"], "AMD": ["ai"]},
            ),
            patch(
                "alphalens.archive.screeners.themed.backtest_adapter.early_stage_scorer_adapter",
                new=_fake_scorer,
            ),
            patch(
                "alphalens.archive.screeners.lean.lean_csv_loader.load_lean_histories",
                return_value=self._histories_for(["NVDA", "AMD", "SPY"]),
            ),
        ):
            result = self.runner.invoke(
                app,
                [
                    "backtest",
                    "--scorer",
                    "early-stage",
                    "--start",
                    "2023-07-03",
                    "--end",
                    "2023-07-10",
                    "--no-attrib",
                    "--top-n",
                    "1",
                    "--report",
                    str(self.report_path),
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        _assert_report_has_structural_markers(self, self.report_path)

    def test_lean_scorer_end_to_end(self):
        """`--scorer lean` executes the archived Layer 2c path the same way."""
        from alphalens_cli.main import app

        with (
            patch(
                "alphalens.archive.screeners.lean.universe.all_tickers",
                return_value=["NVDA", "AMD"],
            ),
            patch(
                "alphalens.archive.screeners.lean.lean_project.scorer.rank_universe",
                new=_fake_scorer,
            ),
            patch(
                "alphalens.archive.screeners.lean.lean_csv_loader.load_lean_histories",
                return_value=self._histories_for(["NVDA", "AMD", "SPY", "QQQ", "IWM"]),
            ),
        ):
            result = self.runner.invoke(
                app,
                [
                    "backtest",
                    "--scorer",
                    "lean",
                    "--start",
                    "2023-07-03",
                    "--end",
                    "2023-07-10",
                    "--no-attrib",
                    "--top-n",
                    "1",
                    "--report",
                    str(self.report_path),
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        _assert_report_has_structural_markers(self, self.report_path)

    def test_theme_mapping_fallback_when_universe_load_fails(self):
        """When `flatten_universe(load_universe())` raises, the command logs
        `theme mapping skipped` and continues (theme analysis is advisory,
        not required). Verify the fallback path exits 0 and emits the
        skip notice.
        """
        from alphalens_cli.main import app

        # For the momentum path, backtest_adapter pulls its own universe
        # config (THEMED_DEFAULTS + UNIVERSE_PATH), so we need the scorer
        # pass to succeed — patch the scorer and break the *second*
        # themed-universe import (the one used by theme_analysis).
        def _raise(*_args, **_kwargs):
            raise RuntimeError("simulated universe load failure")

        with (
            patch(
                "alphalens.archive.screeners.themed.universe.load_universe",
                side_effect=_raise,
            ),
            patch(
                "alphalens.archive.screeners.themed.universe.flatten_universe",
                side_effect=_raise,
            ),
            patch(
                "alphalens.archive.screeners.themed.backtest_adapter.momentum_scorer_adapter",
                new=_fake_scorer,
            ),
            patch(
                "alphalens.archive.screeners.lean.universe.all_tickers",
                return_value=["NVDA", "AMD"],
            ),
            patch(
                "alphalens.archive.screeners.lean.lean_project.scorer.rank_universe",
                new=_fake_scorer,
            ),
            patch(
                "alphalens.archive.screeners.lean.lean_csv_loader.load_lean_histories",
                return_value=self._histories_for(["NVDA", "AMD", "SPY", "QQQ", "IWM"]),
            ),
        ):
            # Use `--scorer lean` so momentum's universe loader is NOT called
            # for scorer configuration (only the theme_analysis post-hoc
            # block attempts the themed universe load — which is exactly
            # the branch we're testing).
            result = self.runner.invoke(
                app,
                [
                    "backtest",
                    "--scorer",
                    "lean",
                    "--start",
                    "2023-07-03",
                    "--end",
                    "2023-07-10",
                    "--no-attrib",
                    "--top-n",
                    "1",
                    "--report",
                    str(self.report_path),
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("theme mapping skipped", result.output)
        _assert_report_has_structural_markers(self, self.report_path)


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
                "--scorer",
                "bogus",
                "--start",
                "2023-07-03",
                "--end",
                "2023-07-10",
                "--no-attrib",
            ],
        )
        self.assertNotEqual(result.exit_code, 0)
        # Strip ANSI + collapse whitespace so the assertion survives Typer's
        # narrow-terminal wrapping (e.g. `--scorer` rendered as `-` `-scorer`).
        self.assertIn("Unknown --scorer", _normalize_cli_output(result.output))


class TestBacktestInsiderDispatch(unittest.TestCase):
    """Phase 3a: verify `--scorer insider` wires correctly and guards preconditions.

    Does not run a full insider backtest (requires Phase 2.5 PIT universe +
    yfinance prices). Only exercises the dispatch branch up to the point
    where missing inputs raise BadParameter with helpful guidance.
    """

    def setUp(self):
        self.runner = CliRunner()

    def test_rejects_when_data_files_absent(self):
        from alphalens_cli.main import app

        with patch("pathlib.Path.exists", return_value=False):
            result = self.runner.invoke(
                app,
                [
                    "backtest",
                    "--scorer",
                    "insider",
                    "--start",
                    "2023-07-03",
                    "--end",
                    "2023-07-10",
                    "--no-attrib",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn(
            "Missing",
            result.output + (str(result.exception) if result.exception else ""),
        )

    def test_rejects_when_user_agent_missing(self):
        from alphalens_cli.main import app

        # Make both data files appear to exist; lookups run before UA check.
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch(
                "alphalens.alt_data.russell_universe.load_iwm_current",
                return_value=["UPST", "SMCI"],
            ),
            patch(
                "alphalens.alt_data.ticker_cik_map.TickerCikMap.load",
            ),
            patch.dict("os.environ", {}, clear=True),
        ):
            result = self.runner.invoke(
                app,
                [
                    "backtest",
                    "--scorer",
                    "insider",
                    "--start",
                    "2023-07-03",
                    "--end",
                    "2023-07-10",
                    "--no-attrib",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn(
            "SEC_EDGAR_USER_AGENT",
            result.output + (str(result.exception) if result.exception else ""),
        )


if __name__ == "__main__":
    unittest.main()
