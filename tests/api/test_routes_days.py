"""Routes: /healthz, /readyz, /v1/days, /v1/days/{date}, /v1/days/{date}/candidates."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from alphalens.api.app import create_app
from alphalens.api.cache import rebuild_from_parquet
from tests.api._fixtures import seed_day_with_distinct_rank_order, seed_two_days


class DaysRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.briefs_dir = root / "briefs"
        cls.db_path = root / "briefs.db"
        seed_two_days(cls.briefs_dir)
        rebuild_from_parquet(briefs_dir=cls.briefs_dir, db_path=cls.db_path)
        cls.app = create_app(cls.db_path)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_healthz(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_readyz_with_cache(self):
        r = self.client.get("/readyz")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["n_days"], 2)
        self.assertEqual(body["n_candidates"], 7)
        self.assertIsNotNone(body["last_rebuild_at"])

    def test_readyz_missing_db_returns_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_app = create_app(Path(tmp) / "ghost.db")
            with TestClient(missing_app) as client:
                r = client.get("/readyz")
                self.assertEqual(r.status_code, 503)

    def test_list_days_descending(self):
        r = self.client.get("/v1/days")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        dates = [d["date"] for d in body["data"]]
        self.assertEqual(dates, ["2026-05-18", "2026-05-17"])
        self.assertEqual(body["meta"]["total"], 2)
        self.assertEqual(body["meta"]["limit"], 50)
        self.assertEqual(body["data"][0]["n_candidates"], 4)

    def test_list_days_pagination(self):
        r = self.client.get("/v1/days?limit=1&offset=1")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["data"]), 1)
        self.assertEqual(body["data"][0]["date"], "2026-05-17")
        self.assertEqual(body["meta"], {"total": 2, "limit": 1, "offset": 1})

    def test_list_days_date_range(self):
        r = self.client.get("/v1/days?from=2026-05-18&to=2026-05-18")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual([d["date"] for d in body["data"]], ["2026-05-18"])

    def test_list_days_rejects_bad_date(self):
        r = self.client.get("/v1/days?from=05/18/2026")
        self.assertEqual(r.status_code, 422)

    def test_get_day_full(self):
        r = self.client.get("/v1/days/2026-05-18")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["date"], "2026-05-18")
        self.assertEqual(body["n_candidates"], 4)
        self.assertEqual(len(body["candidates"]), 4)
        tickers = [c["ticker"] for c in body["candidates"]]
        # Descending by score, then ticker — DDD (5) first, then AAA (6) -> wait
        # AAA score 6 > DDD score 5; ordering check:
        self.assertEqual(tickers[0], "AAA")  # highest score (6)
        self.assertIn("FFF", tickers)
        # theme_counts sums to n_candidates
        self.assertEqual(sum(body["theme_counts"].values()), body["n_candidates"])

    def test_get_day_decodes_list_columns(self):
        r = self.client.get("/v1/days/2026-05-18")
        body = r.json()
        aaa = next(c for c in body["candidates"] if c["ticker"] == "AAA")
        self.assertEqual(aaa["gates_passed"], ["polygon_news", "etf_holdings"])
        self.assertIsInstance(aaa["theme_search_keywords"], list)

    def test_get_day_404(self):
        r = self.client.get("/v1/days/2099-12-31")
        self.assertEqual(r.status_code, 404)

    def test_get_day_invalid_date_422(self):
        # Bad shape and impossible calendar dates both rejected at validation.
        # (Slash-separated dates like ``05/18/2026`` are path-routing matters,
        # not handler concerns — FastAPI returns 404 before the handler runs.)
        for bad in ["abcdefghij", "2026-13-01", "2026-02-30"]:
            r = self.client.get(f"/v1/days/{bad}")
            self.assertEqual(r.status_code, 422, f"expected 422 for {bad}, got {r.status_code}")

    def test_day_candidates_theme_filter(self):
        r = self.client.get("/v1/days/2026-05-18/candidates?theme=weight_loss_drugs")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["meta"]["total"], 2)
        for c in body["data"]:
            self.assertEqual(c["theme"], "weight_loss_drugs")

    def test_day_candidates_min_score_filter(self):
        r = self.client.get("/v1/days/2026-05-18/candidates?min_score=5")
        body = r.json()
        for c in body["data"]:
            self.assertGreaterEqual(c["layer4_weighted_score"], 5)

    def test_day_candidates_404_for_unknown_date(self):
        r = self.client.get("/v1/days/2099-01-01/candidates")
        self.assertEqual(r.status_code, 404)


class RankInDayOrderingTests(unittest.TestCase):
    """The orchestrator's 7-key sort assigns ``rank_in_day``; the API must
    serve candidates in that order so DOM position matches the rank chip
    rendered on each card. Pinned 2026-05-22 after the order-mismatch was
    spotted live (BAH=01, MMS=03, TTEK=04, WK=02 in DOM but the chips
    showed the 7-key permutation 1,3,4,2).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.briefs_dir = root / "briefs"
        cls.db_path = root / "briefs.db"
        seed_day_with_distinct_rank_order(cls.briefs_dir)
        rebuild_from_parquet(briefs_dir=cls.briefs_dir, db_path=cls.db_path)
        cls.app = create_app(cls.db_path)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_get_day_returns_candidates_in_rank_in_day_order(self):
        r = self.client.get("/v1/days/2026-05-21")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        tickers = [c["ticker"] for c in body["candidates"]]
        ranks = [c["rank_in_day"] for c in body["candidates"]]
        # Expected 7-key sort order seeded by the fixture
        self.assertEqual(tickers, ["AAA", "DDD", "BBB", "CCC", "EEE"])
        self.assertEqual(ranks, [1, 2, 3, 4, 5])

    def test_day_candidates_endpoint_also_orders_by_rank_in_day(self):
        r = self.client.get("/v1/days/2026-05-21/candidates")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        tickers = [c["ticker"] for c in body["data"]]
        self.assertEqual(tickers, ["AAA", "DDD", "BBB", "CCC", "EEE"])


if __name__ == "__main__":
    unittest.main()
