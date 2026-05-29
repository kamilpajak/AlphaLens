"""Tests for ``alphalens_pipeline.thematic.verify_cache`` — gap-detection
on the daily news_ingest parquet cache.

Design memo: ``docs/research/paper_trading_non_trading_day_2026_05_29.md``
§5.1 Risk A (silent missing-day in news cache → silent missing news in
the next 30-day-lookback catalyst_resolver pass).

The verifier distinguishes:

* **missing-day** — no ``{YYYY-MM-DD}.parquet`` file at all. Ingest
  crashed before write or the systemd timer didn't fire. ALERT.
* **no-news day** — parquet exists but contains zero rows. All sources
  legitimately returned empty (rare but possible — full-market US
  holiday with no overnight wires, e.g. Christmas Day). NOT an alert.

Cache layout: ``~/.alphalens/thematic_news/{YYYY-MM-DD}.parquet`` keyed
on UTC calendar date (intentional — news doesn't stop on weekends, see
memo §5.1 prose).
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS, empty_news_frame
from alphalens_pipeline.thematic.verify_cache import VerifyResult, verify_cache


def _write_parquet(path: Path, n_rows: int) -> None:
    """Write a parquet with ``n_rows`` zero-padded rows conforming to the
    NEWS_COLUMNS schema. Used to seed both "no-news day" (n=0) and
    "real news day" (n>0) fixtures."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if n_rows == 0:
        df = empty_news_frame()
    else:
        df = pd.DataFrame(
            [
                {
                    "url": f"https://example.com/{i}",
                    "title": f"news {i}",
                    "timestamp": pd.Timestamp("2026-05-29 12:00:00+00:00"),
                    "source": "polygon",
                    "tickers": ["NVDA"],
                    "summary": "",
                    "extra": "{}",
                }
                for i in range(n_rows)
            ],
            columns=NEWS_COLUMNS,
        )
    df.to_parquet(path, index=False)


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TestAllPresent(_Base):
    def test_full_window_no_missing(self):
        """7 consecutive days, every parquet present + non-empty.
        ``missing_days`` empty; ``zero_row_days`` empty; exit-0 territory."""
        today = dt.date(2026, 5, 29)
        for i in range(7):
            d = today - dt.timedelta(days=i)
            _write_parquet(self.cache / f"{d.isoformat()}.parquet", n_rows=5)

        result = verify_cache(cache_dir=self.cache, days=7, today=today)

        self.assertIsInstance(result, VerifyResult)
        self.assertEqual(result.missing_days, [])
        self.assertEqual(result.zero_row_days, [])
        self.assertEqual(result.checked_days, 7)


class TestMissingDays(_Base):
    def test_single_missing_day_detected(self):
        """6/7 parquets present + one calendar gap. The single missing
        date is surfaced; zero_row stays empty."""
        today = dt.date(2026, 5, 29)
        for i in range(7):
            d = today - dt.timedelta(days=i)
            if d == dt.date(2026, 5, 27):
                continue  # leave a hole
            _write_parquet(self.cache / f"{d.isoformat()}.parquet", n_rows=3)

        result = verify_cache(cache_dir=self.cache, days=7, today=today)

        self.assertEqual(result.missing_days, [dt.date(2026, 5, 27)])
        self.assertEqual(result.zero_row_days, [])
        self.assertEqual(result.checked_days, 7)

    def test_multi_missing_days_detected_in_chronological_order(self):
        """Three holes; verifier reports them sorted ascending."""
        today = dt.date(2026, 5, 29)
        for i in range(7):
            d = today - dt.timedelta(days=i)
            if d in {dt.date(2026, 5, 25), dt.date(2026, 5, 27), dt.date(2026, 5, 29)}:
                continue
            _write_parquet(self.cache / f"{d.isoformat()}.parquet", n_rows=3)

        result = verify_cache(cache_dir=self.cache, days=7, today=today)

        self.assertEqual(
            result.missing_days,
            [dt.date(2026, 5, 25), dt.date(2026, 5, 27), dt.date(2026, 5, 29)],
        )

    def test_all_missing_full_window_reported(self):
        """Empty cache → every requested day appears as missing.
        ``checked_days`` stays equal to ``days`` (the verifier always
        reports how many days it tried to look at)."""
        today = dt.date(2026, 5, 29)

        result = verify_cache(cache_dir=self.cache, days=3, today=today)

        self.assertEqual(
            result.missing_days,
            [dt.date(2026, 5, 27), dt.date(2026, 5, 28), dt.date(2026, 5, 29)],
        )
        self.assertEqual(result.checked_days, 3)


