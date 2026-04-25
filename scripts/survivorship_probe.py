"""Test B — survivorship bias probe for Layer 2b curated universe.

Approach:
1. grouped_daily('2021-04-19') → all tickers trading on backtest start day.
2. grouped_daily(recent) → all tickers trading today.
3. Diff → tickers that existed 2021-04-19 and disappeared by now.
4. Filter to liquid small/mid caps ($300M–$10B proxy via dollar volume).
5. For shortlist, fetch sic_description + delisted_utc to confirm thematic match.
6. Output: CSV of delisted thematic small/mid caps that were alive in 2021.

Usage: .venv/bin/python scripts/survivorship_probe.py
"""

from __future__ import annotations

import csv
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens.screeners.lean.polygon_client import PolygonClient  # noqa: E402

CACHE_DIR = Path.home() / ".alphalens" / "survivorship"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

START_DATE = (
    "2021-06-01"  # Polygon plan boundary; original backtest start 2021-04-19 is past entitlement
)
END_DATE = "2026-04-17"

# Keyword heuristic for thematic shortlist. Crude but fast.
THEMATIC_KEYWORDS = [
    # biotech / gene therapy
    "therap",
    "pharma",
    "biosci",
    "bio ",
    "gene",
    "cell ",
    "rna",
    "crispr",
    "oncolog",
    "neuro",
    "cancer",
    "antibod",
    "clinical",
    # semis
    "semicond",
    "silicon",
    "microchip",
    "chip",
    "wafer",
    "litho",
    "photon",
    # AI / quantum / robotics
    "artificial intel",
    "machine lear",
    "quantum",
    "robot",
    "autonom",
    "a.i.",
    "cogniti",
    "neural",
    "cloud comp",
    "data center",
]


@dataclass
class Candidate:
    ticker: str
    name: str
    dollar_volume_2021: float
    close_2021: float
    sic_description: str | None = None
    delisted_utc: str | None = None
    theme: str | None = None


def grouped_snapshot(client: PolygonClient, date: str) -> dict[str, dict]:
    """Return {ticker: {close, volume, dollar_volume}} from grouped_daily."""
    cache = CACHE_DIR / f"grouped_{date}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    bars = client.grouped_daily(date)
    out = {
        b.ticker: {"close": b.close, "volume": b.volume, "dollar_volume": b.close * b.volume}
        for b in bars
    }
    cache.write_text(json.dumps(out))
    return out


def match_theme(sic: str | None, name: str) -> str | None:
    """Coarse mapping: SIC + name keywords → theme label."""
    sic = (sic or "").lower()
    n = name.lower()
    if any(k in sic for k in ("biolog", "pharma", "medicinal", "drug")) or any(
        k in n for k in ("therap", "biosci", "gene ", "gene-", "onc", "crispr", "rna")
    ):
        return "biotech"
    if "semicond" in sic or any(
        k in n for k in ("semicond", "silicon", "wafer", "litho", "photon", "microchip")
    ):
        return "semis"
    if "services-computer" in sic or "prepackaged software" in sic:
        if any(
            k in n
            for k in (
                "a.i.",
                "artificial",
                "machine lear",
                "quantum",
                "robot",
                "autonom",
                "cogniti",
                "neural",
                "cloud",
                "data center",
            )
        ):
            return "ai"
    if any(k in n for k in ("quantum", "quantum comp")):
        return "quantum"
    return None


def fetch_ticker_details(client: PolygonClient, ticker: str) -> dict:
    """Cached per-ticker details call."""
    cache = CACHE_DIR / f"details_{ticker}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    # Use search with active=false to fetch delisted records with delisted_utc
    # Workaround for the bulk endpoint not populating that field.
    try:
        resp = client._get(
            "/v3/reference/tickers",
            params={"search": ticker, "active": "false", "limit": 10},
        )
        for r in resp.get("results", []):
            if r.get("ticker") == ticker:
                cache.write_text(json.dumps(r))
                return r
    except Exception as e:
        print(f"  ! {ticker}: {e}")
    # Fallback — try without active filter, with historical date
    try:
        resp = client._get(f"/v3/reference/tickers/{ticker}", params={"date": "2021-04-19"})
        r = resp.get("results") or {}
        cache.write_text(json.dumps(r))
        return r
    except Exception as e:
        print(f"  ! {ticker} fallback: {e}")
    cache.write_text(json.dumps({}))
    return {}


