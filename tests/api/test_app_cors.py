"""CORS middleware boots safely for both explicit-origin and wildcard configs."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from alphalens.api.app import ENV_CORS, create_app


class CorsBootstrapTests(unittest.TestCase):
    def test_explicit_origins_keep_credentials(self):
        with patch.dict(
            os.environ,
            {ENV_CORS: "http://localhost:5173,https://briefs.example"},
            clear=False,
        ):
            app = create_app(Path(tempfile.gettempdir()) / "nope.db")
        cors_mw = next(mw for mw in app.user_middleware if mw.cls.__name__ == "CORSMiddleware")
        self.assertEqual(
            sorted(cors_mw.kwargs["allow_origins"]),
            ["http://localhost:5173", "https://briefs.example"],
        )
        self.assertTrue(cors_mw.kwargs["allow_credentials"])

    def test_wildcard_origin_drops_credentials_and_boots(self):
        # Without the wildcard guard Starlette would raise at construction.
        with patch.dict(os.environ, {ENV_CORS: "*"}, clear=False):
            app = create_app(Path(tempfile.gettempdir()) / "nope.db")
            with TestClient(app) as client:
                self.assertEqual(client.get("/healthz").status_code, 200)
        cors_mw = next(mw for mw in app.user_middleware if mw.cls.__name__ == "CORSMiddleware")
        self.assertEqual(cors_mw.kwargs["allow_origins"], ["*"])
        self.assertFalse(cors_mw.kwargs["allow_credentials"])


if __name__ == "__main__":
    unittest.main()
