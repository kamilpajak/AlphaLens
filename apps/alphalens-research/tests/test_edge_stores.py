"""Unit tests for the shared EDGE store loaders (edge_stores)."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.data import rs_history
from alphalens_research.diagnostics import edge_stores


class TestEdgeStores(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_load_store_concats_stamps_date_and_skips_bad_stem(self):
        d = self.root / "ladders"
        d.mkdir()
        pd.DataFrame([{"ticker": "AAA", "plannable": True}]).to_parquet(d / "2026-05-01.parquet")
        pd.DataFrame([{"ticker": "BBB", "plannable": False}]).to_parquet(d / "2026-05-02.parquet")
        pd.DataFrame([{"ticker": "ZZZ"}]).to_parquet(
            d / "notadate.parquet"
        )  # non-ISO stem -> skipped
        df = edge_stores.load_store(d)
        self.assertEqual(len(df), 2)
        self.assertEqual(set(df["brief_date"]), {dt.date(2026, 5, 1), dt.date(2026, 5, 2)})

    def test_load_store_empty_dir(self):
        d = self.root / "empty"
        d.mkdir()
        self.assertTrue(edge_stores.load_store(d).empty)

    def test_setup_index_decodes_and_skips_blank_ticker(self):
        d = self.root / "briefs"
        d.mkdir()
        setup = {"status": "OK", "entry_tiers": [{"limit": 9.0}], "disaster_stop": 8.0}
        pd.DataFrame(
            [
                {"ticker": "aaa", "brief_trade_setup": json.dumps(setup)},
                {"ticker": "", "brief_trade_setup": json.dumps(setup)},  # no ticker -> skipped
                {"ticker": "CCC", "brief_trade_setup": None},  # no setup -> skipped
            ]
        ).to_parquet(d / "2026-05-01.parquet")
        idx = edge_stores.setup_index(d)
        self.assertEqual(list(idx), [(dt.date(2026, 5, 1), "AAA")])
        self.assertEqual(idx[(dt.date(2026, 5, 1), "AAA")]["disaster_stop"], 8.0)

    def test_setup_index_empty_dir(self):
        d = self.root / "nobriefs"
        d.mkdir()
        self.assertEqual(edge_stores.setup_index(d), {})

    def test_grouped_daily_cache_reads_and_memoizes_misses(self):
        gd = self.root / "grouped"
        gd.mkdir()
        day = dt.date(2026, 5, 1)
        rs_history.write_grouped_day_atomic(
            gd, day, {"AAA": {"o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 100.0}}
        )
        cache = edge_stores.GroupedDailyCache(gd)
        snap = cache.get(day)
        self.assertIsNotNone(snap)
        assert snap is not None  # narrow for type-checker
        self.assertIn("AAA", snap)
        # a missing session resolves to None and is cached (second call hits the cache)
        self.assertIsNone(cache.get(dt.date(2020, 1, 1)))
        self.assertIsNone(cache.get(dt.date(2020, 1, 1)))

    def test_newest_session_picks_max_iso_stem(self):
        gd = self.root / "g2"
        gd.mkdir()
        for stem in ("2026-05-01", "2026-06-12", "2026-05-30", "notadate"):
            (gd / f"{stem}.parquet").write_bytes(b"")  # newest_session only parses stems
        self.assertEqual(edge_stores.newest_session(gd), dt.date(2026, 6, 12))

    def test_newest_session_missing_dir(self):
        self.assertIsNone(edge_stores.newest_session(self.root / "does-not-exist"))


if __name__ == "__main__":
    unittest.main()
