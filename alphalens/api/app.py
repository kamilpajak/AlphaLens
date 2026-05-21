"""FastAPI application factory.

``create_app()`` wires the cache DB path into ``app.state``, configures CORS
from ``$CORS_ORIGINS`` (comma-separated), and mounts the v1 route modules.
Uvicorn's ``--factory`` flag calls this entry point.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from alphalens.api import db as db_module
from alphalens.api.routes import candidates, days, health, stats, themes, tickers

ENV_CORS = "CORS_ORIGINS"
DEFAULT_CORS = "http://localhost:5173,http://localhost:8080"

API_VERSION = "1.0.0"
API_TITLE = "AlphaLens Briefs API"
API_DESCRIPTION = (
    "Read-only HTTP access to thematic briefs produced by the AlphaLens daily "
    "pipeline. The SQLite cache is rebuilt from "
    "`~/.alphalens/thematic_briefs/*.parquet` on each daily run."
)


def _parse_cors(value: str | None) -> list[str]:
    raw = value if value is not None else DEFAULT_CORS
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def create_app(db_path: str | os.PathLike[str] | None = None) -> FastAPI:
    resolved_db = db_module.resolve_db_path(db_path)

    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.db_path = Path(resolved_db)

    origins = _parse_cors(os.environ.get(ENV_CORS))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(days.router)
    app.include_router(candidates.router)
    app.include_router(themes.router)
    app.include_router(tickers.router)
    app.include_router(stats.router)

    return app
