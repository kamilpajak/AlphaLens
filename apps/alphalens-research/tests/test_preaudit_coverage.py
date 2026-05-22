"""Coverage-check unit tests for the pre-audit smoke framework.

Pure-function TDD harness: synthesise tiny fixture dirs that mimic
each :class:`CheckType` and assert :func:`check_coverage` returns the
right :class:`CoverageStatus`. Real `~/.alphalens/` data is NOT touched.
"""

from __future__ import annotations

import random
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from alphalens_research.preaudit.coverage import check_all_deps, check_coverage
from alphalens_research.preaudit.profiles import (
    CheckType,
    CoverageStatus,
    DataDep,
    SmokeProfile,
)


def _write_flat_parquet(
    dir_path: Path, ticker: str, dates: list[date], date_col: str = "date"
) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({date_col: pd.to_datetime(dates), "close": range(len(dates))})
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), dir_path / f"{ticker}.parquet")


def _write_partition_dir(root: Path, partition_key: str, years: list[int]) -> None:
    for y in years:
        sub = root / f"{partition_key}={y}"
        sub.mkdir(parents=True, exist_ok=True)
        # one tiny parquet inside so the dir is non-empty
        df = pd.DataFrame({"x": [1]})
        pq.write_table(pa.Table.from_pandas(df, preserve_index=False), sub / "part-0.parquet")


class TestCheckCoverageExistsNonempty(unittest.TestCase):
    """``CheckType.EXISTS_NONEMPTY`` — minimal existence-only check."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_dir_returns_fail_missing(self):
        dep = DataDep(name="ghost", check_type=CheckType.EXISTS_NONEMPTY)
        result = check_coverage(dep, root=self.root)
        self.assertEqual(result.status, CoverageStatus.FAIL_MISSING)

    def test_empty_dir_returns_fail_empty(self):
        (self.root / "empty").mkdir()
        dep = DataDep(name="empty", check_type=CheckType.EXISTS_NONEMPTY)
        result = check_coverage(dep, root=self.root)
        self.assertEqual(result.status, CoverageStatus.FAIL_EMPTY)

    def test_nonempty_dir_returns_pass(self):
        (self.root / "ok").mkdir()
        (self.root / "ok" / "any.txt").write_text("data")
        dep = DataDep(name="ok", check_type=CheckType.EXISTS_NONEMPTY)
        result = check_coverage(dep, root=self.root)
        self.assertEqual(result.status, CoverageStatus.PASS)


class TestCheckCoverageParquetPartition(unittest.TestCase):
    """``CheckType.PARQUET_PARTITION`` — Form-4 ``transaction_year=NNNN``."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dep = DataDep(
            name="form4_parquet",
            check_type=CheckType.PARQUET_PARTITION,
            min_date=date(2018, 1, 1),
            max_date=date(2020, 12, 31),
            pattern="transaction_year",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_required_years_present_returns_pass(self):
        _write_partition_dir(
            self.root / "form4_parquet", "transaction_year", [2017, 2018, 2019, 2020]
        )
        result = check_coverage(self.dep, root=self.root)
        self.assertEqual(result.status, CoverageStatus.PASS)

    def test_missing_year_returns_fail_gap(self):
        # 2019 missing — window 2018-2020 requires it
        _write_partition_dir(self.root / "form4_parquet", "transaction_year", [2018, 2020])
        result = check_coverage(self.dep, root=self.root)
        self.assertEqual(result.status, CoverageStatus.FAIL_GAP)
        self.assertIn("2019", result.detail)

    def test_missing_dir_returns_fail_missing(self):
        # don't create form4_parquet at all
        result = check_coverage(self.dep, root=self.root)
        self.assertEqual(result.status, CoverageStatus.FAIL_MISSING)

    def test_no_partitions_returns_fail_empty(self):
        (self.root / "form4_parquet").mkdir()
        result = check_coverage(self.dep, root=self.root)
        self.assertEqual(result.status, CoverageStatus.FAIL_EMPTY)


