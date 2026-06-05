"""Record the L3 golden-master fixtures for the news-ingest stage (Phase 3b).

ONE-TIME live capture of the ingest MERGE pipeline. Scope (see "Vendor
constraints" below): **GDELT (synthetic) + Polygon (live) + RSS (live)**. The
golden drives the REAL per-source parsers + the cross-source dedup / priority-
merge / recency-cap over a frozen capture:

  1. GDELT  -> UrlJsonCassette over a SYNTHETIC 2-bucket response  gdelt.json
  2. Polygon-> VendorCassette (get_news_range, trimmed to universe) cassettes_vendor/
  3. RSS    -> FeedCassette (rss._parse_feed)                       rss.json

## Vendor constraints (why not a full 4-source live capture)
* **GDELT free tier 429s aggressively** — a full 9-bucket live sweep from one
  IP reliably rate-limits mid-run (6/9 buckets failed even at 25s spacing),
  leaving missing cassettes (= fail-loud replay misses). So GDELT is driven by
  a small SYNTHETIC 2-bucket response that exercises the real ``gdelt.transform``
  parser (seendate parse, clean_title, id, schema) deterministically — the same
  synthetic-realistic-input approach the extract golden uses for templates.
* **EDGAR press-release ingest is high-volume** — a normal business day enriches
  ~160 in-universe 8-Ks (3 nested ``get_text`` each = ~485 cassettes + large
  EX-99.1 bodies), and its rows are recency-capped out by GDELT's "now" articles
  anyway. Excluded from THIS golden; the EDGAR parse path is covered by unit
  tests + the 3b-2 tenk gate already exercises ``SecEdgarClient.get_text``.

These two are deferred follow-ups (see PR "Behaviour notes"). Polygon + RSS are
captured LIVE (both reliable). The merge/dedup/cap integration — the headline
behaviour — is fully locked over all three.

    POLYGON_API_KEY=... uv run python -m scripts.record_golden_ingest
    # (run from apps/alphalens-research; GDELT synthetic + RSS keyless)
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from alphalens_pipeline.data.alt_data.polygon_client import PolygonClient
from alphalens_pipeline.thematic import news_ingest
from alphalens_pipeline.thematic.config.universe import load_input_universe
from alphalens_pipeline.thematic.sources import gdelt, polygon_news, rss
from alphalens_pipeline.thematic.sources.schema import empty_news_frame
from tests.golden.projection import ingest_projection
from tests.golden.url_cassette import RecordingFeed, UrlJsonCassette
from tests.golden.vendor_cassette import RecordingVendor

ASOF = dt.date(2026, 5, 29)
MAX_ITEMS = news_ingest.DEFAULT_MAX_ITEMS

_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "golden" / "fixtures" / "ingest_day"

# Synthetic GDELT: a 2-bucket subset with realistic artlist rows. The queries
# are arbitrary (they only determine the URL key); the articles carry the exact
# fields gdelt.transform reads (url / seendate / title / domain / language /
# sourcecountry / socialimage).
_GDELT_BUCKETS = {
    "quantum_ai": '("quantum computing" OR "AI accelerator")',
    "semiconductors": '("semiconductor" OR "chip foundry")',
}
_SYNTH_ARTICLES = {
    "quantum_ai": [
        {
            "url": "https://example.com/news/quantum-milestone",
            "seendate": "20260529T130000Z",
            "title": "Quantum computing startup reports error-correction milestone",
            "domain": "example.com",
            "language": "English",
            "sourcecountry": "US",
            "socialimage": "",
        },
        {
            "url": "https://example.com/news/ai-accelerator-launch",
            "seendate": "20260529T150000Z",
            "title": "New AI accelerator chip targets inference workloads",
            "domain": "example.com",
            "language": "English",
            "sourcecountry": "US",
            "socialimage": "",
        },
    ],
    "semiconductors": [
        {
            "url": "https://example.com/news/foundry-capacity",
            "seendate": "20260529T140000Z",
            "title": "Chip foundry expands advanced-node capacity",
            "domain": "example.com",
            "language": "English",
            "sourcecountry": "US",
            "socialimage": "",
        },
    ],
}


def _no_edgar(*, date: dt.date):
    """EDGAR-excluded stand-in matching ``_fetch_edgar_press_release(*, date)``."""
    return empty_news_frame()


def _write_synthetic_gdelt() -> None:
    """Author the GDELT cassette: build_query_url(query, window) -> {articles:[...]}.

    The URL key MUST match the one ``gdelt.fetch_theme`` builds at replay time —
    which now carries the explicit P1a single-day window ``startdatetime`` /
    ``enddatetime`` for ``ASOF`` (no more relative ``timespan``).
    """
    start = dt.datetime.combine(ASOF, dt.time.min, tzinfo=dt.UTC)
    end = start + dt.timedelta(days=1)
    store: dict[str, dict] = {}
    for theme, query in _GDELT_BUCKETS.items():
        url = gdelt.build_query_url(
            query=query,
            startdatetime=gdelt._format_datetime_for_gdelt(start),
            enddatetime=gdelt._format_datetime_for_gdelt(end),
        )
        store[url] = {"articles": _SYNTH_ARTICLES[theme]}
    (_FIXTURES / "gdelt.json").write_text(json.dumps(store, indent=2, sort_keys=True))


def _trim_polygon_cassettes(universe: set[str]) -> None:
    upper = {t.upper() for t in universe}
    vendor_dir = _FIXTURES / "cassettes_vendor"
    for path in vendor_dir.glob("*.json"):
        rec = json.loads(path.read_text())
        if rec.get("method") != "get_news_range":
            continue
        before = len(rec["payload"])
        rec["payload"] = [
            it
            for it in rec["payload"]
            if upper & {str(t).upper() for t in (it.get("tickers") or [])}
        ]
        path.write_text(json.dumps(rec, indent=2, sort_keys=True, ensure_ascii=False, default=str))
        print(
            f"  trimmed polygon cassette {path.name[:12]}: {before} -> {len(rec['payload'])} rows"
        )


def main() -> None:
    if not os.environ.get("POLYGON_API_KEY"):
        raise SystemExit("POLYGON_API_KEY must be set for the live capture")

    golden_dir = _FIXTURES / "golden"
    vendor_dir = _FIXTURES / "cassettes_vendor"
    for d in (golden_dir, vendor_dir):
        d.mkdir(parents=True, exist_ok=True)

    _write_synthetic_gdelt()
    gdelt_player = UrlJsonCassette(_FIXTURES / "gdelt.json")

    real_polygon = polygon_news.fetch_daily_news
    real_rss = rss.fetch_daily_news
    real_gdelt = gdelt.fetch_daily_news
    real_parse = rss._parse_feed
    rec_poly = RecordingVendor(PolygonClient(os.environ["POLYGON_API_KEY"]), vendor_dir)
    rec_feed = RecordingFeed(real_parse, _FIXTURES / "rss.json")

    with tempfile.TemporaryDirectory(prefix="ingest_record_") as tmp_root:
        tmp = Path(tmp_root)
        with (
            # GDELT: synthetic, subset to the 2 authored buckets (no live call).
            mock.patch.object(gdelt, "load_theme_buckets", lambda: dict(_GDELT_BUCKETS)),
            mock.patch.object(gdelt, "_http_get_json", gdelt_player),
            # Polygon + RSS: live, with per-source cache redirected to temp.
            mock.patch.object(
                news_ingest.polygon_news,
                "fetch_daily_news",
                functools.partial(real_polygon, cache_dir=tmp / "polygon", force=True),
            ),
            mock.patch.object(polygon_news, "get_default_polygon_client", lambda: rec_poly),
            mock.patch.object(
                news_ingest.rss,
                "fetch_daily_news",
                functools.partial(real_rss, cache_dir=tmp / "rss", force=True),
            ),
            mock.patch.object(rss, "_parse_feed", rec_feed),
            # GDELT source cache also redirected (its fetch_daily_news caches too).
            mock.patch.object(
                news_ingest.gdelt,
                "fetch_daily_news",
                functools.partial(real_gdelt, cache_dir=tmp / "gdelt", force=True),
            ),
            # EDGAR excluded from this golden (see module docstring).
            mock.patch.object(news_ingest, "_fetch_edgar_press_release", _no_edgar),
        ):
            df = news_ingest.ingest_daily(date=ASOF, cache_dir=tmp / "unified", max_items=MAX_ITEMS)

    _trim_polygon_cassettes(set(load_input_universe()))
    (golden_dir / "projection.json").write_text(
        json.dumps(ingest_projection(df), indent=2, sort_keys=True)
    )
    by_source = df.groupby("source").size().to_dict() if len(df) else {}
    print(
        f"captured {len(df)} unified rows; by_source={by_source}; "
        f"{len(list(vendor_dir.glob('*.json')))} vendor cassettes -> {_FIXTURES}"
    )


if __name__ == "__main__":
    main()
