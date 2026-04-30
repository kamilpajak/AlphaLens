import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd


def _make_picks(tickers_themes_scores):
    return pd.DataFrame(
        [{"ticker": t, "momentum_score": s, "themes": th} for t, th, s in tickers_themes_scores]
    )


class TestThemedHistoryStoreBasic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "hist.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_and_retrieve_run(self):
        from alphalens.archive.screeners.themed.history_store import ThemedHistoryStore

        store = ThemedHistoryStore(self.db)
        picks = _make_picks([("A", ["quantum"], 0.9), ("B", ["ai"], 0.8)])
        run_id = store.record_run(picks, config={"top_n": 2}, universe_size=100)
        self.assertGreaterEqual(run_id, 1)

        runs = store.recent_runs(days=10)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].scored_count, 2)
        self.assertEqual(runs[0].universe_size, 100)
        self.assertIsNone(runs[0].error)

    def test_picks_persisted_with_rank(self):
        from alphalens.archive.screeners.themed.history_store import ThemedHistoryStore

        store = ThemedHistoryStore(self.db)
        picks = _make_picks(
            [
                ("A", ["quantum"], 0.9),
                ("B", ["ai", "biotech"], 0.8),
                ("C", ["semis"], 0.7),
            ]
        )
        run_id = store.record_run(picks, config={}, universe_size=50)

        df = store.picks_for_run(run_id)
        self.assertEqual(len(df), 3)
        self.assertEqual(list(df["rank"]), [1, 2, 3])
        self.assertEqual(df.iloc[0]["ticker"], "A")
        # Multi-theme serialized as comma list
        self.assertEqual(df.iloc[1]["themes"], "ai,biotech")

    def test_empty_picks_still_records_run(self):
        from alphalens.archive.screeners.themed.history_store import ThemedHistoryStore

        store = ThemedHistoryStore(self.db)
        picks = _make_picks([])
        run_id = store.record_run(picks, config={}, universe_size=20)
        self.assertGreaterEqual(run_id, 1)
        self.assertEqual(store.picks_for_run(run_id).shape[0], 0)

    def test_error_field_persisted(self):
        from alphalens.archive.screeners.themed.history_store import ThemedHistoryStore

        store = ThemedHistoryStore(self.db)
        store.record_run(_make_picks([]), {}, universe_size=0, error="fetcher failed")
        runs = store.recent_runs()
        self.assertEqual(runs[0].error, "fetcher failed")

    def test_weights_and_scheme_persist(self):
        from alphalens.archive.screeners.themed.history_store import ThemedHistoryStore

        store = ThemedHistoryStore(self.db)
        picks = _make_picks([("A", ["q"], 0.9), ("B", ["q"], 0.8)])
        run_id = store.record_run(
            picks,
            {},
            universe_size=10,
            weighting_scheme="linear",
            weights=[0.7, 0.3],
        )
        df = store.picks_for_run(run_id)
        self.assertAlmostEqual(df.iloc[0]["weight"], 0.7)
        self.assertAlmostEqual(df.iloc[1]["weight"], 0.3)
        self.assertEqual(df.iloc[0]["weighting_scheme"], "linear")


class TestPicksTimeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "hist.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_timeline_joins_runs_and_picks(self):
        from alphalens.archive.screeners.themed.history_store import ThemedHistoryStore

        store = ThemedHistoryStore(self.db)
        today = date.today()
        # Zapisz 3 kolejne runs
        for i, run_date in enumerate([today - timedelta(days=2), today - timedelta(days=1), today]):
            picks = _make_picks([(f"T{j}", ["q"], 1.0 - 0.1 * j) for j in range(3)])
            store.record_run(picks, {}, universe_size=10, run_date=run_date)

        tl = store.picks_timeline(days=10)
        # 3 runs × 3 picks each = 9 rows
        self.assertEqual(len(tl), 9)
        self.assertIn("run_date", tl.columns)
        self.assertIn("ticker", tl.columns)


