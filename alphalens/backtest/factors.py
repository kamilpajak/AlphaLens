"""Fama-French / Momentum / Industry factor data loaders.

Source: Ken French's data library (https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html).
All files are free CSVs; refresh quarterly by re-downloading:

    F-F_Research_Data_5_Factors_2x3_daily_CSV.zip
    F-F_Momentum_Factor_daily_CSV.zip
    12_Industry_Portfolios_daily_CSV.zip

Default location: ``~/.alphalens/factors/``.

CSV format across all three files: a multi-line preamble, a header row beginning
with a comma (``,Mkt-RF,...``, ``,Mom``, or ``,NoDur,Durbl,...``), then 8-digit
date rows (YYYYMMDD), then a blank line and optionally another section
(equal-weighted returns for the industry portfolio file) or a copyright footer.
All returns are published in **percent** and are converted to decimals on load.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from io import StringIO
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

_DATE_ROW_RE = re.compile(r"^\s*\d{8}\s*,")

DEFAULT_FACTORS_DIR = Path.home() / ".alphalens" / "factors"
DEFAULT_FF5_PATH = DEFAULT_FACTORS_DIR / "F-F_Research_Data_5_Factors_2x3_daily.csv"
DEFAULT_UMD_PATH = DEFAULT_FACTORS_DIR / "F-F_Momentum_Factor_daily.csv"
DEFAULT_INDUSTRY12_PATH = DEFAULT_FACTORS_DIR / "12_Industry_Portfolios_Daily.csv"

_FF5_COLUMNS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]
_INDUSTRY12_COLUMNS = [
    "NoDur", "Durbl", "Manuf", "Enrgy", "Chems", "BusEq",
    "Telcm", "Utils", "Shops", "Hlth", "Money", "Other",
]


def _parse_dartmouth_section(
    target: Path,
    header_signature: str,
    expected_columns: list[str],
) -> pd.DataFrame:
    """Locate header line, parse contiguous 8-digit date rows after it, stop at
    the first non-data line (blank / next section / footer). Percent → decimal.
    """
    if not target.exists():
        raise FileNotFoundError(f"Factor file not found: {target}")

    with target.open() as fh:
        lines = fh.readlines()

    header_idx: int | None = None
    for i, line in enumerate(lines):
        if line.startswith(header_signature):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(
            f"Could not find header row starting with {header_signature!r} in {target}"
        )

    data_lines: list[str] = []
    for line in lines[header_idx + 1:]:
        if _DATE_ROW_RE.match(line):
            data_lines.append(line)
        elif data_lines:
            break  # End of this section (blank line, next header, or footer).

    if not data_lines:
        raise ValueError(f"No data rows found after header in {target}")

    buf = StringIO(lines[header_idx] + "".join(data_lines))
    raw = pd.read_csv(buf, skipinitialspace=True, engine="python")
    raw = raw.rename(columns={raw.columns[0]: "date"})
    raw["date"] = pd.to_datetime(raw["date"].astype(int).astype(str), format="%Y%m%d")
    raw = raw.set_index("date").sort_index()

    for col in expected_columns:
        if col not in raw.columns:
            raise ValueError(f"Column {col!r} missing from {target}")
        raw[col] = pd.to_numeric(raw[col], errors="coerce") / 100.0

    return raw[expected_columns]


def _apply_date_filter(
    df: pd.DataFrame, start: date | None, end: date | None
) -> pd.DataFrame:
    if start is not None:
        df = df.loc[pd.Timestamp(start):]
    if end is not None:
        df = df.loc[:pd.Timestamp(end)]
    return df


def load_ff5_daily(
    path: Path | None = None,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Load daily Fama-French 5-factor + RF (Mkt-RF, SMB, HML, RMW, CMA, RF)."""
    target = Path(path) if path else DEFAULT_FF5_PATH
    df = _parse_dartmouth_section(
        target,
        header_signature=",Mkt-RF",
        expected_columns=_FF5_COLUMNS,
    )
    return _apply_date_filter(df, start, end)


