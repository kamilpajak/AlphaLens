"""SEC EDGAR 8-K adapter for the thematic tool.

Wraps the existing ``alphalens_pipeline.edgar_detector.sources.edgar.SECEdgarSource`` so that
parsing logic, CIK resolution and rate-limiting are shared with the Layer 1
production edgar_detector. To avoid colliding with the live detector's deduplication
state, this adapter uses its own ``SeenEventStore`` at
``~/.alphalens/thematic_news/edgar/seen.db``.

Net effect: ~141 SEC requests per daily ingest (one Atom feed per universe
ticker) at the configured 0.15s spacing, well below SEC's 10 req/s ceiling.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from alphalens_pipeline.edgar_detector.sources.cik_loader import CIKLoader
from alphalens_pipeline.edgar_detector.sources.edgar import SECEdgarSource
from alphalens_pipeline.edgar_detector.storage import SeenEventStore
from alphalens_pipeline.edgar_detector.types import Event, FormType
from alphalens_pipeline.thematic.config.universe import load_input_universe
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS, empty_news_frame

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_news" / "edgar"


def _detect_events(*, tickers: list[str], cache_dir: Path) -> list[Event]:
    """Run one SEC EDGAR Atom-feed sweep and return new 8-K events.

    Isolated in its own function so tests can patch this single seam without
    spinning up real HTTP traffic. SEC transport (UA + throttle) is owned by
    the shared :class:`SecEdgarClient` singleton.
    """
    cik_cache = cache_dir / "company_tickers.json"
    cik_cache.parent.mkdir(parents=True, exist_ok=True)
    seen_db = cache_dir / "seen.db"

    loader = CIKLoader(cache_path=cik_cache)
    loader.load()
    store = SeenEventStore(db_path=seen_db)
    source = SECEdgarSource(
        tickers=tickers,
        config={"form_filter": [FormType.FORM_8K]},
        store=store,
        cik_loader=loader,
    )
    try:
        return source.detect()
    finally:
        store.close()


def transform(events: Iterable[Event]) -> pd.DataFrame:
    """Normalise edgar_detector ``Event`` records to the unified ``NEWS_COLUMNS`` schema."""
    rows: list[dict] = []
    for ev in events:
        items_str = (ev.raw_data or {}).get("items") or ""
        keywords = [s.strip() for s in items_str.split(",") if s.strip()]
        extra = {
            "form_type": ev.form_type.value,
            "accession_number": ev.accession_number,
            "raw": ev.raw_data or {},
        }
        ts = pd.Timestamp(ev.filed_at)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        rows.append(
            {
                "id": ev.accession_number,
                "source": "edgar",
                "timestamp": ts,
                "tickers": [ev.ticker.upper()],
                "title": f"{ev.ticker} {ev.form_type.value} ({items_str})".strip(),
                "body": "",
                "url": ev.url,
                "keywords": keywords,
                "extra": json.dumps(extra, ensure_ascii=False, default=str),
            }
        )

    if not rows:
        return empty_news_frame()
    df = pd.DataFrame(rows, columns=NEWS_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def fetch_daily_news(
    *,
    date: dt.date,
    universe: Iterable[str] | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    lookback_days: int = 2,
    force: bool = False,
) -> pd.DataFrame:
    """Detect 8-K filings for the input universe and cache one day's slice."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{date.isoformat()}.parquet"
    if cache_path.exists() and not force:
        return pd.read_parquet(cache_path)

    universe_set = {t.upper() for t in universe} if universe is not None else load_input_universe()
    events = _detect_events(tickers=sorted(universe_set), cache_dir=cache_dir)
    events = [e for e in events if e.ticker.upper() in universe_set]

    df = transform(events)
    if len(df) > 0:
        anchor = pd.Timestamp(date, tz="UTC")
        lo = anchor - pd.Timedelta(days=lookback_days)
        hi = anchor + pd.Timedelta(days=1)
        df = df[(df["timestamp"] >= lo) & (df["timestamp"] < hi)].reset_index(drop=True)

    df.to_parquet(cache_path, index=False)
    return df
