"""CLI tests for `alphalens buffett lens` (Mode-A lens, #511).

Exercises the command body via typer's CliRunner against the ROOT app
(`alphalens buffett lens ...`, mirroring the real `add_typer` registration)
with `build_comparison` monkeypatched, so no network / store fetch happens:
the store + yfinance singleton are constructed (cheap, no I/O) but the patched
assembler returns fixed panels. Covers the table render, the `--out` parquet
write, the no-candidates path, and the bad-date guard.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import alphalens_pipeline.buffett.comparison as comparison_mod
from alphalens_cli.commands.buffett import _fmt_num, _format_table
from alphalens_cli.main import app
from alphalens_pipeline.buffett.comparison import BuffettPanel
from typer.testing import CliRunner


def _panel(ticker: str, **kw) -> BuffettPanel:
    base: dict = {
        "ticker": ticker,
        "theme": "AI infrastructure",
        "market_cap": 1.0e9,
        "owner_earnings_latest": 5.0e7,
        "owner_earnings_yield_pct": 5.0,
        "roic_latest": 18.0,
        "roic_3y_avg": 16.0,
        "op_margin_latest": 22.0,
        "op_margin_3y_avg": 20.0,
        "intrinsic_value_per_share": 120.0,
        "margin_of_safety_pct": 12.0,
        "buyback_pct": -1.5,
        "net_buyback": True,
        "dividend_yield_pct": 1.2,
        "data_coverage": 1.0,
    }
    base.update(kw)
    return BuffettPanel(**base)


class TestFormatTable(unittest.TestCase):
    def test_headers_and_none_dash(self):
        table = _format_table([_panel("AAPL", roic_latest=None)])
        self.assertIn("TICKER", table)
        self.assertIn("AAPL", table)
        # A None metric renders as the dash sentinel, never a fabricated 0.
        self.assertIn("-", table)

    def test_fmt_num_none_and_decimals(self):
        self.assertEqual(_fmt_num(None), "-")
        self.assertEqual(_fmt_num(3.14159, decimals=2), "3.14")


class TestLensCommand(unittest.TestCase):
    def setUp(self):
        self._runner = CliRunner()
        self._original = comparison_mod.build_comparison

    def tearDown(self):
        comparison_mod.build_comparison = self._original  # type: ignore[assignment]

    def _patch_panels(self, panels: list[BuffettPanel]) -> None:
        comparison_mod.build_comparison = lambda *_a, **_k: panels  # type: ignore[assignment]

    def test_prints_table_and_writes_parquet(self):
        self._patch_panels([_panel("AAPL"), _panel("MSFT", data_coverage=0.5)])
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "lens.parquet"
            result = self._runner.invoke(
                app,
                ["buffett", "lens", "2026-06-10", "--briefs-dir", tmp, "--out", str(out)],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Buffett lens (Mode A)", result.output)
            self.assertIn("AAPL", result.output)
            self.assertTrue(out.exists())

    def test_no_candidates_message(self):
        self._patch_panels([])
        with TemporaryDirectory() as tmp:
            result = self._runner.invoke(
                app, ["buffett", "lens", "2026-06-10", "--briefs-dir", tmp]
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("No candidates", result.output)

    def test_bad_date_is_rejected(self):
        result = self._runner.invoke(app, ["buffett", "lens", "not-a-date"])
        self.assertNotEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
