"""Tests for S&P 1500 PIT universe loader (event_drift v4 dependency)."""

from __future__ import annotations

import contextlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd
import yaml

from alphalens.data.universes.sp1500_pit import (
    UniverseError,
    load_sp400_pit_for_date,
    load_sp500_pit_for_date,
    load_sp600_pit_for_date,
    load_sp1500_pit_for_date,
)


def _write_snapshot(path: Path, *, as_of: str, tickers: list[str], source: str = "test") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "as_of": as_of,
                "source": source,
                "tickers": tickers,
            },
            sort_keys=False,
        )
    )


class _LoaderForDateContract:
    """Shared behaviours for sp500/sp400/sp600 _for_date loaders.

    Mixin only — concrete subclasses below also inherit ``unittest.TestCase``
    so each loader's contract runs once. No bare-mixin discovery occurs.
    """

    loader = staticmethod(load_sp500_pit_for_date)
    subdir = "sp500_pit"

    def test_picks_latest_snapshot_le_asof(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / self.subdir
            _write_snapshot(base / "2018.yaml", as_of="2018-01-01", tickers=["A", "B"])
            _write_snapshot(base / "2020.yaml", as_of="2020-01-01", tickers=["C", "D"])
            _write_snapshot(base / "2024.yaml", as_of="2024-01-01", tickers=["E", "F"])

            tickers = self.loader(pd.Timestamp("2022-06-15"), data_dir=base)
            self.assertEqual(tickers, ["C", "D"])

            tickers = self.loader(pd.Timestamp("2024-01-01"), data_dir=base)
            self.assertEqual(tickers, ["E", "F"])

            tickers = self.loader(pd.Timestamp("2026-04-30"), data_dir=base)
            self.assertEqual(tickers, ["E", "F"])

    def test_raises_when_asof_before_all_snapshots(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / self.subdir
            _write_snapshot(base / "2024.yaml", as_of="2024-01-01", tickers=["X"])

            with self.assertRaises(UniverseError):
                self.loader(pd.Timestamp("2020-01-01"), data_dir=base)

    def test_raises_when_directory_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / self.subdir
            with self.assertRaises(UniverseError):
                self.loader(pd.Timestamp("2024-01-01"), data_dir=base)

    def test_uppercases_tickers(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / self.subdir
            _write_snapshot(base / "2024.yaml", as_of="2024-01-01", tickers=["aapl", "msft"])

            tickers = self.loader(pd.Timestamp("2024-06-30"), data_dir=base)
            self.assertEqual(tickers, ["AAPL", "MSFT"])

    def test_skips_yaml_without_as_of(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / self.subdir
            base.mkdir(parents=True, exist_ok=True)
            (base / "bad.yaml").write_text(yaml.safe_dump({"tickers": ["X"]}, sort_keys=False))
            _write_snapshot(base / "2024.yaml", as_of="2024-01-01", tickers=["A"])

            tickers = self.loader(pd.Timestamp("2024-06-30"), data_dir=base)
            self.assertEqual(tickers, ["A"])


class TestLoadSp500ForDate(_LoaderForDateContract, unittest.TestCase):
    loader = staticmethod(load_sp500_pit_for_date)
    subdir = "sp500_pit"


class TestLoadSp400ForDate(_LoaderForDateContract, unittest.TestCase):
    loader = staticmethod(load_sp400_pit_for_date)
    subdir = "sp400_pit"


class TestLoadSp600ForDate(_LoaderForDateContract, unittest.TestCase):
    loader = staticmethod(load_sp600_pit_for_date)
    subdir = "sp600_pit"


class TestLoadSp1500ForDate(unittest.TestCase):
    def test_returns_sorted_union_of_three_indices(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(
                root / "sp500_pit" / "2024.yaml", as_of="2024-01-01", tickers=["AAPL", "MSFT"]
            )
            _write_snapshot(
                root / "sp400_pit" / "2024.yaml", as_of="2024-01-01", tickers=["TPL", "WAL"]
            )
            _write_snapshot(
                root / "sp600_pit" / "2024.yaml", as_of="2024-01-01", tickers=["INSW", "CRC"]
            )

            tickers = load_sp1500_pit_for_date(pd.Timestamp("2024-06-30"), data_root=root)
            self.assertEqual(tickers, ["AAPL", "CRC", "INSW", "MSFT", "TPL", "WAL"])

    def test_dedupes_overlapping_tickers_across_indices(self) -> None:
        # Defensive: although S&P 500/400/600 are mutually exclusive at any
        # point in time, a stale snapshot pair could overlap during transitions.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(root / "sp500_pit" / "2024.yaml", as_of="2024-01-01", tickers=["AAPL"])
            _write_snapshot(
                root / "sp400_pit" / "2024.yaml", as_of="2024-01-01", tickers=["AAPL", "TPL"]
            )
            _write_snapshot(root / "sp600_pit" / "2024.yaml", as_of="2024-01-01", tickers=["INSW"])

            tickers = load_sp1500_pit_for_date(pd.Timestamp("2024-06-30"), data_root=root)
            self.assertEqual(tickers, ["AAPL", "INSW", "TPL"])

    def test_raises_if_any_index_snapshot_unavailable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_snapshot(root / "sp500_pit" / "2024.yaml", as_of="2024-01-01", tickers=["AAPL"])
            _write_snapshot(root / "sp400_pit" / "2024.yaml", as_of="2024-01-01", tickers=["TPL"])
            # sp600_pit missing entirely

            with self.assertRaises(UniverseError):
                load_sp1500_pit_for_date(pd.Timestamp("2024-06-30"), data_root=root)

    def test_uses_repo_default_data_root_when_none(self) -> None:
        # Smoke: when called with no data_root, falls back to repo default.
        # This test only verifies the function does not crash on signature;
        # actual content depends on real snapshots which Phase 1c populates.
        with contextlib.suppress(UniverseError):
            load_sp1500_pit_for_date(pd.Timestamp("2024-06-30"))


class TestRealRepoSnapshotsLoad(unittest.TestCase):
    """Integration smoke: real on-disk snapshots load + produce non-empty union."""

    def test_real_2024_snapshots_produce_sp1500_universe(self) -> None:
        try:
            tickers = load_sp1500_pit_for_date(pd.Timestamp("2024-06-30"))
        except UniverseError as exc:
            self.skipTest(f"Real snapshots not yet populated: {exc}")

        # Sanity: S&P 1500 should have ~1500 tickers.
        self.assertGreater(len(tickers), 1000, "S&P 1500 should have >1000 names")
        self.assertLess(len(tickers), 1700, "S&P 1500 should have <1700 names")
        # All upper-case + sorted + deduped
        self.assertEqual(tickers, sorted(set(tickers)))
        for t in tickers[:10]:
            self.assertEqual(t, t.upper())


if __name__ == "__main__":
    unittest.main()
