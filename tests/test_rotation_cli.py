"""CLI smoke tests for `alphalens rotation ...`."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
from typer.testing import CliRunner

from alphalens.data.macro.signals import SignalSet
from alphalens.data.store.history import HistoryStore


def _ohlcv(n_bars: int = 520, daily_ret: float = 0.0003):
    idx = pd.date_range("2018-01-02", periods=n_bars, freq="B")
    close = (1.0 + daily_ret) ** np.arange(n_bars) * 100.0
    return pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": np.full(n_bars, 1e6),
        },
        index=idx,
    )


def _fake_rotation_data():
    store = HistoryStore(
        {
            "SPY": _ohlcv(daily_ret=0.0004),
            "QQQ": _ohlcv(daily_ret=0.0006),
            "IWM": _ohlcv(daily_ret=0.0002),
        }
    )
    idx = store.full("SPY").index
    signals = SignalSet(
        yield_curve_slope=pd.Series(1.5, index=idx),
        vix_decile=pd.Series(0.5, index=idx),
        qqq_iwm_spread=pd.Series(0.0, index=idx),
    )
    return store, signals


_MINIMAL_CONFIG = """
core_weights:
  SPY: 0.60
  QQQ: 0.30
  IWM: 0.10
max_tilt: 0.10
rebalance_stride: 63
etf_spread_bps: {SPY: 1.0, QQQ: 2.0, IWM: 3.0}
rules:
  - name: yield_steep
    signal: yield_curve_slope
    operator: gt
    threshold: 1.0
    tilt: {QQQ: 0.05, SPY: -0.05}
gates:
  rolling_sharpe_min: 0.30
  carhart_oos_t_min: 1.50
"""


class TestRotationBacktest(unittest.TestCase):
    def test_backtest_command_runs_and_writes_report(self):
        from alphalens_cli.main import app

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "rotation.yaml"
            cfg_path.write_text(_MINIMAL_CONFIG)
            out_path = Path(tmp) / "report.md"

            store, signals = _fake_rotation_data()
            with (
                patch(
                    "alphalens_cli.commands.rotation.load_rotation_data",
                    return_value=(store, signals),
                ),
                patch(
                    "alphalens.archive.rotation.config.capture_git_sha",
                    return_value="deadbeef" * 5,
                ),
            ):
                result = runner.invoke(
                    app,
                    [
                        "rotation",
                        "backtest",
                        "--config",
                        str(cfg_path),
                        "--start",
                        "2019-01-02",
                        "--end",
                        "2019-12-31",
                        "--output",
                        str(out_path),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=f"stdout: {result.stdout}")
            self.assertTrue(out_path.exists())
            content = out_path.read_text()
            self.assertIn("Sharpe", content)
            self.assertIn("rebalances", content.lower())


class TestRotationRun(unittest.TestCase):
    def test_run_prints_current_weights(self):
        from alphalens_cli.main import app

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "rotation.yaml"
            cfg_path.write_text(_MINIMAL_CONFIG)

            store, signals = _fake_rotation_data()
            with patch(
                "alphalens_cli.commands.rotation.load_rotation_data",
                return_value=(store, signals),
            ):
                result = runner.invoke(app, ["rotation", "run", "--config", str(cfg_path)])

        self.assertEqual(result.exit_code, 0, msg=f"stdout: {result.stdout}")
        # Signal slope is 1.5 (> 1.0) → yield_steep fires → QQQ +0.05, SPY -0.05
        self.assertIn("SPY", result.stdout)
        self.assertIn("QQQ", result.stdout)
        self.assertIn("IWM", result.stdout)


class TestRotationStatus(unittest.TestCase):
    def test_status_prints_config_fingerprint(self):
        from alphalens_cli.main import app

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "rotation.yaml"
            cfg_path.write_text(_MINIMAL_CONFIG)

            with patch(
                "alphalens.archive.rotation.config.capture_git_sha",
                return_value="b" * 40,
            ):
                result = runner.invoke(app, ["rotation", "status", "--config", str(cfg_path)])

        self.assertEqual(result.exit_code, 0, msg=f"stdout: {result.stdout}")
        self.assertIn("bbbbbbbb", result.stdout)  # git SHA fragment
        self.assertIn("rotation.yaml", result.stdout)


class TestRotationSanityCheck(unittest.TestCase):
    def test_sanity_check_command_runs_all_four_gates(self):
        from alphalens_cli.main import app

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "rotation.yaml"
            cfg_path.write_text(_MINIMAL_CONFIG)

            store, signals = _fake_rotation_data()
            with patch(
                "alphalens_cli.commands.rotation.load_rotation_data",
                return_value=(store, signals),
            ):
                result = runner.invoke(
                    app,
                    [
                        "rotation",
                        "sanity-check",
                        "--config",
                        str(cfg_path),
                        "--start",
                        "2019-01-02",
                        "--end",
                        "2019-12-31",
                    ],
                )

        self.assertEqual(
            result.exit_code, 1, msg=f"stdout: {result.stdout}"
        )  # exit=1 since constant-trending fake data has corr≈1 with passive
        # All 4 gate names printed
        for gate_name in (
            "passive_correlation",
            "rolling_sharpe_stability",
            "per_regime_vs_passive",
            "overlay_alpha",
        ):
            self.assertIn(gate_name, result.stdout)
        # Verdict line
        self.assertIn("VERDICT", result.stdout.upper())

    def test_sanity_check_exit_zero_when_all_gates_pass(self):
        """When we construct data where every gate passes, CLI returns 0."""
        from alphalens.archive.rotation.sanity_checks import (
            SanityCheckReport,
            SanityCheckResult,
        )
        from alphalens_cli.main import app

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "rotation.yaml"
            cfg_path.write_text(_MINIMAL_CONFIG)
            store, signals = _fake_rotation_data()

            all_pass = SanityCheckReport(
                checks=tuple(
                    SanityCheckResult(name=n, passed=True, value=0.5, threshold=0.5, detail="ok")
                    for n in (
                        "passive_correlation",
                        "rolling_sharpe_stability",
                        "per_regime_vs_passive",
                        "overlay_alpha",
                    )
                )
            )
            with (
                patch(
                    "alphalens_cli.commands.rotation.load_rotation_data",
                    return_value=(store, signals),
                ),
                patch(
                    "alphalens_cli.commands.rotation.run_all_sanity_checks",
                    return_value=all_pass,
                ),
            ):
                result = runner.invoke(
                    app,
                    [
                        "rotation",
                        "sanity-check",
                        "--config",
                        str(cfg_path),
                        "--start",
                        "2019-01-02",
                        "--end",
                        "2019-12-31",
                    ],
                )

        self.assertEqual(result.exit_code, 0, msg=f"stdout: {result.stdout}")
        self.assertIn("PASS", result.stdout.upper())


if __name__ == "__main__":
    unittest.main()