class TestStaleness(unittest.TestCase):
    def test_detects_persistent_name(self):
        from alphalens.archive.screeners.themed.history_store import compute_staleness

        # A is top-1 for 5 days, B appears once
        timeline = pd.DataFrame(
            [
                {"run_date": "2024-01-01", "rank": 1, "ticker": "A"},
                {"run_date": "2024-01-02", "rank": 1, "ticker": "A"},
                {"run_date": "2024-01-03", "rank": 1, "ticker": "A"},
                {"run_date": "2024-01-04", "rank": 1, "ticker": "A"},
                {"run_date": "2024-01-05", "rank": 1, "ticker": "A"},
                {"run_date": "2024-01-05", "rank": 2, "ticker": "B"},
            ]
        )
        stale = compute_staleness(timeline, top_n=5)
        row_a = stale[stale["ticker"] == "A"].iloc[0]
        self.assertEqual(row_a["consecutive_days"], 5)

    def test_empty_input(self):
        from alphalens.archive.screeners.themed.history_store import compute_staleness

        self.assertTrue(compute_staleness(pd.DataFrame()).empty)


class TestTurnover(unittest.TestCase):
    def test_turnover_by_day(self):
        from alphalens.archive.screeners.themed.history_store import compute_turnover_by_day

        timeline = pd.DataFrame(
            [
                {"run_date": "2024-01-01", "rank": 1, "ticker": "A"},
                {"run_date": "2024-01-01", "rank": 2, "ticker": "B"},
                {"run_date": "2024-01-02", "rank": 1, "ticker": "A"},
                {"run_date": "2024-01-02", "rank": 2, "ticker": "C"},  # B -> C swap
                {"run_date": "2024-01-03", "rank": 1, "ticker": "X"},  # full turnover
                {"run_date": "2024-01-03", "rank": 2, "ticker": "Y"},
            ]
        )
        df = compute_turnover_by_day(timeline, top_n=5)
        self.assertEqual(len(df), 3)
        # Day 2: 1/2 changed → 50%
        row2 = df[df["run_date"] == "2024-01-02"].iloc[0]
        self.assertAlmostEqual(row2["turnover"], 0.5)
        # Day 3: 100% turnover
        row3 = df[df["run_date"] == "2024-01-03"].iloc[0]
        self.assertAlmostEqual(row3["turnover"], 1.0)


class TestThemeHHI(unittest.TestCase):
    def test_single_theme_hhi_one(self):
        from alphalens.archive.screeners.themed.history_store import compute_theme_hhi_by_day

        timeline = pd.DataFrame(
            [
                {
                    "run_date": "2024-01-01",
                    "rank": 1,
                    "ticker": "A",
                    "themes": "quantum",
                },
                {
                    "run_date": "2024-01-01",
                    "rank": 2,
                    "ticker": "B",
                    "themes": "quantum",
                },
                {
                    "run_date": "2024-01-01",
                    "rank": 3,
                    "ticker": "C",
                    "themes": "quantum",
                },
            ]
        )
        df = compute_theme_hhi_by_day(timeline, top_n=5)
        self.assertAlmostEqual(df.iloc[0]["hhi"], 1.0)
        self.assertEqual(df.iloc[0]["dominant_theme"], "quantum")

    def test_balanced_themes_hhi_diversified(self):
        from alphalens.archive.screeners.themed.history_store import compute_theme_hhi_by_day

        timeline = pd.DataFrame(
            [
                {
                    "run_date": "2024-01-01",
                    "rank": 1,
                    "ticker": "A",
                    "themes": "quantum",
                },
                {"run_date": "2024-01-01", "rank": 2, "ticker": "B", "themes": "ai"},
                {
                    "run_date": "2024-01-01",
                    "rank": 3,
                    "ticker": "C",
                    "themes": "biotech",
                },
            ]
        )
        df = compute_theme_hhi_by_day(timeline, top_n=5)
        # 3 equal themes → HHI = 3 × (1/3)² = 1/3
        self.assertAlmostEqual(df.iloc[0]["hhi"], 1 / 3, places=3)


if __name__ == "__main__":
    unittest.main()
