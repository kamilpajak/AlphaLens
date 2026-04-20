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

import re
from datetime import date
from io import StringIO
from pathlib import Path

import pandas as pd

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
