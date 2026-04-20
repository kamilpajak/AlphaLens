"""Fama-French factor data loader.

Ken French's data library publishes free FF3 daily factors. Refresh quarterly
by re-downloading `F-F_Research_Data_Factors_daily_CSV.zip` from
https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html and
saving to `FF3_DAILY_PATH`.

CSV format (after a 4-line header and a trailing copyright row):
    YYYYMMDD, Mkt-RF, SMB, HML, RF
All values are in **percent** (e.g. `0.45` = 0.45%, not 0.0045). We convert to
decimals on load so consumers can multiply directly against return series.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from .config import FF3_DAILY_PATH

_FF3_COLUMNS = ["Mkt-RF", "SMB", "HML", "RF"]


def load_ff3_daily(
    path: Path | None = None,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Load daily FF3 factors from Ken French CSV, return DataFrame indexed by date.

    Values converted from percent to decimals. Filtered to [start, end] if provided.
    """
    target = Path(path) if path else FF3_DAILY_PATH
    if not target.exists():
        raise FileNotFoundError(f"FF3 factor file not found: {target}")

    # The CSV has a 4-line preamble and a trailing copyright row. We find the
    # header row (the only one that starts with ',Mkt-RF') and parse forward.
    header_idx = None
    with target.open() as fh:
        for i, line in enumerate(fh):
            if line.startswith(",Mkt-RF"):
                header_idx = i
                break
    if header_idx is None:
        raise ValueError(f"Could not find FF3 header row in {target}")

    raw = pd.read_csv(
        target,
        skiprows=header_idx,
        skipinitialspace=True,
        engine="python",
    )
    raw = raw.rename(columns={raw.columns[0]: "date"})

    # Drop any trailing non-data rows (e.g. blank lines, copyright footer).
    raw = raw.dropna(subset=["date"])
    raw = raw[raw["date"].astype(str).str.match(r"^\d{8}$", na=False)]

    raw["date"] = pd.to_datetime(raw["date"].astype(int).astype(str), format="%Y%m%d")
    raw = raw.set_index("date").sort_index()

    # Convert percent → decimal.
    for col in _FF3_COLUMNS:
        raw[col] = pd.to_numeric(raw[col], errors="coerce") / 100.0

    if start is not None:
        raw = raw.loc[pd.Timestamp(start):]
    if end is not None:
        raw = raw.loc[:pd.Timestamp(end)]
    return raw[_FF3_COLUMNS]
