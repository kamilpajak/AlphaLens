"""PIT-integrity replication probe — gate for v7 pre-reg lock.

Purpose:
  Verify that iVolatility's `/equities/stock-market-data` (smd) endpoint
  returns IVP values frozen-as-of (computed using only data up to asof_t),
  not retrospectively recomputed using current-time history. Without this
  guarantee, querying smd at historical dates leaks future information into
  cross-sectional backtests.

Method:
  1. Fetch raw daily IVX30 history for AAPL across two years (2022-2023).
  2. Pick N monthly asof dates in 2023.
  3. At each asof_t:
     - Empirical IVP = scipy percentileofscore of strict backward window
       [asof_t - 365d, asof_t) computed by us.
     - Vendor IVP = smd's ivp30 column at asof_t (single API call).
  4. Pearson correlation across N pairs. Gate >= 0.95 => PASS.

Single ticker (AAPL) is sufficient: we test temporal frozenness of vendor
analytics, not cross-sectional signal. Highly liquid mega-cap minimizes
data-quality noise.

Run:
    ALPHALENS_IVOL_API_KEY=... .venv/bin/python scripts/probe_pit_replication.py

Output:
    docs/research/pit_replication_probe_2026_05_01.json
    docs/research/pit_replication_probe_2026_05_01.md
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_JSON = REPO_ROOT / "docs" / "research" / "pit_replication_probe_2026_05_01.json"
OUTPUT_MD = REPO_ROOT / "docs" / "research" / "pit_replication_probe_2026_05_01.md"

DEFAULT_TICKER = "AAPL"
DEFAULT_LOOKBACK_DAYS = 365
DEFAULT_CORR_THRESHOLD = 0.95
DEFAULT_STRIDE_DAYS = 30
DEFAULT_TEST_START = date(2023, 1, 16)
DEFAULT_TEST_END = date(2023, 12, 15)
DELAY_BETWEEN_REQUESTS = 0.3

# Candidate IVP column names — iVolatility smd schema is documented loosely;
# probe tries each in order until a numeric column matches.
IVP_COLUMN_CANDIDATES = (
    "ivp30",
    "iv30Percentile",
    "iv30_percentile",
    "IVP30",
    "ivp_30",
)


# ============================================================================
# Pure functions (testable without live API)
# ============================================================================


def compute_empirical_iv_percentile(history: list[float], current: float) -> float:
    """Percentile of `current` within `history`, scipy 'mean' convention.

    Returns NaN if `current` is NaN or `history` empty after dropping NaNs.
    Returns 0.0 / 100.0 for current strictly below min / above max.
    """
    if math.isnan(current):
        return float("nan")
    clean = [v for v in history if not math.isnan(v)]
    if not clean:
        return float("nan")
    if current > max(clean):
        return 100.0
    if current < min(clean):
        return 0.0
    return float(stats.percentileofscore(clean, current, kind="mean"))


def pit_window_start(
    asof: pd.Timestamp, lookback_days: int = DEFAULT_LOOKBACK_DAYS
) -> pd.Timestamp:
    """Strict backward-looking window start: asof - lookback_days calendar days."""
    return asof - pd.Timedelta(days=lookback_days)


def pearson_correlation(pairs: list[tuple[float, float]]) -> float:
    """Pearson correlation of (x, y) pairs. NaN if too few clean pairs or
    zero variance in either side."""
    clean = [(x, y) for x, y in pairs if not (math.isnan(x) or math.isnan(y))]
    if len(clean) < 3:
        return float("nan")
    xs = np.array([p[0] for p in clean])
    ys = np.array([p[1] for p in clean])
    if xs.std() == 0 or ys.std() == 0:
        return float("nan")
    corr = np.corrcoef(xs, ys)[0, 1]
    return float(corr)


def evaluate_pit_gate(correlation: float, threshold: float = DEFAULT_CORR_THRESHOLD) -> dict:
    """Apply PIT integrity gate. NaN correlation => structural FAIL."""
    if math.isnan(correlation):
        verdict = "FAIL"
    else:
        verdict = "PASS" if correlation >= threshold else "FAIL"
    return {
        "correlation": correlation,
        "threshold": threshold,
        "verdict": verdict,
    }


def select_test_asofs(start: date, end: date, stride_days: int = DEFAULT_STRIDE_DAYS) -> list[date]:
    """Inclusive monthly stride. Returns dates `start, start+stride, ...`
    while <= end."""
    out = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur = cur + timedelta(days=stride_days)
    return out


def find_ivx30_at(history_df: pd.DataFrame, asof: pd.Timestamp) -> float:
    """Return IVX30 at exact asof, or last trading day on/before it."""
    if history_df.empty or "ivx30" not in history_df.columns:
        return float("nan")
    eligible = history_df[history_df["date"] <= asof]
    if eligible.empty:
        return float("nan")
    val = eligible.iloc[-1]["ivx30"]
    return float(val) if pd.notna(val) else float("nan")


def slice_backward_history(
    history_df: pd.DataFrame,
    asof: pd.Timestamp,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[float]:
    """Strict backward window [asof - lookback, asof). asof itself excluded
    so that empirical percentile uses only PRIOR observations."""
    if history_df.empty or "ivx30" not in history_df.columns:
        return []
    start = pit_window_start(asof, lookback_days)
    mask = (history_df["date"] >= start) & (history_df["date"] < asof)
    return [float(v) for v in history_df.loc[mask, "ivx30"].dropna().tolist()]


def extract_smd_ivp(smd_df: pd.DataFrame) -> float:
    """Pluck IVP30 column from smd response, trying known field names."""
    if smd_df is None or not isinstance(smd_df, pd.DataFrame) or smd_df.empty:
        return float("nan")
    for col in IVP_COLUMN_CANDIDATES:
        if col in smd_df.columns:
            non_null = smd_df[col].dropna()
            if len(non_null) > 0:
                return float(non_null.iloc[0])
    return float("nan")


# ============================================================================
# Live probe
# ============================================================================


def _build_query_fns(ivol_module):
    def make(endpoint: str):
        try:
            return ivol_module.setMethod(endpoint)
        except Exception as e:
            logger.warning("setMethod failed for %s: %s", endpoint, e)
            return None

    return {
        "ivx": make("/equities/eod/ivx"),
        "smd": make("/equities/stock-market-data"),
    }


def fetch_ivx_history(
    query_fn, ticker: str, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame:
    """Fetch raw daily IVX30 history. Returns DataFrame with columns
    [date, ivx30] sorted ascending."""
    if query_fn is None:
        raise RuntimeError("ivx wrapper unavailable")
    df = query_fn(
        symbol=ticker,
        from_=start.strftime("%Y-%m-%d"),
        to=end.strftime("%Y-%m-%d"),
    )
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame(columns=["date", "ivx30"])
    cols = {c.lower(): c for c in df.columns}
    date_col = cols.get("date")
    if date_col is None:
        return pd.DataFrame(columns=["date", "ivx30"])
    ivx_col = None
    for cand in ("ivx30", "iv30", "iv_30", "IVX30"):
        if cand.lower() in cols:
            ivx_col = cols[cand.lower()]
            break
    if ivx_col is None:
        ivx_label = next((c for c in df.columns if "30" in c.lower() and "iv" in c.lower()), None)
        if ivx_label is None:
            return pd.DataFrame(columns=["date", "ivx30"])
        ivx_col = ivx_label
    out = (
        pd.DataFrame(
            {
                "date": pd.to_datetime(df[date_col]),
                "ivx30": pd.to_numeric(df[ivx_col], errors="coerce"),
            }
        )
        .sort_values("date")
        .reset_index(drop=True)
    )
    return out


def fetch_smd_at(query_fn, ticker: str, asof: pd.Timestamp) -> pd.DataFrame:
    if query_fn is None:
        raise RuntimeError("smd wrapper unavailable")
    d = asof.strftime("%Y-%m-%d")
    df = query_fn(symbols=ticker, from_=d, to=d)
    return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def run_probe(
    ticker: str = DEFAULT_TICKER,
    test_start: date = DEFAULT_TEST_START,
    test_end: date = DEFAULT_TEST_END,
    stride_days: int = DEFAULT_STRIDE_DAYS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    threshold: float = DEFAULT_CORR_THRESHOLD,
) -> dict:
    api_key = os.environ.get("ALPHALENS_IVOL_API_KEY") or os.environ.get("IVOL_API_KEY")
    if not api_key:
        raise RuntimeError("ALPHALENS_IVOL_API_KEY env var not set")

    import ivolatility as ivol

    ivol.setLoginParams(apiKey=api_key)
    fns = _build_query_fns(ivol)

    asofs = select_test_asofs(test_start, test_end, stride_days)
    if not asofs:
        raise RuntimeError("no asofs selected — check date range / stride")

    history_start = pd.Timestamp(asofs[0]) - pd.Timedelta(days=lookback_days + 30)
    history_end = pd.Timestamp(asofs[-1]) + pd.Timedelta(days=5)

    logger.info(
        "Fetching IVX30 history for %s from %s to %s",
        ticker,
        history_start.date(),
        history_end.date(),
    )
    history = fetch_ivx_history(fns["ivx"], ticker, history_start, history_end)
    logger.info("History rows: %d", len(history))
    if len(history) < lookback_days // 2:
        logger.warning("History sparse — may cause low correlation")

    pairs = []
    per_asof_records = []
    for asof_d in asofs:
        asof = pd.Timestamp(asof_d)
        time.sleep(DELAY_BETWEEN_REQUESTS)

        backward_history = slice_backward_history(history, asof, lookback_days)
        current_ivx = find_ivx30_at(history, asof)
        empirical_ivp = compute_empirical_iv_percentile(backward_history, current_ivx)

        try:
            smd_df = fetch_smd_at(fns["smd"], ticker, asof)
            vendor_ivp = extract_smd_ivp(smd_df)
        except Exception as e:
            logger.warning("smd fetch failed for %s: %s", asof.date(), e)
            vendor_ivp = float("nan")

        pairs.append((empirical_ivp, vendor_ivp))
        per_asof_records.append(
            {
                "asof": asof.strftime("%Y-%m-%d"),
                "history_window_size": len(backward_history),
                "current_ivx30": current_ivx,
                "empirical_ivp": empirical_ivp,
                "vendor_ivp": vendor_ivp,
            }
        )
        logger.info(
            "asof=%s  ivx30=%.2f  emp_ivp=%.2f  smd_ivp=%.2f  window=%d",
            asof.date(),
            current_ivx if not math.isnan(current_ivx) else float("nan"),
            empirical_ivp if not math.isnan(empirical_ivp) else float("nan"),
            vendor_ivp if not math.isnan(vendor_ivp) else float("nan"),
            len(backward_history),
        )

    correlation = pearson_correlation(pairs)
    gate = evaluate_pit_gate(correlation, threshold)

    return {
        "ticker": ticker,
        "test_start": test_start.isoformat(),
        "test_end": test_end.isoformat(),
        "stride_days": stride_days,
        "lookback_days": lookback_days,
        "asof_count": len(asofs),
        "valid_pairs": len([p for p in pairs if not (math.isnan(p[0]) or math.isnan(p[1]))]),
        "per_asof": per_asof_records,
        "gate": gate,
    }


def write_outputs(result: dict) -> None:
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(result, indent=2, default=str))
    gate = result["gate"]
    md_lines = [
        "# PIT replication probe — iVolatility smd",
        "",
        f"**Ticker:** {result['ticker']}",
        f"**Test asofs:** {result['test_start']} -> {result['test_end']} stride {result['stride_days']}d",
        f"**Lookback window:** {result['lookback_days']} calendar days",
        f"**Asofs tested:** {result['asof_count']}, valid pairs: {result['valid_pairs']}",
        "",
        "## Gate",
        "",
        f"- Pearson correlation: **{gate['correlation']:.4f}**"
        if not math.isnan(gate["correlation"])
        else "- Pearson correlation: **NaN**",
        f"- Threshold: {gate['threshold']:.2f}",
        f"- **Verdict: {gate['verdict']}**",
        "",
        "## Per-asof",
        "",
        "| asof | window | ivx30 | empirical IVP | vendor IVP |",
        "|---|---|---|---|---|",
    ]
    for rec in result["per_asof"]:
        emp = rec["empirical_ivp"]
        vend = rec["vendor_ivp"]
        ivx = rec["current_ivx30"]
        md_lines.append(
            f"| {rec['asof']} | {rec['history_window_size']} | "
            f"{'NaN' if math.isnan(ivx) else f'{ivx:.2f}'} | "
            f"{'NaN' if math.isnan(emp) else f'{emp:.2f}'} | "
            f"{'NaN' if math.isnan(vend) else f'{vend:.2f}'} |"
        )
    OUTPUT_MD.write_text("\n".join(md_lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default=DEFAULT_TICKER)
    parser.add_argument("--start", default=DEFAULT_TEST_START.isoformat())
    parser.add_argument("--end", default=DEFAULT_TEST_END.isoformat())
    parser.add_argument("--stride-days", type=int, default=DEFAULT_STRIDE_DAYS)
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--threshold", type=float, default=DEFAULT_CORR_THRESHOLD)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    result = run_probe(
        ticker=args.ticker,
        test_start=date.fromisoformat(args.start),
        test_end=date.fromisoformat(args.end),
        stride_days=args.stride_days,
        lookback_days=args.lookback_days,
        threshold=args.threshold,
    )
    write_outputs(result)
    print(json.dumps(result["gate"], indent=2))
    return 0 if result["gate"]["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