class TestCheckCoverageFlatParquet(unittest.TestCase):
    """``CheckType.FLAT_PARQUET`` — one parquet per ticker (iVolatility SMD).

    The critical correctness property is MULTI-TICKER sampling: a single
    long-history ticker (AAPL) must not be allowed to false-pass when
    most of the universe has shorter history.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dir = self.root / "ivolatility_smd"
        # Deterministic random sample across runs.
        random.seed(0)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_dep(self, sample_size: int = 10) -> DataDep:
        return DataDep(
            name="ivolatility_smd",
            check_type=CheckType.FLAT_PARQUET,
            min_date=date(2018, 1, 1),
            max_date=date(2020, 12, 31),
            sample_size=sample_size,
            pattern="tradeDate",
        )

    def test_all_sampled_tickers_cover_window_returns_pass(self):
        # 50 tickers, ALL with the full window covered.
        dates = pd.date_range("2017-01-01", "2021-12-31").date.tolist()
        for i in range(50):
            _write_flat_parquet(self.dir, f"TKR{i:03d}", dates, date_col="tradeDate")
        result = check_coverage(self._make_dep(), root=self.root)
        self.assertEqual(result.status, CoverageStatus.PASS)

    def test_majority_short_history_returns_fail_gap(self):
        # Mimics today's bug: AAPL has pre-2018 data but most tickers don't.
        # 1 long-history, 49 short-history. Sample 10 → expected ratio
        # of pass ~ 20% (well below default 70%) → FAIL_GAP. Catches
        # "environment is missing the bulk of pre-window data".
        full_dates = pd.date_range("2017-01-01", "2021-12-31").date.tolist()
        short_dates = pd.date_range("2018-08-01", "2021-12-31").date.tolist()
        _write_flat_parquet(self.dir, "AAPL", full_dates, date_col="tradeDate")
        for i in range(49):
            _write_flat_parquet(self.dir, f"TKR{i:03d}", short_dates, date_col="tradeDate")
        result = check_coverage(self._make_dep(sample_size=10), root=self.root)
        self.assertEqual(result.status, CoverageStatus.FAIL_GAP)
        self.assertIn("2018", result.detail)

    def test_minority_short_history_tolerated_returns_pass(self):
        # Realistic universe shape: 80% mature, 20% recent IPOs.
        # Sample 10 with default min_pass_ratio=0.7 → ~7-8 pass on avg →
        # check PASSES. Catches today's full-environment-missing bug
        # without false-failing on routine recent-IPO sprinkling.
        full_dates = pd.date_range("2017-01-01", "2021-12-31").date.tolist()
        ipo_dates = pd.date_range("2020-06-01", "2021-12-31").date.tolist()
        for i in range(80):
            _write_flat_parquet(self.dir, f"OLD{i:03d}", full_dates, date_col="tradeDate")
        for i in range(20):
            _write_flat_parquet(self.dir, f"IPO{i:03d}", ipo_dates, date_col="tradeDate")
        # Deterministic via seed=0 set in setUp. With 80/100 mature tickers,
        # default 0.7 ratio should pass with overwhelming probability.
        result = check_coverage(self._make_dep(sample_size=10), root=self.root)
        self.assertEqual(result.status, CoverageStatus.PASS)

    def test_strict_min_pass_ratio_1_still_supported(self):
        # Caller can opt into strict "ALL must span" via min_pass_ratio=1.0.
        full_dates = pd.date_range("2017-01-01", "2021-12-31").date.tolist()
        ipo_dates = pd.date_range("2020-06-01", "2021-12-31").date.tolist()
        for i in range(8):
            _write_flat_parquet(self.dir, f"OLD{i:03d}", full_dates, date_col="tradeDate")
        for i in range(2):
            _write_flat_parquet(self.dir, f"IPO{i:03d}", ipo_dates, date_col="tradeDate")
        strict_dep = DataDep(
            name="ivolatility_smd",
            check_type=CheckType.FLAT_PARQUET,
            min_date=date(2018, 1, 1),
            max_date=date(2020, 12, 31),
            sample_size=10,
            min_pass_ratio=1.0,
            pattern="tradeDate",
        )
        result = check_coverage(strict_dep, root=self.root)
        self.assertEqual(result.status, CoverageStatus.FAIL_GAP)

    def test_sample_size_clamped_to_available(self):
        # Only 3 tickers exist; sample_size=10 should clamp, not crash.
        dates = pd.date_range("2017-01-01", "2021-12-31").date.tolist()
        for t in ("AAA", "BBB", "CCC"):
            _write_flat_parquet(self.dir, t, dates, date_col="tradeDate")
        result = check_coverage(self._make_dep(sample_size=10), root=self.root)
        self.assertEqual(result.status, CoverageStatus.PASS)

    def test_empty_dir_returns_fail_empty(self):
        self.dir.mkdir()
        result = check_coverage(self._make_dep(), root=self.root)
        self.assertEqual(result.status, CoverageStatus.FAIL_EMPTY)

    def test_missing_dir_returns_fail_missing(self):
        result = check_coverage(self._make_dep(), root=self.root)
        self.assertEqual(result.status, CoverageStatus.FAIL_MISSING)

    def test_falls_back_to_datetime_index_when_column_missing(self):
        # OHLCV-style files store the date on a DatetimeIndex, not as a
        # data column. Coverage check should still work in that case
        # via _peek_dates fallback (real-world: ~/.alphalens/prices).
        idx = pd.date_range("2017-06-01", "2021-12-31", freq="B")
        for t in ("AAA", "BBB", "CCC"):
            df = pd.DataFrame({"open": 1.0, "close": 2.0}, index=idx)
            df.index.name = "Date"
            self.dir.mkdir(parents=True, exist_ok=True)
            df.to_parquet(self.dir / f"{t}.parquet")
        dep = DataDep(
            name="ivolatility_smd",
            check_type=CheckType.FLAT_PARQUET,
            min_date=date(2018, 1, 1),
            max_date=date(2020, 12, 31),
            pattern="ColumnThatDoesntExist",  # forces fallback to index
            sample_size=3,
        )
        result = check_coverage(dep, root=self.root)
        self.assertEqual(result.status, CoverageStatus.PASS)


class TestCheckCoverageYamlDir(unittest.TestCase):
    """``CheckType.YAML_DIR`` — filename-encoded dates (PIT universe)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dir = self.root / "pit_universe"
        self.dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _make_dep(self) -> DataDep:
        return DataDep(
            name="pit_universe",
            check_type=CheckType.YAML_DIR,
            min_date=date(2018, 6, 30),
            max_date=date(2020, 6, 30),
            pattern=r"r2000_pit_(\d{4}-\d{2}-\d{2})\.yaml",
        )

    def test_window_straddled_by_filenames_returns_pass(self):
        for d in ("2018-03-31", "2018-06-30", "2019-12-31", "2020-06-30", "2021-03-31"):
            (self.dir / f"r2000_pit_{d}.yaml").write_text("foo: bar")
        result = check_coverage(self._make_dep(), root=self.root)
        self.assertEqual(result.status, CoverageStatus.PASS)

    def test_filenames_all_after_window_start_returns_fail_gap(self):
        for d in ("2019-12-31", "2020-06-30"):
            (self.dir / f"r2000_pit_{d}.yaml").write_text("foo: bar")
        result = check_coverage(self._make_dep(), root=self.root)
        self.assertEqual(result.status, CoverageStatus.FAIL_GAP)


