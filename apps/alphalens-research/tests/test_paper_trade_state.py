"""Tests for ``alphalens_research.paper_trade.state.PaperTradeState``."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from alphalens_research.paper_trade.state import PaperTradeState, default_state_path


class DefaultPathTests(unittest.TestCase):
    def test_default_path_under_alphalens_home(self):
        p = default_state_path("v9d")
        self.assertEqual(p.name, "v9d_state.yaml")
        self.assertIn(".alphalens", str(p))

    def test_default_path_unknown_strategy_raises(self):
        with self.assertRaises(KeyError):
            default_state_path("nonexistent")


class LoadSaveRoundTripTests(unittest.TestCase):
    def test_empty_state_when_file_missing(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nonexistent.yaml"
            s = PaperTradeState.load(path)
            self.assertEqual(s.held, [])
            self.assertEqual(s.scores, {})
            self.assertIsNone(s.as_of)
            self.assertEqual(s.rebalance_n, 0)

    def test_save_then_load_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.yaml"
            original = PaperTradeState(
                held=["AAPL", "MSFT"],
                scores={"AAPL": 0.13, "MSFT": -0.05},
                as_of=date(2026, 5, 4),
                rebalance_n=1,
            )
            original.save(path)

            reloaded = PaperTradeState.load(path)
            self.assertEqual(reloaded.held, ["AAPL", "MSFT"])
            self.assertEqual(reloaded.scores, {"AAPL": 0.13, "MSFT": -0.05})
            self.assertEqual(reloaded.as_of, date(2026, 5, 4))
            self.assertEqual(reloaded.rebalance_n, 1)

    def test_save_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "subdir1" / "subdir2" / "state.yaml"
            self.assertFalse(path.parent.exists())
            PaperTradeState(held=["X"], as_of=date(2026, 5, 4)).save(path)
            self.assertTrue(path.exists())

    def test_load_handles_yaml_date_object(self):
        """yaml.safe_load may return a ``date`` directly (not str) for ISO-format
        dates without quotes. The loader must accept either."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.yaml"
            path.write_text(
                "held: [AAPL]\n"
                "scores: {AAPL: 0.1}\n"
                "as_of: 2026-05-04\n"  # yaml will parse this as a date object
                "rebalance_n: 0\n"
            )
            s = PaperTradeState.load(path)
            self.assertEqual(s.as_of, date(2026, 5, 4))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
