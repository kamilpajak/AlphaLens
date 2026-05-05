"""Three universe-loader variants for retrospective replication audit.

Per ``params_v9d_retrospective_pre_2018_2026_05_05.json`` pre-reg:

- ``U1`` ``pit_union_legacy`` — existing yaml snapshots filtered by current
  IWM seed; worst-case max-survivor-bias baseline.
- ``U2`` ``pit_union_from_ivol_cache`` — every ticker in the iVol cache with
  valid ivp30 data on the asof date; cleaner because the cache contains
  delisted firms after the 2026-05-05 Polygon backfill.
- ``U3`` ``pit_union_nber_rebuild`` — U2 with the NBER/Russell cap-band filter
  (300 M to 3 B USD) applied via SEC XBRL shares × iVol close.

The inventory parquet at
``~/.alphalens/ivolatility_smd_inventory.parquet`` is the fast index used by
U2/U3 for asof eligibility (``first_date <= asof <= last_date``). Rebuild it
after the backfill via ``scripts/build_ivol_inventory.py`` before running any
audit.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from alphalens.data.alt_data.shares_outstanding import (
    SharesFact,
    latest_shares_as_of,
    parse_company_facts,
)
from alphalens.data.alt_data.ticker_cik_map import TickerCikMap
from alphalens.paper_trade.scorer_v9d import (
    DEFAULT_ETFS,
    DEFAULT_SMD_CACHE_DIR,
)
from alphalens.paper_trade.scorer_v9d import (
    pit_union as _legacy_pit_union,
)

DEFAULT_INVENTORY_PATH = Path.home() / ".alphalens" / "ivolatility_smd_inventory.parquet"
DEFAULT_COMPANYFACTS_DIR = Path.home() / ".alphalens" / "companyfacts"
DEFAULT_TICKER_CIK_MAP_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "alt_data" / "data" / "ticker_cik_map.yaml"
)
DEFAULT_CAP_MIN_USD = 300_000_000.0
DEFAULT_CAP_MAX_USD = 3_000_000_000.0
DEFAULT_MIN_PRE_ROWS = 100
"""Minimum number of trading days a ticker must have on or before asof to be
considered scoreable. Below this the per-asof OLS for v9D risks rank-deficient
fits since the scorer requires ivp30 + 3 equity controls per row."""

# Module-level caches: persist within a process across multiple asof
# iterations so the U3 NBER rebuild does not re-read each ticker's
# companyfacts JSON / SMD parquet 268 times when computing per-asof
# universes for a sub-period. Without these the loader is I/O-bound
# (~187k file reads per cell, ~45 min wall on local SSD); with caching
# the same workload is ~5 sec per cell after warm-up.
_CIK_MAP_CACHE: dict[Path, TickerCikMap] = {}
_COMPANYFACTS_CACHE: dict[tuple[Path, str], list[SharesFact]] = {}
_SMD_HISTORY_CACHE: dict[tuple[Path, str], pd.DataFrame] = {}


def _get_cik_map(path: Path) -> TickerCikMap:
    if path not in _CIK_MAP_CACHE:
        _CIK_MAP_CACHE[path] = TickerCikMap.load(path)
    return _CIK_MAP_CACHE[path]


def clear_universe_caches() -> None:
    """Drop the in-process universe caches.

    Tests should call this when they swap fixture paths between cases so a
    second invocation does not return stale data from the first."""
    _CIK_MAP_CACHE.clear()
    _COMPANYFACTS_CACHE.clear()
    _SMD_HISTORY_CACHE.clear()


def pit_union_legacy(
    *,
    start_year: int = 2008,
    pit_dir: Path | None = None,
    extra_etfs: tuple[str, ...] = DEFAULT_ETFS,
) -> list[str]:
    """U1 — existing PIT yaml universe (today's IWM ∩ historical cap-band).

    Thin wrapper around :func:`alphalens.paper_trade.scorer_v9d.pit_union` for
    naming consistency; default ``start_year=2008`` aligns with the
    retrospective window's earliest sub-period (GFC_recovery)."""
    return _legacy_pit_union(start_year=start_year, pit_dir=pit_dir, extra_etfs=extra_etfs)


def _load_inventory(inventory_path: Path | None) -> pd.DataFrame:
    inv_path = inventory_path or DEFAULT_INVENTORY_PATH
    if not inv_path.exists():
        raise FileNotFoundError(
            f"iVol cache inventory not found at {inv_path}. "
            "Build it via `scripts/build_ivol_inventory.py`."
        )
    df = pd.read_parquet(inv_path)
    for col in ("first_date", "last_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col]).dt.date
    return df


def pit_union_from_ivol_cache(
    asof: date,
    *,
    inventory_path: Path | None = None,
    min_pre_rows: int = DEFAULT_MIN_PRE_ROWS,
    extra_etfs: tuple[str, ...] = DEFAULT_ETFS,
) -> list[str]:
    """U2 — every iVol-cache ticker with valid SMD data on or before ``asof``.

    A ticker is included iff:
    - inventory ``first_date <= asof <= last_date`` (or asof falls inside the
      vendor's coverage window for the ticker),
    - inventory ``n_rows >= min_pre_rows`` (defensive against tickers with
      vestigial coverage).

    Includes delisted firms (after Polygon backfill 2026-05-05) → less
    survivor-biased than U1. Caller still applies ADV filter via the scorer's
    ``adv_min_dollar`` argument; this loader returns the *candidate* set."""
    inv = _load_inventory(inventory_path)
    asof_d = pd.Timestamp(asof).date() if not isinstance(asof, date) else asof
    if inv.empty or "first_date" not in inv.columns:
        # Empty inventory still yields a valid universe of just the seeded ETFs.
        return sorted(set(extra_etfs))
    mask = (
        (inv["first_date"] <= asof_d)
        & (inv["last_date"] >= asof_d)
        & (inv["n_rows"] >= min_pre_rows)
    )
    tickers = set(inv.loc[mask, "ticker"].astype(str).str.upper().tolist())
    tickers |= set(extra_etfs)
    return sorted(tickers)


def _load_companyfacts(cik: str, companyfacts_dir: Path) -> list[SharesFact]:
    key = (companyfacts_dir, cik)
    if key in _COMPANYFACTS_CACHE:
        return _COMPANYFACTS_CACHE[key]
    path = companyfacts_dir / f"{cik}.json"
    if not path.exists():
        _COMPANYFACTS_CACHE[key] = []
        return []
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        _COMPANYFACTS_CACHE[key] = []
        return []
    facts = parse_company_facts(payload, cik=cik)
    _COMPANYFACTS_CACHE[key] = facts
    return facts


def _load_smd_history(ticker: str, smd_cache_dir: Path) -> pd.DataFrame | None:
    """Load + memoize a ticker's full ``(tradeDate, close)`` history.

    Per-process cache so the U3 cap-band loop does not re-parse the same
    parquet for every asof in a sub-period. Returns ``None`` for missing
    or unreadable parquets."""
    key = (smd_cache_dir, ticker)
    if key in _SMD_HISTORY_CACHE:
        return _SMD_HISTORY_CACHE[key]
    path = smd_cache_dir / f"{ticker}.parquet"
    if not path.exists():
        _SMD_HISTORY_CACHE[key] = pd.DataFrame()
        return None
    try:
        df = pd.read_parquet(path, columns=["tradeDate", "close"])
    except Exception:
        _SMD_HISTORY_CACHE[key] = pd.DataFrame()
        return None
    if df.empty or "close" not in df.columns:
        _SMD_HISTORY_CACHE[key] = pd.DataFrame()
        return None
    df = df.dropna(subset=["close"]).copy()
    df["tradeDate"] = pd.to_datetime(df["tradeDate"], errors="coerce")
    df = df.dropna(subset=["tradeDate"]).sort_values("tradeDate")
    _SMD_HISTORY_CACHE[key] = df
    return df


def _load_smd_close(ticker: str, asof: date, smd_cache_dir: Path) -> float | None:
    df = _load_smd_history(ticker, smd_cache_dir)
    if df is None or df.empty:
        return None
    eligible = df.loc[df["tradeDate"].dt.date <= asof]
    if eligible.empty:
        return None
    return float(eligible.iloc[-1]["close"])


def pit_union_nber_rebuild(
    asof: date,
    *,
    inventory_path: Path | None = None,
    smd_cache_dir: Path | None = None,
    companyfacts_dir: Path | None = None,
    ticker_cik_map_path: Path | None = None,
    cap_min_usd: float = DEFAULT_CAP_MIN_USD,
    cap_max_usd: float = DEFAULT_CAP_MAX_USD,
    min_pre_rows: int = DEFAULT_MIN_PRE_ROWS,
    extra_etfs: tuple[str, ...] = DEFAULT_ETFS,
    on_missing_shares: str = "include",
) -> list[str]:
    """U3 — U2 candidates with NBER/Russell mechanical cap-band filter.

    For each U2 candidate:
    - resolve ``ticker → CIK`` via the ticker_cik_map yaml,
    - load XBRL companyfacts shares-outstanding history,
    - compute ``shares_outstanding(asof) × close(asof)``,
    - keep iff ``cap_min_usd <= mcap <= cap_max_usd``.

    ``on_missing_shares`` policy when shares cannot be resolved:
    - ``"include"`` (default) — keep the ticker, treat as cap-band-eligible
      (conservative for a sensitivity loader; preserves coverage),
    - ``"exclude"`` — drop the ticker (strict cap-band conformance)."""
    if on_missing_shares not in {"include", "exclude"}:
        raise ValueError(
            f"on_missing_shares must be 'include' or 'exclude', got {on_missing_shares!r}"
        )
    smd_cache_dir = smd_cache_dir or DEFAULT_SMD_CACHE_DIR
    companyfacts_dir = companyfacts_dir or DEFAULT_COMPANYFACTS_DIR
    cik_map = _get_cik_map(ticker_cik_map_path or DEFAULT_TICKER_CIK_MAP_PATH)

    candidates = pit_union_from_ivol_cache(
        asof,
        inventory_path=inventory_path,
        min_pre_rows=min_pre_rows,
        extra_etfs=(),
    )

    asof_d = pd.Timestamp(asof).date() if not isinstance(asof, date) else asof
    eligible: list[str] = [
        ticker
        for ticker in candidates
        if _is_ticker_in_cap_band(
            ticker,
            asof_d=asof_d,
            cik_map=cik_map,
            companyfacts_dir=companyfacts_dir,
            smd_cache_dir=smd_cache_dir,
            cap_min_usd=cap_min_usd,
            cap_max_usd=cap_max_usd,
            on_missing_shares=on_missing_shares,
        )
    ]
    eligible_set = set(eligible) | set(extra_etfs)
    return sorted(eligible_set)


def _is_ticker_in_cap_band(
    ticker: str,
    *,
    asof_d: date,
    cik_map: TickerCikMap,
    companyfacts_dir: Path,
    smd_cache_dir: Path,
    cap_min_usd: float,
    cap_max_usd: float,
    on_missing_shares: str,
) -> bool:
    """Per-ticker cap-band eligibility check (extracted from
    pit_union_nber_rebuild for cognitive complexity).

    Returns True iff the ticker's market cap on ``asof_d`` falls in
    [cap_min_usd, cap_max_usd], with ``on_missing_shares`` policy applied
    when CIK or shares-outstanding cannot be resolved.
    """
    cik = cik_map.lookup(ticker)
    if cik is None:
        return on_missing_shares == "include"
    facts = _load_companyfacts(cik, companyfacts_dir)
    shares = latest_shares_as_of(facts, asof_d) if facts else None
    if shares is None:
        return on_missing_shares == "include"
    close = _load_smd_close(ticker, asof_d, smd_cache_dir)
    if close is None or close <= 0.0:
        return False
    mcap = float(shares) * float(close)
    return cap_min_usd <= mcap <= cap_max_usd


__all__ = [
    "DEFAULT_CAP_MAX_USD",
    "DEFAULT_CAP_MIN_USD",
    "DEFAULT_COMPANYFACTS_DIR",
    "DEFAULT_INVENTORY_PATH",
    "DEFAULT_MIN_PRE_ROWS",
    "pit_union_from_ivol_cache",
    "pit_union_legacy",
    "pit_union_nber_rebuild",
]
