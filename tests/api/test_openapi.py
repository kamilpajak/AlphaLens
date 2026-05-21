"""Contract checks against the generated OpenAPI schema."""

from __future__ import annotations

import os
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

    def test_default_docs_reference_unprefixed_openapi(self):
        # Regression guard: a no-root-path app must NOT advertise ``/api/``
        # — that would break the existing direct-serve mode (e.g. SSH tunnel
        # straight to the api container) where no proxy strips a prefix.
        html = self.client.get("/docs").text
        self.assertIn("/openapi.json", html)
        self.assertNotIn("/api/openapi.json", html)


class OpenApiRootPathTests(unittest.TestCase):
    """``root_path`` makes Swagger HTML + OpenAPI servers honor the proxy prefix."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.briefs_dir = root / "briefs"
        cls.db_path = root / "briefs.db"
        seed_two_days(cls.briefs_dir)
        rebuild_from_parquet(briefs_dir=cls.briefs_dir, db_path=cls.db_path)
        cls.app = create_app(cls.db_path, root_path="/api")
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_openapi_servers_carry_root_path(self):
        servers = self.client.get("/openapi.json").json().get("servers", [])
        self.assertIn({"url": "/api"}, servers)

    def test_swagger_html_loads_prefixed_openapi(self):
        html = self.client.get("/docs").text
        self.assertIn("/api/openapi.json", html)

    def test_redoc_html_loads_prefixed_openapi(self):
        html = self.client.get("/redoc").text
        self.assertIn("/api/openapi.json", html)

    def test_internal_routes_unchanged(self):
        # The proxy strips ``/api/`` before forwarding, so the api still sees
        # bare paths like ``/v1/days``. ``root_path`` must not rewrite the
        # internal route table.
        self.assertEqual(self.client.get("/v1/days").status_code, 200)


class OpenApiRootPathEnvTests(unittest.TestCase):
    """``ALPHALENS_ROOT_PATH`` env var feeds the factory when no kwarg passed."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "briefs.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_env_var_picked_up(self):
        from unittest import mock

        from alphalens.api.app import ENV_ROOT_PATH

        with mock.patch.dict(os.environ, {ENV_ROOT_PATH: "/api"}):
            app = create_app(self.db_path)
        self.assertEqual(app.root_path, "/api")

    def test_kwarg_overrides_env_var(self):
        from unittest import mock

        from alphalens.api.app import ENV_ROOT_PATH

        with mock.patch.dict(os.environ, {ENV_ROOT_PATH: "/api"}):
            app = create_app(self.db_path, root_path="/v1")
        self.assertEqual(app.root_path, "/v1")

    def test_default_empty(self):
        from unittest import mock

        from alphalens.api.app import ENV_ROOT_PATH

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_ROOT_PATH, None)
            app = create_app(self.db_path)
        self.assertEqual(app.root_path, "")


if __name__ == "__main__":
    unittest.main()
