"""Tests for `scripts/pull_ivolatility_ivs.py`.

The IVS puller is a thin per-ticker driver that:

1. Resolves the ticker's cache parquet path under
   ``~/.alphalens/ivolatility_ivs/{TICKER}.parquet``.
2. Skips tickers whose existing parquet already covers the requested
   ``[from_date, to_date]`` range (idempotency, mirrors
   ``backfill_ivolatility_pre_2018.py``).
3. Otherwise calls ``fetch_calendar_endpoint`` (async machinery from
   ``pull_ivolatility_calendars``) on ``/equities/eod/ivs``, validates
   the response schema, dedupes by (date, period, strike, Call/Put),
   and writes the resulting DataFrame back to disk.

Tests mock the network layer; no live API calls.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


def _ivs_row(date_, period, strike, otm_pct, call_put, iv, delta, symbol="AAPL"):
    return {
        "record_no": 0,
        "symbol": symbol,
        "exchange": "NASDAQ",
        "date": date_,
        "period": period,
        "strike": strike,
        "out-of-the-money %": otm_pct,
        "Call/Put": call_put,
        "IV": iv,
        "delta": delta,
    }


class TestPullOneTicker(unittest.TestCase):
    """Per-ticker driver — happy path + idempotency + corruption guards."""

    def test_first_pull_writes_parquet_with_full_schema(self):
        from scripts.pull_ivolatility_ivs import pull_ivs_ticker

        rows = [
            _ivs_row("2024-01-02", 30, 184.25, 0, "C", 0.205, 0.52),
            _ivs_row("2024-01-02", 30, 184.25, 0, "P", 0.192, -0.48),
            _ivs_row("2024-01-03", 30, 185.50, 0, "C", 0.210, 0.53),
            _ivs_row("2024-01-03", 30, 185.50, 0, "P", 0.198, -0.47),
        ]
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            with patch(
                "scripts.pull_ivolatility_ivs.fetch_calendar_endpoint",
                return_value=pd.DataFrame(rows),
            ):
                outcome = pull_ivs_ticker(
                    ticker="AAPL",
                    from_date="2024-01-02",
                    to_date="2024-01-03",
                    cache_dir=cache_dir,
                    api_key="fake-key",
                )
            self.assertEqual(outcome, "fetched")
            path = cache_dir / "AAPL.parquet"
            self.assertTrue(path.exists())
            df = pd.read_parquet(path)
            self.assertEqual(len(df), 4)
            self.assertIn("date", df.columns)
            self.assertIn("IV", df.columns)
            self.assertIn("Call/Put", df.columns)

    def test_idempotent_skip_when_range_already_covered(self):
        from scripts.pull_ivolatility_ivs import pull_ivs_ticker

        # Seed an existing parquet covering 2024-01-01 → 2024-01-31.
        existing = pd.DataFrame(
            [
                _ivs_row("2024-01-02", 30, 184.25, 0, "C", 0.205, 0.52),
                _ivs_row("2024-01-31", 30, 190.00, 0, "C", 0.215, 0.55),
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            existing.to_parquet(cache_dir / "AAPL.parquet")
            with patch(
                "scripts.pull_ivolatility_ivs.fetch_calendar_endpoint",
            ) as mock_fetch:
                outcome = pull_ivs_ticker(
                    ticker="AAPL",
                    from_date="2024-01-05",
                    to_date="2024-01-15",  # narrow window inside existing
                    cache_dir=cache_dir,
                    api_key="fake-key",
                )
            self.assertEqual(outcome, "skipped_already_covered")
            mock_fetch.assert_not_called()

    def test_pull_extends_existing_parquet_dedup_overlap(self):
        from scripts.pull_ivolatility_ivs import pull_ivs_ticker

        # Existing covers Jan; we request Jan 25 → Feb 5 (overlap on last week of Jan).
        existing = pd.DataFrame(
            [
                _ivs_row("2024-01-25", 30, 190.00, 0, "C", 0.21, 0.55),
                _ivs_row("2024-01-31", 30, 192.00, 0, "C", 0.22, 0.56),
            ]
        )
        # Fetcher returns Jan 25 → Feb 5 (overlapping Jan 25/31 + new Feb dates).
        new_data = pd.DataFrame(
            [
                _ivs_row("2024-01-25", 30, 190.00, 0, "C", 0.21, 0.55),  # dup
                _ivs_row("2024-01-31", 30, 192.00, 0, "C", 0.22, 0.56),  # dup
                _ivs_row("2024-02-01", 30, 193.00, 0, "C", 0.23, 0.57),
                _ivs_row("2024-02-05", 30, 194.00, 0, "C", 0.24, 0.58),
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            existing.to_parquet(cache_dir / "AAPL.parquet")
            with patch(
                "scripts.pull_ivolatility_ivs.fetch_calendar_endpoint",
                return_value=new_data,
            ):
                outcome = pull_ivs_ticker(
                    ticker="AAPL",
                    from_date="2024-01-25",
                    to_date="2024-02-05",
                    cache_dir=cache_dir,
                    api_key="fake-key",
                )
            self.assertEqual(outcome, "fetched")
            df = pd.read_parquet(cache_dir / "AAPL.parquet")
            # 2 existing + 2 new (the 2 overlapping rows deduped).
            self.assertEqual(len(df), 4)
            dates = sorted(df["date"].unique())
            self.assertIn("2024-02-05", dates)
            self.assertIn("2024-01-25", dates)

    def test_empty_response_returns_skipped_no_coverage_no_overwrite(self):
        from scripts.pull_ivolatility_ivs import pull_ivs_ticker

        # Existing parquet should NOT be wiped just because the new fetch
        # returned an empty frame (could be a valid "no data in window"
        # result OR a tier-denial; either way, preserve what we have).
        existing = pd.DataFrame([_ivs_row("2024-01-25", 30, 190.00, 0, "C", 0.21, 0.55)])
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            existing.to_parquet(cache_dir / "AAPL.parquet")
            with patch(
                "scripts.pull_ivolatility_ivs.fetch_calendar_endpoint",
                return_value=pd.DataFrame(),
            ):
                outcome = pull_ivs_ticker(
                    ticker="AAPL",
                    from_date="2099-01-01",
                    to_date="2099-01-31",
                    cache_dir=cache_dir,
                    api_key="fake-key",
                )
            self.assertEqual(outcome, "skipped_no_coverage")
            df = pd.read_parquet(cache_dir / "AAPL.parquet")
            self.assertEqual(len(df), 1)  # untouched

    def test_fetcher_exception_does_not_corrupt_existing_parquet(self):
        from scripts.pull_ivolatility_ivs import pull_ivs_ticker

        existing = pd.DataFrame([_ivs_row("2024-01-25", 30, 190.00, 0, "C", 0.21, 0.55)])
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            existing.to_parquet(cache_dir / "AAPL.parquet")
            with patch(
                "scripts.pull_ivolatility_ivs.fetch_calendar_endpoint",
                side_effect=RuntimeError("vendor 500"),
            ):
                outcome = pull_ivs_ticker(
                    ticker="AAPL",
                    from_date="2024-02-01",
                    to_date="2024-02-28",
                    cache_dir=cache_dir,
                    api_key="fake-key",
                )
            self.assertEqual(outcome, "errors")
            df = pd.read_parquet(cache_dir / "AAPL.parquet")
            self.assertEqual(len(df), 1)


class TestBatchDriver(unittest.TestCase):
    """Aggregate counts across a list of tickers (mirror backfill script API)."""

    def test_batch_aggregates_outcome_counts(self):
        from scripts.pull_ivolatility_ivs import pull_ivs_universe

        outcomes = ["fetched", "skipped_already_covered", "errors", "fetched"]

        with patch("scripts.pull_ivolatility_ivs.pull_ivs_ticker", side_effect=outcomes):
            counts = pull_ivs_universe(
                tickers=["A", "B", "C", "D"],
                from_date="2024-01-01",
                to_date="2024-01-31",
                cache_dir=Path("/tmp/no-such-dir"),
                api_key="fake",
                sleep_between=0.0,
            )
        self.assertEqual(
            counts,
            {
                "fetched": 2,
                "skipped_already_covered": 1,
                "skipped_no_coverage": 0,
                "errors": 1,
            },
        )


class TestValidateCache(unittest.TestCase):
    """Pre-resume cleanup — delete corrupt/suspect parquet files so the
    main loop's idempotent skip cannot be tricked by a half-written file
    (SIGKILL / disk-full mid-write produces these)."""

    def test_zero_byte_file_is_deleted(self):
        from scripts.pull_ivolatility_ivs import validate_cache

        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            (cache_dir / "ZEROBYTE.parquet").write_bytes(b"")

            counts = validate_cache(cache_dir)

            self.assertEqual(counts["deleted_zero"], 1)
            self.assertFalse((cache_dir / "ZEROBYTE.parquet").exists())

    def test_subthreshold_size_is_deleted(self):
        from scripts.pull_ivolatility_ivs import validate_cache

        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            # 100 bytes — well under the 1KB default threshold; smaller
            # than any plausible parquet file (parquet has ~200B footer
            # alone, plus magic bytes, schema, and at least one row group).
            (cache_dir / "TINY.parquet").write_bytes(b"x" * 100)

            counts = validate_cache(cache_dir)

            self.assertEqual(counts["deleted_too_small"], 1)
            self.assertFalse((cache_dir / "TINY.parquet").exists())

    def test_corrupt_parquet_is_deleted(self):
        from scripts.pull_ivolatility_ivs import validate_cache

        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            # Above size threshold but not a valid parquet — pyarrow
            # raises on open. Mimics SIGKILL mid-write where the file
            # passes size check but body is incomplete.
            (cache_dir / "CORRUPT.parquet").write_bytes(b"\x00" * 4096)

            counts = validate_cache(cache_dir)

            self.assertEqual(counts["deleted_corrupt"], 1)
            self.assertFalse((cache_dir / "CORRUPT.parquet").exists())

    def test_valid_parquet_retained(self):
        from scripts.pull_ivolatility_ivs import validate_cache

        rows = [
            _ivs_row("2024-01-02", 30, 184.25, 0, "C", 0.205, 0.52),
            _ivs_row("2024-01-02", 30, 184.25, 0, "P", 0.192, -0.48),
        ] * 200
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            pd.DataFrame(rows).to_parquet(cache_dir / "GOOD.parquet")

            counts = validate_cache(cache_dir)

            self.assertEqual(counts["ok"], 1)
            self.assertEqual(counts["deleted_zero"], 0)
            self.assertEqual(counts["deleted_too_small"], 0)
            self.assertEqual(counts["deleted_corrupt"], 0)
            self.assertTrue((cache_dir / "GOOD.parquet").exists())

    def test_mixed_directory_returns_full_counts(self):
        from scripts.pull_ivolatility_ivs import validate_cache

        rows = [_ivs_row("2024-01-02", 30, 184.25, 0, "C", 0.205, 0.52)] * 200
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            pd.DataFrame(rows).to_parquet(cache_dir / "GOOD.parquet")
            (cache_dir / "ZERO.parquet").write_bytes(b"")
            (cache_dir / "TINY.parquet").write_bytes(b"x" * 100)
            (cache_dir / "CORRUPT.parquet").write_bytes(b"\x00" * 4096)

            counts = validate_cache(cache_dir)

            self.assertEqual(
                counts,
                {
                    "ok": 1,
                    "deleted_zero": 1,
                    "deleted_too_small": 1,
                    "deleted_corrupt": 1,
                },
            )

    def test_missing_cache_dir_returns_zero_counts(self):
        from scripts.pull_ivolatility_ivs import validate_cache

        with tempfile.TemporaryDirectory() as td:
            counts = validate_cache(Path(td) / "does-not-exist")

        self.assertEqual(
            counts,
            {"ok": 0, "deleted_zero": 0, "deleted_too_small": 0, "deleted_corrupt": 0},
        )


if __name__ == "__main__":
    unittest.main()
