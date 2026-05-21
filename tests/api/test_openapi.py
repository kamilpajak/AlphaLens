"""Contract checks against the generated OpenAPI schema."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from alphalens.api.app import API_VERSION, create_app
from alphalens.api.cache import rebuild_from_parquet
from alphalens.api.schema import CANDIDATE_COLUMN_NAMES
from tests.api._fixtures import seed_two_days


class OpenApiContractTests(unittest.TestCase):
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

    def test_openapi_served(self):
        r = self.client.get("/openapi.json")
        self.assertEqual(r.status_code, 200)
        schema = r.json()
        self.assertEqual(schema["info"]["version"], API_VERSION)
        self.assertEqual(schema["openapi"][:3], "3.1")

    def test_all_routes_documented(self):
        r = self.client.get("/openapi.json")
        paths = set(r.json()["paths"].keys())
        for expected in [
            "/healthz",
            "/readyz",
            "/v1/days",
            "/v1/days/{date}",
            "/v1/days/{date}/candidates",
            "/v1/candidates/{date}/{ticker}",
            "/v1/themes",
            "/v1/themes/{theme}/candidates",
            "/v1/tickers/{ticker}/history",
            "/v1/stats",
        ]:
            self.assertIn(expected, paths, f"missing path {expected} in OpenAPI")

    def test_candidate_schema_has_every_column(self):
        r = self.client.get("/openapi.json")
        schema = r.json()
        candidate_schema = schema["components"]["schemas"]["Candidate"]
        documented = set(candidate_schema["properties"].keys())
        missing = set(CANDIDATE_COLUMN_NAMES) - documented
        self.assertEqual(missing, set(), f"undocumented columns: {missing}")

    def test_swagger_and_redoc_reachable(self):
        self.assertEqual(self.client.get("/docs").status_code, 200)
        self.assertEqual(self.client.get("/redoc").status_code, 200)


if __name__ == "__main__":
    unittest.main()
