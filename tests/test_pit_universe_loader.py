"""PIT universe yaml-snapshot loader — TDD harness."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import yaml

from alphalens.data.alt_data.pit_universe_loader import (
    load_pit_universe_for_asof,
    load_universe_union,
)


def _write_snapshot(root: Path, year: int, month: int, tickers: list[str]) -> None:
    eom = date(year, month, 28)  # safe approximation; loader does not require EOM
    (root / f"{year:04d}-{month:02d}.yaml").write_text(
        yaml.safe_dump({"asof": eom.isoformat(), "tickers": tickers}, sort_keys=False)
    )


class TestLoadPitUniverseForAsof(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_returns_snapshot_when_asof_at_or_after_snapshot_date(self):
        # Snapshot helper writes asof=YYYY-MM-28. Load with asof on/after that.
        _write_snapshot(self.root, 2022, 6, ["AAPL", "MSFT"])
        tickers = load_pit_universe_for_asof(date(2022, 6, 30), root=self.root)
        self.assertEqual(tickers, ["AAPL", "MSFT"])

    def test_pit_discipline_falls_back_when_asof_precedes_snapshot(self):
        # Rebalance asof 2022-06-15 occurs before 2022-06-28 snapshot exists.
        # PIT discipline: cannot use forward-looking snapshot. Returns prior
        # month if available, else empty.
        _write_snapshot(self.root, 2022, 5, ["A"])
        _write_snapshot(self.root, 2022, 6, ["AAPL", "MSFT"])
        tickers = load_pit_universe_for_asof(date(2022, 6, 15), root=self.root)
        self.assertEqual(tickers, ["A"])

    def test_falls_back_to_most_recent_prior_snapshot(self):
        # asof Jul 5 — Jun snapshot is the most recent <= asof.
        _write_snapshot(self.root, 2022, 6, ["AAPL", "MSFT"])
        _write_snapshot(self.root, 2022, 8, ["NVDA", "AMD"])
        tickers = load_pit_universe_for_asof(date(2022, 7, 5), root=self.root)
        self.assertEqual(tickers, ["AAPL", "MSFT"])

    def test_returns_empty_when_no_snapshot_at_or_before_asof(self):
        _write_snapshot(self.root, 2022, 6, ["AAPL"])
        tickers = load_pit_universe_for_asof(date(2022, 1, 15), root=self.root)
        self.assertEqual(tickers, [])

    def test_empty_root_returns_empty_list(self):
        tickers = load_pit_universe_for_asof(date(2022, 6, 1), root=self.root)
        self.assertEqual(tickers, [])

    def test_handles_yaml_without_tickers_list(self):
        (self.root / "2022-06.yaml").write_text("asof: '2022-06-30'\n")
        tickers = load_pit_universe_for_asof(date(2022, 6, 15), root=self.root)
        self.assertEqual(tickers, [])


class TestLoadUniverseUnion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unions_all_snapshots_in_window(self):
        _write_snapshot(self.root, 2022, 1, ["A", "B"])
        _write_snapshot(self.root, 2022, 2, ["B", "C"])
        _write_snapshot(self.root, 2022, 3, ["C", "D"])
        result = load_universe_union(date(2022, 1, 1), date(2022, 3, 31), root=self.root)
        self.assertEqual(result, ["A", "B", "C", "D"])

    def test_excludes_snapshots_outside_window(self):
        _write_snapshot(self.root, 2021, 12, ["X"])
        _write_snapshot(self.root, 2022, 1, ["A"])
        _write_snapshot(self.root, 2022, 6, ["B"])
        _write_snapshot(self.root, 2023, 1, ["Y"])
        result = load_universe_union(date(2022, 1, 1), date(2022, 12, 31), root=self.root)
        self.assertEqual(result, ["A", "B"])

    def test_returns_empty_when_no_snapshots_in_window(self):
        _write_snapshot(self.root, 2020, 1, ["A"])
        result = load_universe_union(date(2022, 1, 1), date(2022, 12, 31), root=self.root)
        self.assertEqual(result, [])

    def test_deduplicates_tickers_seen_in_multiple_snapshots(self):
        _write_snapshot(self.root, 2022, 1, ["A", "B", "C"])
        _write_snapshot(self.root, 2022, 2, ["A", "B", "C"])
        result = load_universe_union(date(2022, 1, 1), date(2022, 2, 28), root=self.root)
        self.assertEqual(result, ["A", "B", "C"])


if __name__ == "__main__":
    unittest.main()
