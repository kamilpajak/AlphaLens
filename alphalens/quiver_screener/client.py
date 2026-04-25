"""Thin wrapper over `quiverquant` SDK: fetch + normalize + disk cache.

Normalization contract (what features.py expects):
    congress: ticker, date, representative, transaction, amount_mid
    insiders: ticker, date, name, transaction, shares, price, value

Raw Quiver column names vary across API versions. This module tries the most
common aliases and raises a clear error if the schema has drifted — so we fix
the mapping once rather than silently miscomputing features.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "quiver"


_CONGRESS_COL_ALIASES = {
    "ticker": ["Ticker", "Stock", "stock"],
    "date": ["TransactionDate", "Traded", "transaction_date"],
    "representative": ["Representative", "Member", "Name", "name"],
    "transaction": ["Transaction", "Type", "transaction"],
    "amount": ["Amount", "Range", "Trade_Size_USD", "amount"],
}

_INSIDER_COL_ALIASES = {
    "ticker": ["Ticker", "Stock", "stock", "Symbol"],
    "date": ["Date", "TransactionDate", "transaction_date"],
    "name": ["Name", "Insider", "InsiderName", "name"],
    "transaction": [
        "AcquistionOrDisposition",
        "AcquiredDisposed",
        "Transaction",
        "Code",
        "transaction",
    ],
    "shares": ["Shares", "SharesTraded", "shares"],
    "price": ["PricePerShare", "Price", "price"],
}


def _resolve_column(df: pd.DataFrame, targets: list[str]) -> str:
    for t in targets:
        if t in df.columns:
            return t
    raise KeyError(f"None of {targets!r} found in columns {list(df.columns)!r}")


# Regex for disclosed-range strings like "$1,001 - $15,000" or "$50,001-$100,000".
_RANGE_RE = re.compile(r"\$?([\d,]+)\s*-\s*\$?([\d,]+)")


def _parse_amount(val) -> float:
    """Convert Quiver-disclosed range string to midpoint $ float."""
    if pd.isna(val):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    m = _RANGE_RE.search(s)
    if m:
        lo = float(m.group(1).replace(",", ""))
        hi = float(m.group(2).replace(",", ""))
        return (lo + hi) / 2.0
    try:
        return float(s.replace("$", "").replace(",", ""))
    except ValueError:
        return 0.0


def normalize_congress(raw: pd.DataFrame) -> pd.DataFrame:
    """Quiver raw congress_trading DataFrame → normalized schema."""
    if raw.empty:
        return pd.DataFrame(
            columns=["ticker", "date", "representative", "transaction", "amount_mid"]
        )

    cols = {k: _resolve_column(raw, v) for k, v in _CONGRESS_COL_ALIASES.items()}
    out = pd.DataFrame(
        {
            "ticker": raw[cols["ticker"]].astype(str).str.upper(),
            "date": pd.to_datetime(raw[cols["date"]]),
            "representative": raw[cols["representative"]].astype(str),
            "transaction": raw[cols["transaction"]].astype(str).str.upper(),
            "amount_mid": raw[cols["amount"]].map(_parse_amount),
        }
    )
    # Canonicalise transaction codes we've seen: "Purchase", "Sale (Partial)", etc.
    out["transaction"] = out["transaction"].where(
        out["transaction"].isin(["PURCHASE", "SALE", "EXCHANGE"]),
        out["transaction"].apply(
            lambda s: "PURCHASE" if "PURCHASE" in s else ("SALE" if "SALE" in s else "EXCHANGE")
        ),
    )
    return out


def normalize_insiders(raw: pd.DataFrame) -> pd.DataFrame:
    """Quiver raw insiders DataFrame → normalized schema.

    Transaction code normalised to 'A' (acquired/buy) or 'D' (disposed/sell).
    """
    if raw.empty:
        return pd.DataFrame(
            columns=[
                "ticker",
                "date",
                "name",
                "transaction",
                "shares",
                "price",
                "value",
            ]
        )

    cols = {k: _resolve_column(raw, v) for k, v in _INSIDER_COL_ALIASES.items()}
    raw_trans = raw[cols["transaction"]].astype(str).str.upper()
    # Match any known buy indicator explicitly. Unknown codes default to 'D' (sell)
    # to stay conservative — misclassifying a buy as sell biases signal to zero,
    # which is safer than misclassifying a sell as buy.
    is_buy = raw_trans.str.contains(r"^(?:P|A|PURCHASE|ACQUIRED|BUY)", regex=True, na=False)
    out = pd.DataFrame(
        {
            "ticker": raw[cols["ticker"]].astype(str).str.upper(),
            "date": pd.to_datetime(raw[cols["date"]]),
            "name": raw[cols["name"]].astype(str),
            "transaction": np.where(is_buy, "A", "D"),
            "shares": pd.to_numeric(raw[cols["shares"]], errors="coerce").fillna(0).astype(int),
            "price": pd.to_numeric(raw[cols["price"]], errors="coerce").fillna(0.0),
        }
    )
    out["value"] = out["shares"] * out["price"]
    return out


def fetch_congress_for_tickers(
    quiver_client,
    tickers: list[str],
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
) -> pd.DataFrame:
    """Fetch + normalize + cache per-ticker congress_trading.  Concatenates all results."""
    cache_dir = Path(cache_dir) / "congress"
    cache_dir.mkdir(parents=True, exist_ok=True)
    frames: list[pd.DataFrame] = []
    for t in tickers:
        cache_file = cache_dir / f"{t.upper()}.pkl"
        if cache_file.exists() and not refresh:
            frames.append(pd.read_pickle(cache_file))
            continue
        try:
            raw = quiver_client.congress_trading(t, recent=False)
        except Exception as exc:
            print(f"  {t}: congress_trading failed ({exc}); skipping (no cache write)")
            continue
        if not isinstance(raw, pd.DataFrame):
            raw = pd.DataFrame()
        norm = normalize_congress(raw)
        norm.to_pickle(cache_file)
        frames.append(norm)
    if not frames:
        return pd.DataFrame(
            columns=["ticker", "date", "representative", "transaction", "amount_mid"]
        )
    return pd.concat(frames, ignore_index=True)


def fetch_insiders_for_tickers(
    quiver_client,
    tickers: list[str],
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
) -> pd.DataFrame:
    cache_dir = Path(cache_dir) / "insiders"
    cache_dir.mkdir(parents=True, exist_ok=True)
    frames: list[pd.DataFrame] = []
    for t in tickers:
        cache_file = cache_dir / f"{t.upper()}.pkl"
        if cache_file.exists() and not refresh:
            frames.append(pd.read_pickle(cache_file))
            continue
        try:
            raw = quiver_client.insiders(t)
        except Exception as exc:
            print(f"  {t}: insiders failed ({exc}); skipping (no cache write)")
            continue
        if not isinstance(raw, pd.DataFrame):
            raw = pd.DataFrame()
        norm = normalize_insiders(raw)
        norm.to_pickle(cache_file)
        frames.append(norm)
    if not frames:
        return pd.DataFrame(
            columns=[
                "ticker",
                "date",
                "name",
                "transaction",
                "shares",
                "price",
                "value",
            ]
        )
    return pd.concat(frames, ignore_index=True)
