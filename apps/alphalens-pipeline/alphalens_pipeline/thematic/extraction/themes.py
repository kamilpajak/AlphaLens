"""Rolling theme aggregator + novelty scorer over Layer-2 extraction parquets.

Reads daily ``thematic_events/{YYYY-MM-DD}.parquet`` files within the lookback
window, explodes the ``themes`` column, and ranks each theme by (a) total
occurrence in the window and (b) novelty — how strongly the last 7 days
over-index versus the trailing baseline. Novelty ≥ 3 flags a theme as a Phase
C trigger candidate (per design memo §2 Layer 3 trigger condition).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

from alphalens_pipeline.thematic.theme_text import slugify_theme

DEFAULT_EVENTS_DIR = Path.home() / ".alphalens" / "thematic_events"
DEFAULT_WINDOW_DAYS = 30
DEFAULT_RECENT_DAYS = 7
DEFAULT_NOVELTY_THRESHOLD = 3.0

# Bump for a code-level change to how novelty is COMPUTED (the roll_up ratio
# formula or normalization) that the three numeric params below cannot express.
_NOVELTY_CONFIG_SCHEMA = 1


def novelty_config_version(*, window_days: int, recent_days: int, threshold: float) -> str:
    """Canonical JSON token of the novelty config that ranked a theme.

    Stamped alongside ``novelty_rank``/``novelty_score`` on the candidate parquet
    so a future EDGE attribution pass can pool only outcomes scored under the
    SAME novelty definition. A deliberate tune of the lookback window, the recent
    sub-window, or the flag threshold must make pre- vs post-change novelty values
    non-comparable — so this token fingerprints all three. Bump
    :data:`_NOVELTY_CONFIG_SCHEMA` for a code-level formula change the params
    cannot capture. Mirrors :func:`mapper_config_version` / ``ladder_config_version``.
    """
    payload = {
        "schema": _NOVELTY_CONFIG_SCHEMA,
        "window_days": int(window_days),
        "recent_days": int(recent_days),
        "threshold": float(threshold),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


_OUTPUT_COLUMNS = [
    "theme",
    "count_window",
    "count_recent",
    "count_baseline",
    "novelty_score",
    "first_seen",
    "latest_seen",
]


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=object) for c in _OUTPUT_COLUMNS})


def _load_window(events_dir: Path, asof: dt.date, window_days: int) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    lo = asof - dt.timedelta(days=window_days)
    for path in sorted(events_dir.glob("*.parquet")):
        try:
            date = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if date < lo or date > asof:
            continue
        df = pd.read_parquet(path)
        if df.empty:
            continue
        df["_event_date"] = pd.Timestamp(date, tz="UTC")
        rows.append(df)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def roll_up(
    *,
    asof: dt.date,
    events_dir: Path = DEFAULT_EVENTS_DIR,
    window_days: int = DEFAULT_WINDOW_DAYS,
    recent_days: int = DEFAULT_RECENT_DAYS,
) -> pd.DataFrame:
    """Aggregate themes across ``[asof - window_days, asof]``; score recency-vs-baseline.

    ``novelty_score = count_recent / max(count_baseline, 1) * (baseline_days / recent_days)``
    so a theme appearing at the same DAILY rate in recent vs baseline scores 1.0;
    appearing 3× more frequently in the recent window scores 3.0.
    """
    if not events_dir.exists():
        return _empty_frame()

    df = _load_window(events_dir, asof, window_days)
    if df.empty:
        return _empty_frame()

    exploded = df[["_event_date", "themes"]].explode("themes").rename(columns={"themes": "theme"})
    exploded = exploded.dropna(subset=["theme"])
    # Slugify on read so format variants ("AI ethics" / "AI_ethics") collapse to
    # ONE theme across the rolling window — a write-format change can never
    # spuriously split a theme or flag it novel. Idempotent on already-slug rows.
    exploded["theme"] = exploded["theme"].astype(str).map(slugify_theme)
    exploded = exploded[exploded["theme"] != ""]
    if exploded.empty:
        return _empty_frame()

    recent_cutoff = pd.Timestamp(asof, tz="UTC") - pd.Timedelta(days=recent_days)
    exploded["is_recent"] = exploded["_event_date"] >= recent_cutoff

    baseline_days = max(window_days - recent_days, 1)
    scale = baseline_days / max(recent_days, 1)

    grouped = exploded.groupby("theme", as_index=False).agg(
        count_window=("theme", "size"),
        count_recent=("is_recent", "sum"),
        first_seen=("_event_date", "min"),
        latest_seen=("_event_date", "max"),
    )
    grouped["count_baseline"] = (grouped["count_window"] - grouped["count_recent"]).clip(lower=0)
    # ``clip(lower=1)`` absorbs the zero-baseline edge case natively:
    # count_recent / max(count_baseline, 1) * scale == count_recent * scale
    # when count_baseline == 0, so no separate new-themes branch is required.
    grouped["novelty_score"] = (
        grouped["count_recent"] / grouped["count_baseline"].clip(lower=1)
    ) * scale

    return (
        grouped[_OUTPUT_COLUMNS]
        .sort_values(["novelty_score", "count_window"], ascending=[False, False])
        .reset_index(drop=True)
    )


def flag_novel(
    rollup: pd.DataFrame, *, threshold: float = DEFAULT_NOVELTY_THRESHOLD
) -> pd.DataFrame:
    """Filter the rollup to themes whose ``novelty_score`` clears the threshold."""
    if rollup.empty:
        return rollup
    return rollup[rollup["novelty_score"] >= threshold].reset_index(drop=True)


__all__ = [
    "DEFAULT_EVENTS_DIR",
    "DEFAULT_NOVELTY_THRESHOLD",
    "DEFAULT_RECENT_DAYS",
    "DEFAULT_WINDOW_DAYS",
    "flag_novel",
    "novelty_config_version",
    "roll_up",
]
