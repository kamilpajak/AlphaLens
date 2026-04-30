"""Backfill delisted-ticker events for the 2021-04-19 → 2024-04-18 gap.

The existing `alphalens/archive/screeners/lean/lean_project/delisted_universe.yaml`
only covers 2024-04-19 → 2026-04-17 (1114 events). The Layer 2b backtest
runs 2021-04-19 → 2026-04-17, so we need events from the earlier ~3 years
to run the survivorship_pit diagnostic battery (C2 selection bias + C3
mid-holding wipeout audit).

Paginates `PolygonClient.delisted_tickers(delisted_gte, delisted_lte)`,
caches each page to disk for resumability, and emits a single parquet at
`~/.alphalens/survivorship/delisted_2021_2026.parquet` that merges the
backfill with the existing YAML fixture.

Polygon Basic tier: 5 req/min. Expected ~6–10 pages at 1000 events each
→ under 2 min wall time. Resumable if interrupted.

Usage:
    .venv/bin/python scripts/backfill_delisted_2021_2024.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens.archive.screeners.lean.polygon_client import PolygonClient  # noqa: E402

CACHE_DIR = Path.home() / ".alphalens" / "survivorship"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

BACKFILL_START = "2021-04-19"
BACKFILL_END = "2024-04-18"  # existing YAML starts 2024-04-19

OUT_PARQUET = CACHE_DIR / "delisted_2021_2026.parquet"
EXISTING_YAML = (
    Path(__file__).resolve().parent.parent
    / "alphalens"
    / "screeners"
    / "lean"
    / "lean_project"
    / "delisted_universe.yaml"
)


def _load_cached_page(page_num: int) -> list[dict] | None:
    """Per-page cache so interrupted runs resume without re-fetching."""
    cache = CACHE_DIR / f"delisted_page_{page_num}.json"
    if not cache.exists():
        return None
    return json.loads(cache.read_text())


def _save_cached_page(page_num: int, rows: list[dict]) -> None:
    cache = CACHE_DIR / f"delisted_page_{page_num}.json"
    cache.write_text(json.dumps(rows))


def fetch_backfill(client: PolygonClient) -> list[dict]:
    """Stream all delisted events in the backfill window, caching per page.

    `PolygonClient.delisted_tickers` is itself paginated via the Polygon
    `next_url` mechanism. We materialise into flat row dicts and chunk to
    disk every 1000 rows so we can resume.
    """
    all_rows: list[dict] = []
    chunk: list[dict] = []
    page_num = 0

    cached = _load_cached_page(page_num)
    while cached is not None:
        all_rows.extend(cached)
        page_num += 1
        cached = _load_cached_page(page_num)

    if all_rows:
        print(f"resumed from cache: {len(all_rows)} rows across {page_num} page(s)")
        # Continue fetching remaining — we don't know how many pages remain
        # so just stream through and append anything the cache misses.

    seen_ids: set[str] = {f"{r.get('ticker')}|{r.get('delisted_utc')}" for r in all_rows}

    print(f"streaming delisted_tickers ({BACKFILL_START} → {BACKFILL_END}, CS/stocks) …")
    for row in client.delisted_tickers(
        market="stocks",
        type_="CS",
        delisted_gte=BACKFILL_START,
        delisted_lte=BACKFILL_END,
    ):
        key = f"{row.get('ticker')}|{row.get('delisted_utc')}"
        if key in seen_ids:
            continue
        seen_ids.add(key)
        chunk.append(row)
        if len(chunk) >= 1000:
            _save_cached_page(page_num, chunk)
            all_rows.extend(chunk)
            page_num += 1
            print(f"  cached page {page_num}, total rows {len(all_rows)}")
            chunk = []

    if chunk:
        _save_cached_page(page_num, chunk)
        all_rows.extend(chunk)
        print(f"  cached final page {page_num + 1}, total rows {len(all_rows)}")

    return all_rows


def _classify_reason(name: str, ticker: str) -> str:
    """Heuristic: merger/acquisition vs unknown.

    Polygon doesn't expose the delisting reason directly. Warrant/rights
    suffixes (-W, -R, -U, .U, .R) typically belong to SPAC restructurings
    which end in acquisition. "Acquisition" / "merger" in the security
    name is a strong signal. Everything else → unknown.
    """
    n = str(name or "").lower()
    t = str(ticker or "").upper()
    if any(k in n for k in ("acquisition", "merger ", "spac", "business combination")):
        return "acquisition"
    if t.endswith(("-W", "-WS", "-R", "-RT", "-U", "-UN")) or t.endswith((".W", ".U", ".R")):
        return "acquisition"  # SPAC warrant/right — follow their parent SPAC
    return "unknown"


def _load_existing_yaml(path: Path) -> list[dict]:
    """Read the existing delisted_universe.yaml (2024-04-19 → 2026-04-17)."""
    if not path.exists():
        print(f"  (no existing YAML at {path}, skipping merge)")
        return []
    with path.open() as fh:
        data = yaml.safe_load(fh) or {}
    rows = []
    for entry in data.get("delisted", []) or []:
        rows.append(
            {
                "ticker": entry.get("ticker"),
                "delisted_utc": str(entry.get("delisted")),
                "name": entry.get("name", ""),
            }
        )
    return rows


def build_merged_parquet(backfill_rows: list[dict], yaml_rows: list[dict]) -> pd.DataFrame:
    """Merge backfill + existing YAML into a single typed DataFrame."""
    combined: dict[tuple[str, str], dict] = {}

    for row in backfill_rows + yaml_rows:
        ticker = row.get("ticker")
        delisted = row.get("delisted_utc")
        # Polygon occasionally returns ticker=True for a handful of rows
        # (data-quality quirk) — drop anything that isn't a proper string.
        if not isinstance(ticker, str) or not ticker:
            continue
        if not delisted:
            continue
        delisted_date = str(delisted)[:10]
        key = (ticker, delisted_date)
        if key in combined:
            continue
        name = row.get("name") or ""
        if not isinstance(name, str):
            name = str(name)
        combined[key] = {
            "ticker": ticker,
            "delisted_date": delisted_date,
            "name": name,
            "reason": _classify_reason(name, ticker),
        }

    df = pd.DataFrame(list(combined.values()))
    if df.empty:
        return df
    df["delisted_date"] = pd.to_datetime(df["delisted_date"])
    df = df.sort_values(["delisted_date", "ticker"]).reset_index(drop=True)
    return df


def main() -> None:
    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        sys.exit("POLYGON_API_KEY not set in env / .env")

    # Basic tier = 5 req/min. If you have Starter (100/min) the client will
    # just move faster; either way the cache makes re-runs cheap.
    client = PolygonClient(api_key=api_key, rate_limit_per_min=5)

    backfill = fetch_backfill(client)
    print(f"\nbackfill rows (2021-04-19 → 2024-04-18): {len(backfill)}")

    existing = _load_existing_yaml(EXISTING_YAML)
    print(f"existing YAML rows (2024-04-19 → 2026-04-17): {len(existing)}")

    df = build_merged_parquet(backfill, existing)
    print(f"merged unique rows: {len(df)}")

    if not df.empty:
        earliest = df["delisted_date"].min().date().isoformat()
        latest = df["delisted_date"].max().date().isoformat()
        reasons = df["reason"].value_counts().to_dict()
        print(f"date range: {earliest} → {latest}")
        print(f"reason breakdown: {reasons}")

    df.to_parquet(OUT_PARQUET, index=False)
    print(f"\nwrote {OUT_PARQUET}")


if __name__ == "__main__":
    main()