def main() -> None:
    api_key = os.environ["POLYGON_API_KEY"]
    # Try high rate limit first — Polygon Advanced is unlimited, Starter is 100/min.
    client = PolygonClient(api_key=api_key, rate_limit_per_min=100)

    print(f"[1/4] grouped_daily {START_DATE} …")
    snap_2021 = grouped_snapshot(client, START_DATE)
    print(f"      {len(snap_2021)} tickers trading")

    print(f"[2/4] grouped_daily {END_DATE} …")
    snap_2026 = grouped_snapshot(client, END_DATE)
    print(f"      {len(snap_2026)} tickers trading")

    disappeared = set(snap_2021) - set(snap_2026)
    print(f"[3/4] tickers in 2021 but not 2026: {len(disappeared)}")

    # Liquid filter — dollar volume > $3M/day (proxy for small/mid cap with real trading).
    shortlist: list[Candidate] = []
    for t in disappeared:
        row = snap_2021[t]
        dv = row["dollar_volume"]
        if dv < 3_000_000:
            continue
        if row["close"] < 2.0:  # exclude penny stocks
            continue
        # Name keyword prefilter — avoid 7000+ API calls, keep likely thematics
        # (we'll look up names later; for now just keep all liquid candidates)
        shortlist.append(
            Candidate(
                ticker=t,
                name="",  # filled in next step
                dollar_volume_2021=dv,
                close_2021=row["close"],
            )
        )
    shortlist.sort(key=lambda c: -c.dollar_volume_2021)
    print(f"      after liquidity filter ($3M+ ADV, $2+ price): {len(shortlist)}")

    # Cap to top-N by dollar volume to keep API calls bounded.
    TOP_N = 2000  # ~20 min at 100 req/min
    shortlist = shortlist[:TOP_N]
    print(f"      fetching details for top {len(shortlist)} by $ADV …")

    print("[4/4] per-ticker details …")
    thematic: list[Candidate] = []
    for i, cand in enumerate(shortlist):
        details = fetch_ticker_details(client, cand.ticker)
        cand.name = details.get("name", "")
        cand.sic_description = details.get("sic_description")
        cand.delisted_utc = details.get("delisted_utc")
        cand.theme = match_theme(cand.sic_description, cand.name)
        if cand.theme:
            thematic.append(cand)
        if (i + 1) % 100 == 0:
            print(f"      {i + 1}/{len(shortlist)} checked; {len(thematic)} thematic so far")

    print(f"\n=== Thematic delisted small/mid caps: {len(thematic)} ===")
    for c in sorted(thematic, key=lambda c: -c.dollar_volume_2021):
        print(
            f"  {c.ticker:8s} {c.theme:8s} ${c.dollar_volume_2021 / 1e6:6.1f}M  "
            f"delisted={c.delisted_utc or '?':24s}  {c.name[:50]}"
        )

    # Write CSV
    out_csv = CACHE_DIR / "delisted_thematic_candidates.csv"
    with out_csv.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "ticker",
                "theme",
                "name",
                "sic_description",
                "dollar_volume_2021_apr",
                "close_2021_apr",
                "delisted_utc",
            ]
        )
        for c in sorted(thematic, key=lambda c: -c.dollar_volume_2021):
            w.writerow(
                [
                    c.ticker,
                    c.theme,
                    c.name,
                    c.sic_description,
                    f"{c.dollar_volume_2021:.0f}",
                    f"{c.close_2021:.2f}",
                    c.delisted_utc or "",
                ]
            )
    print(f"\nWrote {out_csv}")


if __name__ == "__main__":
    main()
