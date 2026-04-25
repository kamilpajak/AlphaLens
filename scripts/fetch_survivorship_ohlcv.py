"""Fetch OHLCV for delisted thematic candidates and write Lean CSV zips.

Reads ~/.alphalens/survivorship/delisted_thematic_candidates_v2.csv,
applies strict name-based biotech filter (therapeutics/biopharm/biosciences/etc),
fetches Polygon ticker_range for each from 2021-06-01 to delisted_utc,
writes to ~/.alphalens/survivorship/lean_data/equity/usa/daily/<ticker>.zip.

Skip if <60 bars (too little for momentum score).

Usage: .venv/bin/python scripts/fetch_survivorship_ohlcv.py
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens.screeners.lean.lean_csv_writer import DailyBar, LeanCsvWriter  # noqa: E402
from alphalens.screeners.lean.polygon_client import PolygonClient  # noqa: E402

SURV_DIR = Path.home() / ".alphalens" / "survivorship"
LEAN_DATA = SURV_DIR / "lean_data"
LEAN_DATA.mkdir(parents=True, exist_ok=True)

# Fixed start of window (bounded by Polygon plan entitlement).
START_DATE = "2021-06-01"
FALLBACK_END = "2026-04-17"


# Strict biotech name keywords — drop "pharm" only (too broad, catches pharmacies).
BIOTECH_NAME_PATTERNS = [
    "therapeutics",
    "therapeutic ",
    "biosciences",
    "bioscience",
    "biopharm",
    "biogen",
    "oncology",
    "genomics",
    "gene therap",
    "immuno",
]
# Semis we accept (filter false positives like Desktop Metal / SPI Energy)
SEMIS_WHITELIST_KEYWORDS = ["photon", "semiconduct", "chip", "neophoton"]
# AI we accept
AI_WHITELIST_KEYWORDS = ["robot", "artificial intel", "machine learn", "autonom"]
# AI hard excludes (ETFs, funds)
AI_EXCLUDES = ["ishares", "etf", "fund"]


def should_fetch(ticker: str, theme: str, name: str, confidence: str) -> bool:
    n = name.lower()
    if theme == "biotech":
        # Accept high/medium SIC matches unconditionally, low only if name keyword
        if confidence in ("high", "medium"):
            return True
        return any(kw in n for kw in BIOTECH_NAME_PATTERNS)
    if theme == "semis":
        if any(ex in n for ex in ("energy", "desktop", "battery")):
            return False
        return confidence in ("high", "medium") or any(kw in n for kw in SEMIS_WHITELIST_KEYWORDS)
    if theme == "ai":
        if any(ex in n for ex in AI_EXCLUDES):
            return False
        return any(kw in n for kw in AI_WHITELIST_KEYWORDS) or confidence == "high"
    if theme == "quantum":
        return True
    return False


def delisted_or_end(iso: str | None) -> str:
    if not iso:
        return FALLBACK_END
    # Polygon returns "2023-10-09T04:00:00Z"
    return iso.split("T")[0]


def main() -> None:
    api_key = os.environ["POLYGON_API_KEY"]
    client = PolygonClient(api_key=api_key, rate_limit_per_min=100)
    writer = LeanCsvWriter(LEAN_DATA)

    csv_path = SURV_DIR / "delisted_thematic_candidates_v2.csv"
    rows = list(csv.DictReader(csv_path.open()))
    to_fetch = [
        r for r in rows if should_fetch(r["ticker"], r["theme"], r["name"], r["confidence"])
    ]

    by_theme: dict[str, int] = {}
    for r in to_fetch:
        by_theme[r["theme"]] = by_theme.get(r["theme"], 0) + 1
    print(f"Total candidates: {len(rows)}, will fetch: {len(to_fetch)} ({by_theme})")

    fetched = []
    skipped_short = []
    skipped_error = []
    for i, r in enumerate(to_fetch, 1):
        ticker = r["ticker"]
        end = delisted_or_end(r["delisted_utc"])
        zip_path = writer.path_for(ticker)
        # Skip if we already have the zip (idempotent).
        if zip_path.exists():
            existing = writer.read_bars(ticker)
            if len(existing) >= 60:
                fetched.append((ticker, r["theme"], len(existing), end))
                continue
        try:
            bars = client.ticker_range(ticker, START_DATE, end, adjusted=True)
        except Exception as e:
            print(f"  [{i}/{len(to_fetch)}] {ticker:8s} ERROR: {str(e)[:100]}")
            skipped_error.append((ticker, str(e)[:80]))
            continue

        if len(bars) < 60:
            print(f"  [{i}/{len(to_fetch)}] {ticker:8s} only {len(bars)} bars, skip")
            skipped_short.append((ticker, len(bars)))
            continue

        # Convert to DailyBar
        daily_bars = [
            DailyBar(
                date=datetime.utcfromtimestamp(b.timestamp_ms / 1000).strftime("%Y%m%d"),
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
            )
            for b in bars
        ]
        writer.write_bars(ticker, daily_bars)
        fetched.append((ticker, r["theme"], len(daily_bars), end))
        print(f"  [{i}/{len(to_fetch)}] {ticker:8s} {r['theme']:8s} {len(daily_bars)} bars → {end}")

    # Summary
    print("\n=== Summary ===")
    print(f"  fetched: {len(fetched)}")
    print(f"  skipped (too short): {len(skipped_short)}")
    print(f"  skipped (error): {len(skipped_error)}")

    # Save a manifest
    manifest = {
        "fetched": [
            {"ticker": t, "theme": th, "bars": b, "end_date": e} for t, th, b, e in fetched
        ],
        "skipped_short": skipped_short,
        "skipped_error": skipped_error,
    }
    manifest_path = SURV_DIR / "fetched_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"  manifest: {manifest_path}")


if __name__ == "__main__":
    main()
