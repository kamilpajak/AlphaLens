"""Tests for ``alphalens.paper_trade.ledger`` append-only ledger."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from alphalens.paper_trade.ledger import (
    LEDGER_COLUMNS,
    LedgerEntry,
    LedgerError,
    append_ledger_entry,
    default_ledger_path,
    load_ledger,
)


def _make_entry(asof: date, n: int, **overrides):
    base = {
        "asof": asof,
        "rebalance_n": n,
        "n_held": 10,
        "holdings": ["AAPL", "MSFT"],
        "prior_holdings": ["GOOG"],
        "realized_return_long_gross": 0.012,
        "realized_return_long_net": 0.011,
        "benchmark_return_mdy": 0.005,
        "cost_drag_bps": 30.0,
        "universe_size": 1500,
    }
    base.update(overrides)
    return LedgerEntry(**base)


class DefaultPathTests(unittest.TestCase):
    def test_default_path(self):
        p = default_ledger_path("v9d")
        self.assertEqual(p.name, "v9d_ledger.parquet")

    def test_default_path_unknown_strategy_raises(self):
        with self.assertRaises(KeyError):
            default_ledger_path("nonexistent")


class LoadLedgerTests(unittest.TestCase):
    def test_load_missing_file_returns_empty_frame_with_schema(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.parquet"
            df = load_ledger(path)
            self.assertTrue(df.empty)
            self.assertEqual(set(df.columns), set(LEDGER_COLUMNS))

    def test_load_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.parquet"
            entry = _make_entry(date(2026, 5, 4), 1)
            append_ledger_entry(entry, path)
            df = load_ledger(path)
            self.assertEqual(len(df), 1)
            self.assertEqual(df.iloc[0]["asof"], "2026-05-04")
            self.assertEqual(df.iloc[0]["rebalance_n"], 1)
            # parquet loads list columns as numpy arrays; convert for comparison
            self.assertEqual(list(df.iloc[0]["holdings"]), ["AAPL", "MSFT"])

    def test_load_rejects_schema_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.parquet"
            pd.DataFrame({"asof": ["2026-05-04"], "wrong_col": [1]}).to_parquet(path)
            with self.assertRaises(LedgerError):
                load_ledger(path)


class AppendInvariantTests(unittest.TestCase):
    def test_append_two_distinct_asofs(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.parquet"
            append_ledger_entry(_make_entry(date(2026, 5, 4), 1), path)
            df = append_ledger_entry(_make_entry(date(2026, 5, 11), 2), path)
            self.assertEqual(len(df), 2)
            self.assertEqual(list(df["asof"]), ["2026-05-04", "2026-05-11"])

    def test_append_duplicate_asof_raises(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.parquet"
            append_ledger_entry(_make_entry(date(2026, 5, 4), 1), path)
            with self.assertRaises(LedgerError):
                append_ledger_entry(_make_entry(date(2026, 5, 4), 2), path)

    def test_append_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "subdir" / "ledger.parquet"
            self.assertFalse(path.parent.exists())
            append_ledger_entry(_make_entry(date(2026, 5, 4), 1), path)
            self.assertTrue(path.exists())

    def test_append_sorts_by_asof(self):
        """Appending an out-of-order asof must result in chronologically
        sorted output (defensive: we never want sort-by-write-time bugs)."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.parquet"
            append_ledger_entry(_make_entry(date(2026, 5, 11), 2), path)
            df = append_ledger_entry(_make_entry(date(2026, 5, 4), 1), path)
            self.assertEqual(list(df["asof"]), ["2026-05-04", "2026-05-11"])


class LedgerEntryTests(unittest.TestCase):
    def test_holdings_sorted_post_init(self):
        e = _make_entry(
            date(2026, 5, 4),
            1,
            holdings=["MSFT", "AAPL"],
            prior_holdings=["GOOG", "AMZN"],
        )
        self.assertEqual(e.holdings, ["AAPL", "MSFT"])
        self.assertEqual(e.prior_holdings, ["AMZN", "GOOG"])

    def test_to_row_serializes_asof_to_iso(self):
        e = _make_entry(date(2026, 5, 4), 1)
        row = e.to_row()
        self.assertEqual(row["asof"], "2026-05-04")
        self.assertIsInstance(row["holdings"], list)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
