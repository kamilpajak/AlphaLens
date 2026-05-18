"""Export ~/.alphalens/thematic_briefs/*.parquet into JSON for the web UI.

Outputs:
- web/static/data/days.json         — index: [{date, n_candidates, n_themes, top_theme}]
- web/static/data/days/<date>.json  — full per-day brief with all candidates
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"
OUT_DIR = Path(__file__).resolve().parent.parent / "web" / "static" / "data"


def _to_jsonable(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (np.floating,)):
        v = float(value)
        return None if math.isnan(v) else v
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, np.ndarray):
        return [_to_jsonable(v) for v in value.tolist()]
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    return value


def _row_to_dict(row: pd.Series) -> dict:
    return {k: _to_jsonable(v) for k, v in row.items()}


def export_day(parquet_path: Path) -> dict:
    df = pd.read_parquet(parquet_path)
    date_str = parquet_path.stem
    candidates = [_row_to_dict(row) for _, row in df.iterrows()]

    theme_counts = df["theme"].value_counts().to_dict()
    # Sort by count desc, theme asc for deterministic tiebreak.
    top_theme = max(theme_counts, key=lambda k: (theme_counts[k], k)) if theme_counts else None

    payload = {
        "date": date_str,
        "n_candidates": len(df),
        "n_themes": int(df["theme"].nunique()),
        "top_theme": top_theme,
        "theme_counts": {k: int(v) for k, v in theme_counts.items()},
        "candidates": candidates,
    }
    return payload


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    days_dir = OUT_DIR / "days"
    days_dir.mkdir(parents=True, exist_ok=True)

    parquet_files = sorted(BRIEFS_DIR.glob("*.parquet"))
    if not parquet_files:
        print(f"no parquet files in {BRIEFS_DIR}")
        return

    index = []
    for pq in parquet_files:
        payload = export_day(pq)
        out = days_dir / f"{payload['date']}.json"
        _atomic_write_text(out, json.dumps(payload, indent=2))
        index.append(
            {
                "date": payload["date"],
                "n_candidates": payload["n_candidates"],
                "n_themes": payload["n_themes"],
                "top_theme": payload["top_theme"],
            }
        )
        print(
            f"wrote {out.relative_to(OUT_DIR.parent.parent)} ({payload['n_candidates']} candidates)"
        )

    index.sort(key=lambda d: d["date"], reverse=True)
    _atomic_write_text(OUT_DIR / "days.json", json.dumps(index, indent=2))
    print(f"wrote days.json index ({len(index)} days)")


def _atomic_write_text(target: Path, content: str) -> None:
    """Write `content` to `target` atomically (tmp file + os.replace).

    Partial reads from a SvelteKit dev server (which watches `static/data/`)
    would otherwise parse a half-written JSON file. os.replace is atomic on
    POSIX and Windows.
    """
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(target)


if __name__ == "__main__":
    main()
