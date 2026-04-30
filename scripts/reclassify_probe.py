"""Re-classify cached probe details with the improved theme_classifier.

Reads ~/.alphalens/survivorship/details_*.json (populated by survivorship_probe.py)
and ~/.alphalens/survivorship/grouped_*.json (2021 snapshot for liquidity/price).
Writes delisted_thematic_candidates_v2.csv with higher-precision filtering.

Also prints a sanity check: for a known list of high-profile thematic delistings,
does our classifier pick them up?

Usage: .venv/bin/python scripts/reclassify_probe.py
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from theme_classifier import classify_theme

CACHE_DIR = Path.home() / ".alphalens" / "survivorship"

# High-profile thematic delistings 2021-2026 — sanity check set.
# If classifier misses these, filter needs refinement.
KNOWN_DELISTINGS = [
    # Biotech
    ("ARNA", "Arena Pharmaceuticals", "biotech", "acquired Pfizer 2022"),
    ("XENT", "Intersect ENT", "biotech", "acquired Medtronic 2021"),
    ("EIGR", "Eiger BioPharmaceuticals", "biotech", "Ch 11 2024"),
    ("CARA", "Cara Therapeutics", "biotech", "liquidation 2024"),
    ("AADI", "Aadi Bioscience", "biotech", "delisted"),
    ("FRLN", "Freeline Therapeutics", "biotech", "delisted 2023"),
    # AI / tech adjacent
    ("MTTR", "Matterport", "ai", "acquired CoStar 2024"),
    ("ARVL", "Arrival", "ai", "delisted 2024 (EV+autonomy)"),
    # Semis
    ("SMTX", "SMTek", "semis", "illustrative"),
    ("ATVI", "Activision Blizzard", None, "gaming not thematic — NEGATIVE test"),
]


def main() -> None:
    # Load 2021 snapshot for dollar volume / price
    snap_file = CACHE_DIR / "grouped_2021-06-01.json"
    if not snap_file.exists():
        print(f"! missing {snap_file} — run survivorship_probe.py first")
        return
    snap = json.loads(snap_file.read_text())
    print(f"loaded {len(snap)} tickers from 2021-06-01 snapshot")

    detail_files = sorted(CACHE_DIR.glob("details_*.json"))
    print(f"reclassifying {len(detail_files)} cached ticker details …\n")

    thematic_rows = []
    theme_counts: Counter = Counter()
    confidence_counts: Counter = Counter()
    examined = 0
    has_delisted_utc = 0

    for df in detail_files:
        ticker = df.stem.replace("details_", "")
        try:
            d = json.loads(df.read_text())
        except json.JSONDecodeError:
            continue
        if not d:
            continue
        examined += 1

        sic_code = d.get("sic_code")
        sic_desc = d.get("sic_description")
        name = d.get("name", "")
        delisted = d.get("delisted_utc") or ""
        if delisted:
            has_delisted_utc += 1

        match = classify_theme(sic_code, sic_desc, name)
        if match.theme is None:
            continue

        theme_counts[match.theme] += 1
        confidence_counts[match.confidence] += 1

        snap_row = snap.get(ticker) or {}
        dv_2021 = snap_row.get("dollar_volume", 0.0)
        close_2021 = snap_row.get("close", 0.0)

        thematic_rows.append(
            {
                "ticker": ticker,
                "theme": match.theme,
                "confidence": match.confidence,
                "reason": match.reason,
                "name": name,
                "sic_code": sic_code or "",
                "sic_description": sic_desc or "",
                "delisted_utc": delisted,
                "dollar_volume_2021_jun": dv_2021,
                "close_2021_jun": close_2021,
            }
        )

    # Output summary
    print("=== Summary ===")
    print(f"  examined: {examined} tickers (populated details)")
    print(f"  with delisted_utc populated: {has_delisted_utc}")
    print(f"  thematic matches: {len(thematic_rows)}")
    print(f"  by theme: {dict(theme_counts)}")
    print(f"  by confidence: {dict(confidence_counts)}")

    # Filter to high+medium confidence only for the main report
    high_med = [r for r in thematic_rows if r["confidence"] in ("high", "medium")]
    print(f"  high+medium only: {len(high_med)}")

    # Sort by dollar volume desc
    high_med.sort(key=lambda r: -r["dollar_volume_2021_jun"])

    # Print top 40 by $ADV
    print("\n=== Top 40 thematic delisted by 2021 dollar volume ===")
    print(f"{'ticker':<8}{'theme':<8}{'conf':<8}{'$ADV':>10}  {'delisted':<22}  {'name':<40}")
    print("-" * 105)
    for r in high_med[:40]:
        print(
            f"{r['ticker']:<8}{r['theme']:<8}{r['confidence']:<8}"
            f"{r['dollar_volume_2021_jun'] / 1e6:>8.1f}M  "
            f"{(r['delisted_utc'] or '?')[:22]:<22}  {r['name'][:40]}"
        )

    # Write full CSV
    out_csv = CACHE_DIR / "delisted_thematic_candidates_v2.csv"
    with out_csv.open("w", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "ticker",
                "theme",
                "confidence",
                "reason",
                "name",
                "sic_code",
                "sic_description",
                "delisted_utc",
                "dollar_volume_2021_jun",
                "close_2021_jun",
            ],
        )
        w.writeheader()
        for r in thematic_rows:
            w.writerow(r)
    print(f"\nWrote {out_csv} ({len(thematic_rows)} rows)")

    # Sanity check: known high-profile delistings
    print("\n=== Sanity check — known high-profile delistings ===")
    for ticker, _expected_name, expected_theme, note in KNOWN_DELISTINGS:
        df = CACHE_DIR / f"details_{ticker}.json"
        if df.exists():
            d = json.loads(df.read_text())
            if d:
                m = classify_theme(d.get("sic_code"), d.get("sic_description"), d.get("name", ""))
                status = "✓" if m.theme == expected_theme else "✗"
                print(
                    f"  [{status}] {ticker:6s} expected={expected_theme or 'None':8s} got={m.theme or 'None':8s} "
                    f"conf={m.confidence:7s}  ({note})"
                )
            else:
                print(
                    f"  [?] {ticker:6s} — empty details (not in shortlist or API returned nothing)"
                )
        else:
            print(f"  [?] {ticker:6s} — not in fetched details cache")


if __name__ == "__main__":
    main()
