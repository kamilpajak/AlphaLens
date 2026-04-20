"""Validate EDGE spread estimator against real bid-ask from tick data.

Reads `~/.alphalens/tick_samples/` (populated by pull_tick_sample.py),
computes a proxy for the "true" effective spread from tick-to-tick price
variation, and compares against EDGE / AR / CS rolling estimates on the
same ticker's daily OHLC (from Lean CSV).

Output: `docs/backtest/edge_validation.md` with per-ticker bias,
correlation by size bucket, and outlier list.

Usage:
    .venv/bin/python scripts/validate_edge_vs_ticks.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens.backtest.history_store import HistoryStore  # noqa: E402
from alphalens.backtest.spread_estimator import (  # noqa: E402
    abdi_ranaldo_spread,
    corwin_schultz_spread,
    edge_spread,
)
from alphalens.screeners.lean.config import DATA_DIR  # noqa: E402
from alphalens.screeners.lean.lean_csv_loader import load_lean_histories  # noqa: E402
from alphalens.screeners.themed.config import UNIVERSE_PATH  # noqa: E402
from alphalens.screeners.themed.universe import flatten_universe  # noqa: E402
from alphalens.tick_data import TickStore  # noqa: E402

import yaml  # noqa: E402


TICK_CACHE = Path.home() / ".alphalens" / "tick_samples"
DOC_OUT = Path(__file__).resolve().parent.parent / "docs" / "backtest" / "edge_validation.md"


def realized_spread_from_ticks(ticks: pd.DataFrame) -> float | None:
    """Proxy for effective spread: 2 × std of log-return between consecutive
    trade prints during regular-hours (9:30-16:00 ET). Captures bid-ask bounce.

    Returns decimal (0.01 = 1%). None if fewer than 50 prints.
    """
    if ticks.empty or len(ticks) < 50:
        return None
    # Convert ns timestamp to UTC datetime then filter to regular session.
    ts = pd.to_datetime(ticks["sip_timestamp_ns"], unit="ns", utc=True)
    rth = ticks[(ts.dt.hour * 60 + ts.dt.minute >= 13 * 60 + 30)  # 13:30 UTC = 9:30 ET (approx)
                & (ts.dt.hour * 60 + ts.dt.minute <= 20 * 60)]  # 20:00 UTC = 16:00 ET
    if len(rth) < 50:
        rth = ticks
    prices = rth["price"].to_numpy(dtype=float)
    prices = prices[prices > 0]
    if len(prices) < 50:
        return None
    log_r = np.diff(np.log(prices))
    # Bid-ask bounce inflates adjacent-tick variance; 2×std(log-r) ≈ spread.
    return float(2.0 * np.std(log_r))


def size_bucket(adv_dollar: float) -> str:
    if adv_dollar >= 5_000_000_000:
        return "mega-cap"
    if adv_dollar >= 500_000_000:
        return "large-cap"
    if adv_dollar >= 50_000_000:
        return "mid-cap"
    return "small-cap"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-rows", type=int, default=50)
    args = parser.parse_args(argv)
    _ = args

    if not TICK_CACHE.exists():
        print(f"ERROR: tick cache not found at {TICK_CACHE}", file=sys.stderr)
        print("Run scripts/pull_tick_sample.py first.", file=sys.stderr)
        return 2

    universe = yaml.safe_load(UNIVERSE_PATH.read_text())
    tickers = sorted(flatten_universe(universe).keys())

    print(f"Loading OHLCV histories for {len(tickers)} tickers…")
    histories = load_lean_histories(DATA_DIR, tickers)
    store = HistoryStore(histories)

    tick_store = TickStore(TICK_CACHE)

    rows = []
    for ticker in tickers:
        cache_dir = TICK_CACHE / ticker
        if not cache_dir.exists():
            continue
        day_files = sorted(cache_dir.glob("*.parquet"))
        if not day_files:
            continue
        try:
            ohlc = store.full(ticker)
        except KeyError:
            continue
        spread_est = edge_spread(ohlc["open"], ohlc["high"], ohlc["low"], ohlc["close"], window=21)
        ar_est = abdi_ranaldo_spread(ohlc["high"], ohlc["low"], ohlc["close"], window=21)
        cs_est = corwin_schultz_spread(ohlc["high"], ohlc["low"], window=21)
        dollar_volume = (ohlc["close"] * ohlc["volume"]).rolling(20).mean()

        for path in day_files:
            trade_date = date.fromisoformat(path.stem)
            ticks = tick_store.get_trades(ticker, trade_date)
            real = realized_spread_from_ticks(ticks)
            if real is None:
                continue
            ts = pd.Timestamp(trade_date)
            edge_val = spread_est.loc[:ts].iloc[-1] if not spread_est.loc[:ts].empty else None
            ar_val = ar_est.loc[:ts].iloc[-1] if not ar_est.loc[:ts].empty else None
            cs_val = cs_est.loc[:ts].iloc[-1] if not cs_est.loc[:ts].empty else None
            adv = dollar_volume.loc[:ts].iloc[-1] if not dollar_volume.loc[:ts].empty else None
            rows.append(
                {
                    "ticker": ticker,
                    "date": trade_date.isoformat(),
                    "real_spread": real,
                    "edge": float(edge_val) if edge_val is not None and not pd.isna(edge_val) else None,
                    "ar": float(ar_val) if ar_val is not None and not pd.isna(ar_val) else None,
                    "cs": float(cs_val) if cs_val is not None and not pd.isna(cs_val) else None,
                    "adv_dollar": float(adv) if adv is not None and not pd.isna(adv) else None,
                }
            )

    if not rows:
        print("No matching ticker-days between tick cache and Lean CSV history.")
        return 1

    df = pd.DataFrame(rows).dropna(subset=["real_spread", "edge", "adv_dollar"])
    df["size_bucket"] = df["adv_dollar"].apply(size_bucket)
    df["edge_bias"] = (df["edge"] - df["real_spread"]) / df["real_spread"]
    df["ar_bias"] = (df["ar"] - df["real_spread"]) / df["real_spread"]
    df["cs_bias"] = (df["cs"] - df["real_spread"]) / df["real_spread"]

    DOC_OUT.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append("# EDGE validation vs. Polygon ticks")
    lines.append("")
    lines.append(f"- Ticker-days evaluated: **{len(df)}** across **{df['ticker'].nunique()}** tickers")
    lines.append(f"- Date range: {df['date'].min()} .. {df['date'].max()}")
    lines.append("")

    lines.append("## Per-size-bucket bias")
    lines.append("")
    lines.append("| Bucket | n | Mean real spread (bps) | Mean EDGE bias | Mean AR bias | Mean CS bias |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for bucket in ["mega-cap", "large-cap", "mid-cap", "small-cap"]:
        sub = df[df["size_bucket"] == bucket]
        if sub.empty:
            continue
        lines.append(
            f"| {bucket} | {len(sub)} | "
            f"{sub['real_spread'].mean() * 10_000:.1f} | "
            f"{sub['edge_bias'].mean() * 100:+.1f}% | "
            f"{sub['ar_bias'].mean() * 100:+.1f}% | "
            f"{sub['cs_bias'].mean() * 100:+.1f}% |"
        )
    lines.append("")

    lines.append("## Per-ticker correlation (EDGE vs. real)")
    lines.append("")
    corr_rows = []
    for ticker, group in df.groupby("ticker"):
        if len(group) < 3:
            continue
        corr_rows.append(
            {"ticker": ticker, "n": len(group),
             "corr": group[["edge", "real_spread"]].corr().iloc[0, 1]}
        )
    corr_df = pd.DataFrame(corr_rows)
    if not corr_df.empty:
        lines.append(f"- Cross-section mean correlation: **{corr_df['corr'].mean():+.3f}**")
        lines.append(f"- Median: {corr_df['corr'].median():+.3f}")
        lines.append(f"- Tickers with corr > 0.5: {(corr_df['corr'] > 0.5).sum()}/{len(corr_df)}")
        lines.append("")

    lines.append("## Outliers (|EDGE bias| > 50%)")
    lines.append("")
    out = df[df["edge_bias"].abs() > 0.5].sort_values("edge_bias", key=lambda x: x.abs(), ascending=False)
    if out.empty:
        lines.append("None.")
    else:
        lines.append("| Ticker | Date | Real (bps) | EDGE (bps) | Bias |")
        lines.append("| --- | --- | ---: | ---: | ---: |")
        for _, row in out.head(20).iterrows():
            lines.append(
                f"| {row['ticker']} | {row['date']} | "
                f"{row['real_spread'] * 10_000:.0f} | "
                f"{row['edge'] * 10_000:.0f} | "
                f"{row['edge_bias'] * 100:+.0f}% |"
            )
    lines.append("")

    lines.append("## Decision gate")
    lines.append("")
    overall_bias = df["edge_bias"].mean()
    lines.append(f"- Overall EDGE mean bias: **{overall_bias * 100:+.1f}%**")
    if abs(overall_bias) < 0.15:
        lines.append(f"- **PASS** (|bias| < 15%): proceed with EDGE as primary estimator.")
    else:
        lines.append(
            f"- **INVESTIGATE** (|bias| ≥ 15%): consider per-size-bucket calibration "
            f"factor or fallback to AR for small-caps."
        )
    lines.append("")

    DOC_OUT.write_text("\n".join(lines))
    print(f"Report written to {DOC_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