def load_umd_daily(
    path: Path | None = None,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Load daily momentum factor (Mom). Column name matches Dartmouth's ``Mom``."""
    target = Path(path) if path else DEFAULT_UMD_PATH
    df = _parse_dartmouth_section(
        target,
        header_signature=",Mom",
        expected_columns=["Mom"],
    )
    return _apply_date_filter(df, start, end)


def load_industry12_daily(
    path: Path | None = None,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Load daily 12-industry value-weighted returns.

    The Dartmouth file also contains an equal-weighted section later in the same
    CSV; we only read value-weighted (standard convention). The VW header row
    is the first ``,NoDur,Durbl,...`` line in the file.
    """
    target = Path(path) if path else DEFAULT_INDUSTRY12_PATH
    df = _parse_dartmouth_section(
        target,
        header_signature=",NoDur",
        expected_columns=_INDUSTRY12_COLUMNS,
    )
    return _apply_date_filter(df, start, end)


def load_carhart_daily(
    ff5_path: Path | None = None,
    umd_path: Path | None = None,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return Carhart-4F-ready merged DataFrame: Mkt-RF, SMB, HML, Mom, RF.

    Inner-joins FF5 (dropping RMW/CMA since Carhart omits them) with UMD on date.
    """
    ff5 = load_ff5_daily(path=ff5_path, start=start, end=end)
    umd = load_umd_daily(path=umd_path, start=start, end=end)
    merged = ff5[["Mkt-RF", "SMB", "HML", "RF"]].join(umd, how="inner")
    return merged


DEFAULT_Q4_DIR = DEFAULT_FACTORS_DIR / "q4"
_Q4_COLUMNS = ["R_F", "R_MKT", "R_ME", "R_IA", "R_ROE", "R_EG"]  # q5; Q4 drops R_EG
_Q4_URL_BASE = "https://global-q.org/uploads/1/2/2/6/122679606"


def _q4_cumulative_url() -> str:
    return f"{_Q4_URL_BASE}/q5_factors_daily.csv"


def _q4_yearly_url(year: int) -> str:
    return f"{_Q4_URL_BASE}/q5_factors_daily_{year}.csv"


def _parse_q4_csv(text: str) -> pd.DataFrame:
    """Parse a global-q.org daily CSV (percent returns, YYYYMMDD date column).

    Schema: ``DATE,R_F,R_MKT,R_ME,R_IA,R_ROE,R_EG``. We keep only the Q4
    subset (Mkt, ME, I/A, ROE) and RF; R_EG is the q5 extension, not
    part of Hou-Xue-Zhang 2015.
    """
    raw = pd.read_csv(StringIO(text))
    raw["DATE"] = pd.to_datetime(raw["DATE"].astype(int).astype(str), format="%Y%m%d")
    raw = raw.set_index("DATE").sort_index()
    raw.index.name = "date"

    for col in _Q4_COLUMNS:
        if col not in raw.columns:
            raise ValueError(f"Q4 CSV missing expected column {col!r}")
        raw[col] = pd.to_numeric(raw[col], errors="coerce") / 100.0

    return raw.rename(
        columns={"R_F": "RF", "R_MKT": "Mkt-RF", "R_ME": "ME", "R_IA": "IA", "R_ROE": "ROE"}
    )[["Mkt-RF", "ME", "IA", "ROE", "RF"]]


def _fetch_q4_text(url: str) -> str:
    import requests

    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def load_q4_daily(
    cache_dir: Path | None = None,
    start: date | None = None,
    end: date | None = None,
    *,
    fetch: "callable | None" = None,
) -> pd.DataFrame:
    """Load Hou-Xue-Zhang q-factor daily returns (Q4: Mkt-RF, ME, I/A, ROE).

    Global-q.org publishes in two parts: a cumulative 1967-2018 file plus
    per-year files 2019+. Latest available at time of writing: 2024 yearly
    (2025-2026 coverage gap — downstream callers must handle by restricting
    date range or accepting partial OOS attribution per Phase 3b plan §3b.2).

    On first use downloads all parts to ``cache_dir`` and concatenates.
    Subsequent calls read the cache. ``fetch`` injected for testing.
    """
    target_dir = cache_dir or DEFAULT_Q4_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    fetcher = fetch or _fetch_q4_text

    # Cumulative file (1967-2018) always required; yearly files 2019→current.
    # Auto-extend to current year so newly-published files get picked up when
    # global-q.org drops them. Unpublished years 404 — skip silently.
    files: list[tuple[str, str]] = [("cumulative.csv", _q4_cumulative_url())]
    current_year = date.today().year
    for year in range(2019, current_year + 1):
        files.append((f"{year}.csv", _q4_yearly_url(year)))

    frames: list[pd.DataFrame] = []
    for filename, url in files:
        path = target_dir / filename
        if not path.exists():
            try:
                path.write_text(fetcher(url))
            except Exception as exc:  # noqa: BLE001 — skip unpublished year files
                logger.debug("q4 yearly file %s not available: %s", filename, exc)
                continue
        frames.append(_parse_q4_csv(path.read_text()))

    combined = (
        pd.concat(frames)
        .sort_index()
        .loc[lambda df: ~df.index.duplicated(keep="last")]
    )
    return _apply_date_filter(combined, start, end)


def load_ff5_umd_daily(
    ff5_path: Path | None = None,
    umd_path: Path | None = None,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Return FF5+UMD (6-factor) merged DataFrame.

    Columns: Mkt-RF, SMB, HML, RMW, CMA, Mom, RF. Used as the Carhart-4F
    robustness check in Phase 3b validation — if Carhart α passes but FF5+UMD
    α attenuates >30%, the alpha was loading on RMW/CMA (profitability /
    investment) rather than an independent edge (design doc §7 R5).
    """
    ff5 = load_ff5_daily(path=ff5_path, start=start, end=end)
    umd = load_umd_daily(path=umd_path, start=start, end=end)
    return ff5.join(umd, how="inner")