class TestNoNewsVsMissing(_Base):
    def test_zero_row_parquet_is_no_news_not_missing(self):
        """The headline semantic of Risk A: ``empty_news_frame`` written
        on a legitimately-quiet day is NOT a missing-day alert. The
        verifier surfaces it on ``zero_row_days`` for observability but
        leaves ``missing_days`` empty (no alert)."""
        today = dt.date(2026, 5, 29)
        _write_parquet(self.cache / "2026-05-29.parquet", n_rows=10)
        _write_parquet(self.cache / "2026-05-28.parquet", n_rows=0)  # quiet day
        _write_parquet(self.cache / "2026-05-27.parquet", n_rows=4)

        result = verify_cache(cache_dir=self.cache, days=3, today=today)

        self.assertEqual(result.missing_days, [])
        self.assertEqual(result.zero_row_days, [dt.date(2026, 5, 28)])

    def test_corrupted_parquet_treated_as_missing(self):
        """A non-parquet file (truncated write, ENOSPC mid-write) at the
        expected path is worse than missing — we cannot read it. The
        verifier reports it as missing so the operator alert fires; the
        next ingest run will rewrite it."""
        today = dt.date(2026, 5, 29)
        (self.cache / "2026-05-29.parquet").write_bytes(b"NOT A PARQUET")

        result = verify_cache(cache_dir=self.cache, days=1, today=today)

        self.assertEqual(result.missing_days, [dt.date(2026, 5, 29)])


class TestWindowing(_Base):
    def test_days_one_includes_today_only(self):
        """``--days 1`` → window is just today. Useful for a fast post-
        timer health check before the daily catalyst_resolver pass."""
        today = dt.date(2026, 5, 29)
        _write_parquet(self.cache / "2026-05-29.parquet", n_rows=5)
        _write_parquet(self.cache / "2026-05-28.parquet", n_rows=5)

        result = verify_cache(cache_dir=self.cache, days=1, today=today)

        self.assertEqual(result.checked_days, 1)
        self.assertEqual(result.missing_days, [])

    def test_days_thirty_matches_catalyst_resolver_window(self):
        """``--days 30`` matches ``catalyst_resolver._load_window``'s
        DEFAULT_LOOKBACK_DAYS — the verifier should cover at least that
        span so any gap inside the brief-generation window surfaces."""
        today = dt.date(2026, 5, 29)
        for i in range(30):
            d = today - dt.timedelta(days=i)
            _write_parquet(self.cache / f"{d.isoformat()}.parquet", n_rows=2)

        result = verify_cache(cache_dir=self.cache, days=30, today=today)

        self.assertEqual(result.checked_days, 30)
        self.assertEqual(result.missing_days, [])

    def test_zero_days_raises(self):
        """A 0-day window is meaningless — caller bug. Raise loud rather
        than silently returning an OK result."""
        with self.assertRaises(ValueError):
            verify_cache(cache_dir=self.cache, days=0, today=dt.date(2026, 5, 29))

    def test_negative_days_raises(self):
        with self.assertRaises(ValueError):
            verify_cache(cache_dir=self.cache, days=-1, today=dt.date(2026, 5, 29))


class TestCacheDirMissing(_Base):
    def test_nonexistent_cache_dir_reports_all_missing(self):
        """The systemd timer's first-ever run starts with no
        ~/.alphalens/thematic_news/ directory. The verifier must not
        crash on FileNotFoundError — it should report every requested
        day as missing so the operator sees the bootstrap state."""
        today = dt.date(2026, 5, 29)
        nonexistent = Path(self._tmp.name) / "does-not-exist"

        result = verify_cache(cache_dir=nonexistent, days=3, today=today)

        self.assertEqual(len(result.missing_days), 3)
        self.assertEqual(result.checked_days, 3)


if __name__ == "__main__":
    unittest.main()
