"""Routes: /healthz, /readyz, /v1/days, /v1/days/{date}, /v1/days/{date}/candidates."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from alphalens.api.app import create_app
from alphalens.api.cache import rebuild_from_parquet
from tests.api._fixtures import seed_two_days


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
        r = self.client.get("/v1/days/2026-13-99")
        # Format check passes (length+dashes); SQL returns no rows -> 404 not 422.
        # But /v1/days/abc should be 422 via validate_date.
        r2 = self.client.get("/v1/days/abcdefghij")
        self.assertEqual(r2.status_code, 422)

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


if __name__ == "__main__":
    unittest.main()
