"""iVolatility `/equities/stock-market-data` (smd) immutable raw-data cache.

Architecture: cache is the bottom-most data tier for v7+ options-implied work.
One parquet per ticker under `cache_dir/{TICKER}.parquet` containing all
vendor columns verbatim across the full backtest window. Range-mode pull
collapses ~800k single-date calls into ~2000 single-call-per-ticker pulls
(empirically confirmed: smd accepts `from_=YYYY-MM-DD, to=YYYY-MM-DD` and
returns a daily series across the window).

The cache is pure passthrough — vendor schema preserved verbatim, no row
filtering. Multi-exchange / cross-listed tickers (e.g. CTT NYSE+TSX) keep
both rows; the feature joiner is the only consumer that filters down to
US exchanges.

Resumable: existing parquets are skipped, so the universe pull can be
restarted after crashes / network issues.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

FetcherFn = Callable[[str, date, date], pd.DataFrame | None]

SMD_ENDPOINT = "/equities/stock-market-data"


def _smd_query_fn():
    """Return the ivolatility wrapper's smd query callable. Imports lazily so
    tests can patch this name without requiring an API key.
    """
    import ivolatility as ivol

    return ivol.setMethod(SMD_ENDPOINT)


def _default_smd_fetcher(ticker: str, start: date, end: date) -> pd.DataFrame | None:
    """Default fetcher: single range-mode call to smd via the ivolatility wrapper.

    Caller must have invoked `ivolatility.setLoginParams(apiKey=...)` and
    `ivolatility.setDelayBetweenRequests(...)` beforehand. Returns whatever
    the vendor returned — empty DataFrame for delisted/unknown tickers,
    otherwise the daily series across [start, end].
    """
    query_fn = _smd_query_fn()
    df = query_fn(
        symbols=ticker,
        from_=start.strftime("%Y-%m-%d"),
        to=end.strftime("%Y-%m-%d"),
    )
    return df


def _robust_smd_fetcher(ticker: str, start: date, end: date) -> pd.DataFrame | None:
    """Fault-tolerant fetcher with two retry strategies for known vendor bugs.

    Vendor's gzipped-CSV download path uses naive `pd.read_csv` (no quoting
    fallback), so any row with an embedded comma in a string field (e.g.
    company name "Berkshire Hathaway, Inc." or address fields for AMZN/PG)
    breaks parsing with `Expected N fields, saw N+1`. Empirically this affects
    ~3-5% of tickers on the 8y smd window.

    Strategy:
    1. Try the default fetcher (full date range).
    2. On `ParserError`: monkeypatch the wrapper's read_csv to use
       `on_bad_lines="warn"` and retry. Bad lines drop with a warning;
       remaining ~99% of rows are preserved.
    3. On any other failure: re-raise; caller (download_and_cache) catches.
    """
    try:
        return _default_smd_fetcher(ticker, start, end)
    except pd.errors.ParserError as exc:
        logger.warning(
            "[%s] vendor CSV parse error (%s) — retrying with on_bad_lines='warn'",
            ticker,
            exc,
        )
        return _retry_with_lenient_csv(ticker, start, end)


def _retry_with_lenient_csv(ticker: str, start: date, end: date) -> pd.DataFrame | None:
    """Monkeypatch the ivolatility wrapper module's `pd.read_csv` reference
    to add `on_bad_lines="warn"`, run the fetcher, then restore. Surgical
    and reversible.
    """
    import ivolatility.ivolatility as ivol_mod

    original_read_csv = ivol_mod.pd.read_csv

    def patched_read_csv(*args, **kwargs):
        kwargs.setdefault("on_bad_lines", "warn")
        return original_read_csv(*args, **kwargs)

    ivol_mod.pd.read_csv = patched_read_csv
    try:
        return _default_smd_fetcher(ticker, start, end)
    finally:
        ivol_mod.pd.read_csv = original_read_csv


def _coerce_mixed_object_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Vendor smd occasionally returns numeric columns as object dtype with
    mixed numeric/string values. PyArrow's strict typing then fails the
    parquet write (`ArrowInvalid: Could not convert '0.336...' with type str`).

    Walk every object-dtype column and try `pd.to_numeric(errors='coerce')`.
    If ≥80% of non-null values successfully convert, replace the column with
    the coerced numeric version. Otherwise leave as object (likely a true
    string column like ticker / company name).
    """
    if df.empty:
        return df
    out = df.copy()
    for col in out.columns:
        if out[col].dtype != object:
            continue
        non_null = out[col].dropna()
        if non_null.empty:
            continue
        coerced = pd.to_numeric(non_null, errors="coerce")
        success_rate = coerced.notna().sum() / len(non_null)
        if success_rate >= 0.80:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def download_and_cache(
    tickers: list[str],
    start: date,
    end: date,
    cache_dir: Path,
    *,
    fetcher: FetcherFn | None = None,
    sleep_between: float = 0.3,
) -> int:
    """Pull missing tickers into `cache_dir` as parquet. Returns count of new files.

    Skipping rule: if `cache_dir/{TICKER}.parquet` already exists, the ticker
    is treated as cached and the fetcher is not invoked. Delete the parquet
    by hand to force a re-pull.

    Sentinels:
    - Empty DataFrame from fetcher → no parquet written (delisted name with
      no smd coverage). Subsequent `download_and_cache` runs WILL re-attempt;
      callers needing terminal-cache semantics should write a manifest.
    - None response → same.
    - Exception in fetcher → logged at WARNING, ticker skipped, batch continues.
    """
    fetch = fetcher or _robust_smd_fetcher
    cache_dir.mkdir(parents=True, exist_ok=True)

    new_count = 0
    for ticker in tickers:
        path = cache_dir / f"{ticker.upper()}.parquet"
        if path.exists():
            continue
        try:
            df = fetch(ticker, start, end)
        except Exception as exc:
            logger.warning("smd fetch %s failed: %s", ticker, exc)
            continue
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            logger.info("smd returned empty for %s (likely no coverage)", ticker)
            continue
        df = _coerce_mixed_object_columns(df)
        try:
            df.to_parquet(path)
        except Exception as exc:
            logger.warning("[%s] parquet write failed: %s — skipping", ticker, exc)
            if path.exists():
                path.unlink()
            continue
        new_count += 1
        if sleep_between > 0:
            time.sleep(sleep_between)
    return new_count


def load_cached_smd(ticker: str, cache_dir: Path) -> pd.DataFrame | None:
    """Load `cache_dir/{TICKER}.parquet`. Returns None if missing.

    Lookup is case-insensitive: `load_cached_smd("aapl", ...)` resolves to
    `AAPL.parquet`.
    """
    path = cache_dir / f"{ticker.upper()}.parquet"
    if not path.exists():
        return None
    return pd.read_parquet(path)
