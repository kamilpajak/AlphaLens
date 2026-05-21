"""REST API for thematic briefs (FastAPI + SQLite cache).

Reads ``~/.alphalens/thematic_briefs/*.parquet`` produced by ``alphalens thematic
brief`` and serves them as a versioned JSON API. The SQLite cache at
``~/.alphalens/api/briefs.db`` is rebuilt incrementally from parquet on each
daily run (``alphalens api rebuild-cache``), and FastAPI handlers read from it
synchronously in the threadpool.

Design memo: docs/research/rest_api_design_2026_05_21.md
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
