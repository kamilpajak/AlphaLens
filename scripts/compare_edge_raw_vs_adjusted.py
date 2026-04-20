"""Compare EDGE spread on raw vs. adjusted daily prices for tickers with splits.

Lean CSV is adjusted (splits + dividends). Polygon ticker_range with
adjusted=False gives raw. For 10 tickers with known split events, compute
EDGE rolling on both sets, compare daily estimates.

Output: `docs/backtest/edge_raw_vs_adjusted.md` with per-ticker median delta
(adjusted − raw) and pre/during/post-split breakdown.

Usage:
    .venv/bin/python scripts/compare_edge_raw_vs_adjusted.py
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens.backtest.spread_estimator import edge_spread  # noqa: E402
from alphalens.screeners.lean.polygon_client import PolygonClient  # noqa: E402


DOC_OUT = Path(__file__).resolve().parent.parent / "docs" / "backtest" / "edge_raw_vs_adjusted.md"


# (ticker, split_execution_date, split_ratio_descr) — publicly announced splits.
SPLIT_EVENTS: list[tuple[str, date, str]] = [
    ("NVDA", date(2024, 6, 10), "10-for-1"),
    ("TSLA", date(2022, 8, 25), "3-for-1"),
    ("AMZN", date(2022, 6, 6), "20-for-1"),
    ("GOOGL", date(2022, 7, 18), "20-for-1"),
    ("SHOP", date(2022, 6, 29), "10-for-1"),
    ("AAPL", date(2020, 8, 31), "4-for-1"),
    ("DXCM", date(2022, 6, 13), "4-for-1"),
    ("PANW", date(2022, 9, 14), "3-for-1"),
    ("CPRT", date(2022, 8, 15), "2-for-1"),
    ("MNST", date(2023, 3, 28), "2-for-1"),
]


def _frame_from_bars(bars) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(
        {
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        },
        index=pd.to_datetime([b.timestamp_ms for b in bars], unit="ms"),
    ).sort_index()
    return df


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window-days",
        type=int,
        default=60,
        help="Window on each side of the split (±N trading days)",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        print("ERROR: POLYGON_API_KEY not set", file=sys.stderr)
        return 2

    client = PolygonClient(api_key=api_key, rate_limit_per_min=500)

    lines = []
    lines.append("# EDGE: raw vs. adjusted prices")
    lines.append("")
    lines.append(
        "Theoretically EDGE is ratio-based and scale-invariant for "
        "multiplicative (split+dividend) adjustments. This report "
        "verifies empirically across known split events."
    )
    lines.append("")
    lines.append("| Ticker | Split | n bars | Median raw | Median adj | Median Δ (adj − raw) | % diff |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")

    for ticker, split_date, ratio in SPLIT_EVENTS:
        window = timedelta(days=int(args.window_days * 1.8))
        start = split_date - window
        end = split_date + window
        try:
            raw_bars = client.ticker_range(
                ticker, start.isoformat(), end.isoformat(), adjusted=False
            )
            adj_bars = client.ticker_range(
                ticker, start.isoformat(), end.isoformat(), adjusted=True
            )
        except Exception as exc:
            lines.append(f"| {ticker} | {ratio} | — | — | — | — | fetch failed: {exc} |")
            continue

        raw_df = _frame_from_bars(raw_bars)
        adj_df = _frame_from_bars(adj_bars)
        if raw_df.empty or adj_df.empty:
            lines.append(f"| {ticker} | {ratio} | — | — | — | — | empty frame |")
            continue

        raw_edge = edge_spread(
            raw_df["open"], raw_df["high"], raw_df["low"], raw_df["close"], window=21
        )
        adj_edge = edge_spread(
            adj_df["open"], adj_df["high"], adj_df["low"], adj_df["close"], window=21
        )

        joined = pd.DataFrame({"raw": raw_edge, "adj": adj_edge}).dropna()
        if joined.empty:
            lines.append(f"| {ticker} | {ratio} | 0 | — | — | — | no overlap after warmup |")
            continue
        joined["delta"] = joined["adj"] - joined["raw"]
        med_raw = joined["raw"].median()
        med_adj = joined["adj"].median()
        med_delta = joined["delta"].median()
        pct_diff = (med_adj - med_raw) / med_raw * 100 if med_raw > 0 else 0.0
        lines.append(
            f"| {ticker} | {ratio} | {len(joined)} | {med_raw * 10_000:.1f} bps | "
            f"{med_adj * 10_000:.1f} bps | {med_delta * 10_000:+.1f} bps | {pct_diff:+.1f}% |"
        )

    lines.append("")
    lines.append("## Decision gate")
    lines.append("")
    lines.append(
        "If all rows show |% diff| < 10% → adjusted prices are safe to use as "
        "production default (EDGE is effectively scale-invariant on this data). "
        "Larger deltas flag tickers where raw should be preferred or a "
        "ticker-specific calibration applied."
    )

    DOC_OUT.parent.mkdir(parents=True, exist_ok=True)
    DOC_OUT.write_text("\n".join(lines))
    print(f"Report written to {DOC_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
