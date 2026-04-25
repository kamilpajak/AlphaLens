"""8-K event screener (go/no-go pre-backtest gate) — Layer 2f candidate.

Per Perplexity 2026-04-24 4th-attempt recommendation: before committing engineering
effort to an event-driven strategy, empirically verify that 8-K filings by Item type
produce Cumulative Abnormal Returns (CAR) above kill threshold (50 bps) and ideally
above proceed threshold (80 bps) at +20d window.

Contract:
  Input:  ticker→CIK pairs, SEC client (our SecEdgarClient), price loader, window.
  Output: DataFrame {item, window_days, mean_car_bps, std_car_bps, n, verdict}.

Nothing in this module touches LLM classification; that's a Phase 2 add-on if raw
Item-level aggregation shows signal worth sub-categorising.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

KILL_THRESHOLD_BPS = 50.0
PROCEED_THRESHOLD_BPS = 80.0
DEFAULT_WINDOWS = (1, 5, 20, 60)
TSTAT_MIN_FOR_PROCEED = 2.0  # signal consistency gate per Perplexity std-dev criterion
MAX_STD_BPS_FOR_PROCEED = 400.0  # Perplexity: std < 4% (we relax to 400 bps for retail noise floor)
WINSORIZE_BOUNDS = (0.05, 0.95)


@dataclass(frozen=True)
class EightKFiling:
    cik: str
    ticker: str
    filing_date: pd.Timestamp
    accession: str
    items: tuple[str, ...]


def parse_items_string(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def extract_8k_filings(
    *,
    submissions: dict,
    cik: str,
    ticker: str,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> list[EightKFiling]:
    recent = (submissions or {}).get("filings", {}).get("recent", {}) or {}
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    items_col = recent.get("items", [])

    n = len(forms)
    if not (len(dates) == len(accessions) == n):
        return []

    out: list[EightKFiling] = []
    for i in range(n):
        if forms[i] != "8-K":
            continue
        try:
            filing_date = pd.Timestamp(dates[i])
        except Exception:
            continue
        if start is not None and filing_date < start:
            continue
        if end is not None and filing_date > end:
            continue
        items_raw = items_col[i] if i < len(items_col) else ""
        parsed = tuple(parse_items_string(items_raw))
        out.append(
            EightKFiling(
                cik=cik,
                ticker=ticker,
                filing_date=filing_date,
                accession=accessions[i],
                items=parsed,
            )
        )
    return out


def compute_abnormal_return(
    ticker_close: pd.Series,
    benchmark_close: pd.Series,
    *,
    event_date: pd.Timestamp,
    window_days: int,
) -> float:
    """CAR = ticker trailing return − benchmark trailing return over `window_days`
    bars, entering at the NEXT trading day's close (standard 1-day implementation lag).

    Returns NaN if insufficient history after the event.
    """
    # Find entry bar: first trading day in ticker series strictly AFTER event_date
    t_idx = ticker_close.index
    future = t_idx[t_idx > event_date]
    if len(future) < window_days + 1:
        return float("nan")
    entry_date = future[0]
    exit_date = future[window_days]

    try:
        t_entry = float(ticker_close.loc[entry_date])
        t_exit = float(ticker_close.loc[exit_date])
    except KeyError:
        return float("nan")
    if t_entry == 0:
        return float("nan")
    ticker_ret = t_exit / t_entry - 1.0

    b_idx = benchmark_close.index
    b_future = b_idx[b_idx >= entry_date]
    if len(b_future) < window_days + 1:
        return float("nan")
    b_entry_date = b_future[0]
    b_exit_date = b_future[window_days] if len(b_future) > window_days else None
    if b_exit_date is None:
        return float("nan")
    b_entry = float(benchmark_close.loc[b_entry_date])
    b_exit = float(benchmark_close.loc[b_exit_date])
    if b_entry == 0:
        return float("nan")
    bench_ret = b_exit / b_entry - 1.0

    return ticker_ret - bench_ret


def aggregate_car_by_item(records: pd.DataFrame) -> pd.DataFrame:
    """Group (item, window_days) → robust stats + verdict column.

    Input columns: ``item``, ``window_days``, ``car`` (fraction).
    Output columns: ``item``, ``window_days``, ``n``, ``mean_car_bps``, ``std_car_bps``,
    ``sem_bps``, ``tstat``, ``median_car_bps``, ``winsorized_mean_bps``, ``verdict``.

    Verdict promotes PROCEED only when ALL hold:
      - winsorized_mean >= PROCEED_THRESHOLD_BPS (mean robust to outliers)
      - |t-stat| >= TSTAT_MIN_FOR_PROCEED (signal consistency)
      - std <= MAX_STD_BPS_FOR_PROCEED (bounded variance)
    KILL when winsorized_mean <= KILL_THRESHOLD_BPS.
    """
    clean = records.dropna(subset=["car"]).copy()
    if clean.empty:
        return pd.DataFrame(
            columns=[
                "item",
                "window_days",
                "n",
                "mean_car_bps",
                "std_car_bps",
                "sem_bps",
                "tstat",
                "median_car_bps",
                "winsorized_mean_bps",
                "verdict",
            ]
        )

    def _winsorized_mean(s: pd.Series) -> float:
        if len(s) < 3:
            return float(s.mean())
        lo = s.quantile(WINSORIZE_BOUNDS[0])
        hi = s.quantile(WINSORIZE_BOUNDS[1])
        return float(s.clip(lo, hi).mean())

    def _group_stats(g: pd.DataFrame) -> pd.Series:
        car = g["car"]
        n = len(car)
        mean = float(car.mean())
        std = float(car.std(ddof=1)) if n > 1 else 0.0
        sem = std / np.sqrt(max(n, 1))
        tstat = mean / sem if sem > 0 else 0.0
        return pd.Series(
            {
                "n": n,
                "mean_car_bps": mean * 10_000.0,
                "std_car_bps": std * 10_000.0,
                "sem_bps": sem * 10_000.0,
                "tstat": tstat,
                "median_car_bps": float(car.median()) * 10_000.0,
                "winsorized_mean_bps": _winsorized_mean(car) * 10_000.0,
            }
        )

    grouped = (
        clean.groupby(["item", "window_days"])
        .apply(_group_stats, include_groups=False)
        .reset_index()
    )

    def _verdict(row) -> str:
        wm = row["winsorized_mean_bps"]
        t = abs(row["tstat"])
        s = row["std_car_bps"]
        if wm <= KILL_THRESHOLD_BPS:
            return "KILL"
        if (
            wm >= PROCEED_THRESHOLD_BPS
            and t >= TSTAT_MIN_FOR_PROCEED
            and s <= MAX_STD_BPS_FOR_PROCEED
        ):
            return "PROCEED"
        return "GRAY"

    grouped["verdict"] = grouped.apply(_verdict, axis=1)
    return grouped[
        [
            "item",
            "window_days",
            "n",
            "mean_car_bps",
            "std_car_bps",
            "sem_bps",
            "tstat",
            "median_car_bps",
            "winsorized_mean_bps",
            "verdict",
        ]
    ]


def run_screen(
    *,
    ticker_cik_pairs: Sequence[tuple[str, str]],
    sec_client,
    price_loader: Callable[[str], pd.Series],
    benchmark: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
) -> dict:
    """Orchestrate the screen: fetch 8-Ks, compute CAR per window, aggregate.

    `price_loader` is caller-provided so tests can inject synthetic data without
    hitting yfinance. Returns dict with 'summary' (DataFrame) + 'records' (raw).
    """
    records: list[dict] = []
    bench_close = price_loader(benchmark)

    for ticker, cik in ticker_cik_pairs:
        try:
            subs = sec_client.fetch_submissions(cik)
        except Exception:
            continue
        filings = extract_8k_filings(submissions=subs, cik=cik, ticker=ticker, start=start, end=end)
        if not filings:
            continue
        try:
            ticker_close = price_loader(ticker)
        except Exception:
            continue
        if ticker_close is None or ticker_close.empty:
            continue

        for f in filings:
            for item in f.items:
                for w in windows:
                    car = compute_abnormal_return(
                        ticker_close,
                        bench_close,
                        event_date=f.filing_date,
                        window_days=w,
                    )
                    records.append(
                        {
                            "ticker": ticker,
                            "cik": cik,
                            "filing_date": f.filing_date,
                            "accession": f.accession,
                            "item": item,
                            "window_days": w,
                            "car": car,
                        }
                    )

    records_df = pd.DataFrame(records)
    summary = aggregate_car_by_item(records_df)
    return {"records": records_df, "summary": summary}
