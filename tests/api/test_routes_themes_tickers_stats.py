"""Routes: /v1/themes, /v1/themes/{theme}/candidates, /v1/tickers/{ticker}/history, /v1/stats."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from alphalens.api.app import create_app
from alphalens.api.cache import rebuild_from_parquet
from tests.api._fixtures import seed_two_days


class ThemesTickersStatsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.briefs_dir = root / "briefs"
        cls.db_path = root / "briefs.db"
        seed_two_days(cls.briefs_dir)
        rebuild_from_parquet(briefs_dir=cls.briefs_dir, db_path=cls.db_path)
        cls.client = TestClient(create_app(cls.db_path))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    # ------------------------------------------------------------------ themes

    def test_list_themes_aggregates(self):
        r = self.client.get("/v1/themes")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        themes = {t["theme"]: t for t in body["data"]}
        self.assertIn("quantum_computing", themes)
        self.assertEqual(themes["quantum_computing"]["n_candidates"], 3)
        self.assertEqual(themes["quantum_computing"]["n_days"], 2)
        self.assertEqual(themes["quantum_computing"]["first_seen"], "2026-05-17")
        self.assertEqual(themes["quantum_computing"]["last_seen"], "2026-05-18")
        self.assertEqual(body["meta"]["total"], len(body["data"]))

    def test_list_themes_date_range(self):
        r = self.client.get("/v1/themes?from=2026-05-18")
        body = r.json()
        themes = {t["theme"] for t in body["data"]}
        # Older 'robotics' has score 3 on 2026-05-17 and score 5 on 2026-05-18 — still present.
        self.assertIn("robotics", themes)
        # 'quantum_computing' is in both; 2026-05-17-only themes (none here) would drop.
        for t in body["data"]:
            self.assertEqual(t["first_seen"], "2026-05-18")

    def test_theme_candidates_filter(self):
        r = self.client.get("/v1/themes/robotics/candidates")
        body = r.json()
        self.assertEqual(body["meta"]["total"], 2)
        for c in body["data"]:
            self.assertEqual(c["theme"], "robotics")

    def test_theme_candidates_pagination(self):
        r = self.client.get("/v1/themes/quantum_computing/candidates?limit=1")
        body = r.json()
        self.assertEqual(len(body["data"]), 1)
        self.assertEqual(body["meta"]["total"], 3)

    def test_theme_candidates_cross_date_orders_by_date_desc_then_rank(self):
        # DEFAULT_ORDER: date DESC, COALESCE(rank_in_day, 999999) ASC, ticker ASC.
        # quantum_computing has AAA@rank=1 on 2026-05-18 and AAA@rank=1 +
        # BBB@rank=2 on 2026-05-17. Newer date first; within each date,
        # rank_in_day ASC.
        r = self.client.get("/v1/themes/quantum_computing/candidates")
        body = r.json()
        rows = [(c["date"], c["ticker"], c["rank_in_day"]) for c in body["data"]]
        self.assertEqual(
            rows,
            [
                ("2026-05-18", "AAA", 1),
                ("2026-05-17", "AAA", 1),
                ("2026-05-17", "BBB", 2),
            ],
        )

    # ----------------------------------------------------------------- tickers

    def test_ticker_history_descending(self):
        r = self.client.get("/v1/tickers/AAA/history")
        body = r.json()
        dates = [c["date"] for c in body["data"]]
        self.assertEqual(dates, ["2026-05-18", "2026-05-17"])
        self.assertEqual(body["meta"]["total"], 2)

    def test_ticker_history_case_insensitive(self):
        r = self.client.get("/v1/tickers/aaa/history")
        self.assertEqual(r.json()["meta"]["total"], 2)

    def test_ticker_history_empty_for_unknown(self):
        r = self.client.get("/v1/tickers/ZZZ/history")
        body = r.json()
        self.assertEqual(body["meta"]["total"], 0)
        self.assertEqual(body["data"], [])

    # ------------------------------------------------------------------- stats

    def test_stats_payload(self):
        r = self.client.get("/v1/stats")
        body = r.json()
        self.assertEqual(body["n_days"], 2)
        self.assertEqual(body["n_candidates"], 7)
        self.assertEqual(body["earliest_date"], "2026-05-17")
        self.assertEqual(body["latest_date"], "2026-05-18")
        self.assertGreater(len(body["top_themes"]), 0)
        # n_themes is distinct theme count across all briefs
        self.assertEqual(body["n_themes"], 3)


if __name__ == "__main__":
    unittest.main()