class TestCheckAllDeps(unittest.TestCase):
    """Aggregate ``check_all_deps`` returns CoverageReport across the profile."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_pass_yields_passed_report(self):
        dates = pd.date_range("2018-01-01", "2020-12-31").date.tolist()
        _write_flat_parquet(self.root / "prices", "TKR1", dates)
        _write_partition_dir(self.root / "form4_parquet", "transaction_year", [2018, 2019, 2020])
        profile = SmokeProfile(
            strategy="dummy",
            smoke_window=(date(2018, 1, 1), date(2020, 12, 31)),
            extra_args=(),
            data_deps=(
                DataDep(
                    name="prices",
                    check_type=CheckType.FLAT_PARQUET,
                    min_date=date(2018, 1, 1),
                    max_date=date(2020, 12, 31),
                    pattern="date",
                ),
                DataDep(
                    name="form4_parquet",
                    check_type=CheckType.PARQUET_PARTITION,
                    min_date=date(2018, 1, 1),
                    max_date=date(2020, 12, 31),
                    pattern="transaction_year",
                ),
            ),
        )
        report = check_all_deps(profile, root=self.root)
        self.assertTrue(report.passed)
        self.assertEqual(len(report.checks), 2)

    def test_one_failure_yields_failed_report_with_detail(self):
        # form4 missing entirely
        dates = pd.date_range("2018-01-01", "2020-12-31").date.tolist()
        _write_flat_parquet(self.root / "prices", "TKR1", dates)
        profile = SmokeProfile(
            strategy="dummy",
            smoke_window=(date(2018, 1, 1), date(2020, 12, 31)),
            extra_args=(),
            data_deps=(
                DataDep(
                    name="prices",
                    check_type=CheckType.FLAT_PARQUET,
                    min_date=date(2018, 1, 1),
                    max_date=date(2020, 12, 31),
                    pattern="date",
                ),
                DataDep(
                    name="form4_parquet",
                    check_type=CheckType.PARQUET_PARTITION,
                    min_date=date(2018, 1, 1),
                    max_date=date(2020, 12, 31),
                    pattern="transaction_year",
                ),
            ),
        )
        report = check_all_deps(profile, root=self.root)
        self.assertFalse(report.passed)
        failures = report.failures
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].dep.name, "form4_parquet")
        self.assertEqual(failures[0].status, CoverageStatus.FAIL_MISSING)


if __name__ == "__main__":
    unittest.main()
