"""Routes: /v1/candidates/{date}/{ticker}."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from alphalens.api.app import create_app
from alphalens.api.cache import rebuild_from_parquet
from tests.api._fixtures import seed_two_days


class CandidateRouteTests(unittest.TestCase):
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

    def test_fetch_single_candidate(self):
        r = self.client.get("/v1/candidates/2026-05-18/AAA")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["ticker"], "AAA")
        self.assertEqual(body["theme"], "quantum_computing")
        self.assertEqual(body["layer4_weighted_score"], 6)
        self.assertEqual(body["gates_passed"], ["polygon_news", "etf_holdings"])

    def test_ticker_case_insensitive(self):
        r = self.client.get("/v1/candidates/2026-05-18/aaa")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["ticker"], "AAA")

    def test_404_for_missing_pair(self):
        r = self.client.get("/v1/candidates/2026-05-18/ZZZ")
        self.assertEqual(r.status_code, 404)

    def test_422_for_malformed_date(self):
        r = self.client.get("/v1/candidates/garbage123/AAA")
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
