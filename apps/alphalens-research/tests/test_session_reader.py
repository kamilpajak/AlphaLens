"""How `_SessionReader` reads the grouped-daily store, and why it reads it that way.

The reader exists because a pass needs a few tickers out of a whole-market session file.
Its first form asked the parquet reader for exactly those rows and remembered which
tickers it had already fetched per session. That is the right instinct and the wrong
arithmetic for this store, measured on the VPS against a real session file (11 467 rows,
ONE row group, 505 KB):

| read                                  | cost     |
|---------------------------------------|----------|
| row filter for 80 tickers             | 18.4 ms  |
| whole file, no filter                 |  6.9 ms  |

The filter is 2.7x SLOWER, because a single row group gives the predicate nothing to
skip: every row is read either way and the filter is extra work on top. The cost that
mattered, though, was not per read but the NUMBER of reads. Each stamped date brings its
own ticker set, so `missing = tickers - already_read` was non-empty on nearly every
(date, session) pair and the same file was re-read once per date. Over the 137-date news
run that is about 37 000 reads and roughly 4.5 hours.

So the reader now reads a session file ONCE, in full, and keeps the rows it was told to
expect. The universe is a HINT and never a contract: a ticker outside it is still
answered, at the cost of one more read, because a universe computed slightly wrong must
never turn into a wrong label. Memory, measured the same way: 0.51 MB per session for
the 3 042-ticker news universe, against 2.28 MB per session for keeping every ticker.
"""

from __future__ import annotations

import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pandas as pd
from alphalens_pipeline.feedback import selection_label as sl

S1 = dt.date(2026, 5, 19)
S2 = dt.date(2026, 5, 20)


def _session_file(root: Path, day: dt.date, rows: list[tuple[str, float, float]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [{"T": t, "o": o, "c": c, "v": 1.0, "n": 1} for t, o, c in rows],
    ).to_parquet(root / f"{day.isoformat()}.parquet", index=False)


class _CountingReads:
    """Counts `read_parquet` calls and records the keyword arguments each one used."""

    def __init__(self):
        self.calls: list[dict] = []
        self._real = pd.read_parquet

    def __call__(self, path, **kwargs):
        self.calls.append({"path": str(path), **kwargs})
        return self._real(path, **kwargs)

    def __len__(self) -> int:
        return len(self.calls)


class _ReaderCase(unittest.TestCase):
    def setUp(self):
        self._d = TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.root = Path(self._d.name) / "grouped"
        _session_file(self.root, S1, [("AAA", 1.0, 2.0), ("BBB", 3.0, 4.0), ("CCC", 5.0, 6.0)])
        _session_file(self.root, S2, [("AAA", 7.0, 8.0), ("BBB", 9.0, 10.0)])

    def spy(self) -> _CountingReads:
        counter = _CountingReads()
        patcher = mock.patch.object(sl.pd, "read_parquet", counter)
        patcher.start()
        self.addCleanup(patcher.stop)
        return counter


class TestOneReadPerSessionFile(_ReaderCase):
    def test_two_dates_with_different_tickers_read_each_file_once(self):
        # The defect this replaces: with a per-ticker memo, the second date's ticker set
        # was "missing" and the same file was read again. Over 137 dates that is one read
        # per (date, session) pair instead of one per session.
        counter = self.spy()
        reader = sl._SessionReader(self.root, universe={"AAA", "BBB", "CCC"})
        reader.book([S1, S2], {"AAA"})
        after_first = len(counter)
        reader.book([S1, S2], {"BBB"})
        reader.book([S1, S2], {"CCC"})
        self.assertEqual(after_first, 2, "the first pass must read each session once")
        self.assertEqual(len(counter), 2, "a later ticker inside the universe must not re-read")

    def test_it_asks_for_no_row_filter(self):
        # 18.4 ms filtered against 6.9 ms unfiltered on a one-row-group file. Pinning the
        # absence keeps a well-meant "optimisation" from putting the filter back.
        counter = self.spy()
        sl._SessionReader(self.root, universe={"AAA"}).book([S1], {"AAA"})
        self.assertEqual(len(counter), 1)
        self.assertNotIn("filters", counter.calls[0])

    def test_it_asks_only_for_the_columns_it_uses(self):
        counter = self.spy()
        sl._SessionReader(self.root, universe={"AAA"}).book([S1], {"AAA"})
        self.assertEqual(counter.calls[0]["columns"], ["T", "o", "c"])

    def test_the_bars_it_returns_are_the_ones_in_the_file(self):
        book = sl._SessionReader(self.root, universe={"AAA", "BBB"}).book([S1, S2], {"AAA", "BBB"})
        self.assertEqual(book[S1]["AAA"], (1.0, 2.0))
        self.assertEqual(book[S1]["BBB"], (3.0, 4.0))
        self.assertEqual(book[S2]["AAA"], (7.0, 8.0))

    def test_a_ticker_absent_from_the_file_is_absent_from_the_book(self):
        book = sl._SessionReader(self.root, universe={"AAA", "CCC"}).book([S2], {"AAA", "CCC"})
        self.assertIn("AAA", book[S2])
        self.assertNotIn("CCC", book[S2])


class TestTheUniverseIsAHintAndNeverAContract(_ReaderCase):
    def test_a_ticker_outside_the_universe_is_still_answered(self):
        # A universe computed slightly wrong must cost a read, never a wrong label: a
        # missing bar reads as `no_open`, which is terminal, so the row would freeze
        # carrying an answer the prices do not support.
        reader = sl._SessionReader(self.root, universe={"AAA"})
        reader.book([S1], {"AAA"})
        book = reader.book([S1], {"CCC"})
        self.assertEqual(book[S1]["CCC"], (5.0, 6.0))

    def test_answering_it_costs_one_more_read_and_then_caches(self):
        counter = self.spy()
        reader = sl._SessionReader(self.root, universe={"AAA"})
        reader.book([S1], {"AAA"})
        reader.book([S1], {"CCC"})
        after = len(counter)
        reader.book([S1], {"CCC"})
        self.assertEqual(after, 2, "the ticker outside the universe needs its own read")
        self.assertEqual(len(counter), 2, "and only one; the second ask is cached")

    def test_no_universe_keeps_every_ticker_in_the_file(self):
        counter = self.spy()
        reader = sl._SessionReader(self.root)
        reader.book([S1], {"AAA"})
        reader.book([S1], {"BBB"})
        reader.book([S1], {"CCC"})
        self.assertEqual(len(counter), 1, "with no universe one read must settle the file")


class TestAFileItCannotRead(_ReaderCase):
    def test_a_missing_session_file_answers_None_and_is_not_retried(self):
        counter = self.spy()
        reader = sl._SessionReader(self.root, universe={"AAA"})
        gone = dt.date(2026, 5, 21)
        self.assertIsNone(reader.book([gone], {"AAA"})[gone])
        self.assertIsNone(reader.book([gone], {"BBB"})[gone])
        self.assertEqual(len(counter), 0, "a missing file is never opened")

    def test_an_unreadable_session_file_answers_None_once_and_warns(self):
        bad = dt.date(2026, 5, 22)
        (self.root / f"{bad.isoformat()}.parquet").write_bytes(b"not a parquet file")
        reader = sl._SessionReader(self.root, universe={"AAA"})
        with self.assertLogs(sl.logger, level="WARNING") as logs:
            self.assertIsNone(reader.book([bad], {"AAA"})[bad])
        self.assertTrue(any("unreadable session file" in m for m in logs.output))
        self.assertIsNone(reader.book([bad], {"AAA"})[bad])


class TestTheDriverSuppliesTheUniverse(unittest.TestCase):
    def test_enrich_passes_the_population_tickers_and_the_benchmark(self):
        """A universe the driver does not pass is a universe of everything.

        With no universe each session file is cached whole, measured at 2.28 MB against
        0.51 MB, and a 270-session window would hold about 600 MB for a nightly run that
        needs a few names.
        """
        seen: dict = {}
        real = sl._SessionReader

        def capture(root, universe=None):
            seen["universe"] = universe
            return real(root, universe=universe)

        with TemporaryDirectory() as d:
            news = Path(d) / "news"
            news.mkdir()
            pd.DataFrame([{"id": "a", "tickers": ["aaa", "bbb"], "source": "polygon"}]).to_parquet(
                news / "2026-05-19.parquet", index=False
            )
            with mock.patch.object(sl, "_SessionReader", capture):
                sl.enrich_selection_labels(
                    source=sl.news_population_source(news),
                    labels_dir=Path(d) / "out",
                    grouped_root=Path(d) / "grouped",
                    now=dt.datetime(2027, 6, 1, tzinfo=dt.UTC),
                    reference_closes=lambda *a, **k: None,
                )
        self.assertIsNotNone(seen["universe"], "the driver must name the universe")
        self.assertEqual(seen["universe"], {"AAA", "BBB", sl.BENCHMARK_TICKER})


if __name__ == "__main__":
    unittest.main()
