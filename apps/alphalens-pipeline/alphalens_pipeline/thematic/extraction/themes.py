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
import math
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from alphalens_pipeline.data.parquet_io import write_parquet_atomic
from alphalens_pipeline.thematic.theme_text import slugify_theme

DEFAULT_EVENTS_DIR = Path.home() / ".alphalens" / "thematic_events"
DEFAULT_THEME_ROLLUP_DIR = Path.home() / ".alphalens" / "theme_rollup"
DEFAULT_WINDOW_DAYS = 30
DEFAULT_RECENT_DAYS = 7
DEFAULT_NOVELTY_THRESHOLD = 3.0
_LN10 = math.log(10.0)

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
    "rate_surprise",
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
    # Sample-size-aware companion to the ratio above, recorded alongside it so a
    # later selection change can be replayed from the stored rollup instead of
    # re-run. Mapped over the SCALAR :func:`rate_surprise` rather than
    # reimplemented vectorised: one implementation cannot disagree with itself,
    # and the cost is one pass over ~11k themes once per map-themes run.
    grouped["rate_surprise"] = [
        rate_surprise(
            int(recent), int(baseline), recent_days=recent_days, baseline_days=baseline_days
        )
        for recent, baseline in zip(grouped["count_recent"], grouped["count_baseline"], strict=True)
    ]

    return (
        grouped[_OUTPUT_COLUMNS]
        .sort_values(["novelty_score", "count_window"], ascending=[False, False])
        .reset_index(drop=True)
    )


def rate_surprise(
    count_recent: int,
    count_baseline: int,
    *,
    recent_days: int,
    baseline_days: int,
) -> float:
    """How unlikely is ``count_recent`` under the rate the baseline implies.

    Returned as ``-log10 P(X >= count_recent)`` for a Poisson arrival process, so
    bigger means more surprising and the scale stays readable (3.0 == a one-in-a-
    thousand week).

    This exists because :func:`roll_up`'s ``novelty_score`` — a plain
    recent/baseline ratio — is blind to how much evidence stands behind it. With
    the production constants a theme with 3 recent articles against a baseline of
    1 scores 9.86 and clears the 3.0 threshold, while 120 recent against a
    baseline of 200 scores 1.97 and is rejected; 30/50, 60/100 and 120/200 all
    score identically. Three articles outranking a hundred and twenty is not a
    threshold-tuning problem, it is the wrong statistic.

    ``+0.5`` smoothing on the baseline is deliberate: a brand-new theme has no
    baseline at all, and the unsmoothed tail probability for it is 0 — an
    infinite score that would make every first-sighting singleton outrank
    everything else forever, which is the failure this is meant to replace. It is
    a choice, not a derivation: its effect on the ordering among the smallest
    themes is unvalidated, and that is the regime this score exists to fix.

    KNOWN BIAS, measured not assumed. Poisson wants variance == mean; news
    arrivals cluster, because one story yields many articles the same day. Over
    30 days of real ``thematic_events`` the index of dispersion across the 161
    themes with >=20 occurrences has median 1.31 (p25 1.03, p75 1.63) — mild, but
    the tail runs much hotter: ``earnings`` 4.22, ``defense`` 2.33, ``inflation``
    2.14. A theme with dispersion phi has its z inflated by ~sqrt(phi), so this
    score OVERSTATES the surprise of exactly the clustered themes — roughly 2x
    for ``earnings``. That is the mechanism behind the 50-day replay in which
    switching selection to this score would have added ``earnings`` 21 times.
    Correcting it needs a per-theme dispersion estimate (quasi-Poisson or
    negative binomial), which is only possible once the per-theme daily counts
    are being stored — which is what :func:`write_theme_rollup` starts doing.

    TELEMETRY ONLY, and it MUST NOT drive selection until that dispersion estimate
    exists — the bias above runs in the same direction as the themes a selector
    would most over-pick. ``flag_novel`` still ranks on the ratio.
    """
    if count_recent <= 0:
        return 0.0
    # Imported here, not at module scope: `themes` is reachable from CLI startup
    # and scipy is not cheap to import, while the edgar-detect poller (which
    # never touches this function) fires every 15 minutes.
    from scipy.stats import poisson

    expected = (count_baseline + 0.5) / max(baseline_days, 1) * recent_days
    log_tail = float(poisson.logsf(count_recent - 1, expected))
    if math.isfinite(log_tail):
        return -log_tail / _LN10
    # scipy's tail underflows to -inf well inside the reachable range (around
    # count_recent 500 against a 100 baseline). An infinity in a telemetry column
    # is worse than a plateau — it breaks ranking and poisons any later
    # aggregate — so fall back to the closed-form Chernoff/KL bound for a Poisson
    # upper tail, which is finite, strictly increasing in ``count_recent``, and
    # agrees with the exact value to ~1% where the two meet. Above that crossover
    # the stored score is therefore a conservative UPPER BOUND on the surprise,
    # not the tail itself — close ranks up there are not meaningfully separated.
    ratio = count_recent / expected
    return expected * (ratio * math.log(ratio) - ratio + 1.0) / _LN10


THEME_ROLLUP_COLUMNS = (
    "asof",
    "theme",
    "count_window",
    "count_recent",
    "count_baseline",
    "novelty_score",
    "novelty_rank",
    "rate_surprise",
    "rate_surprise_rank",
    # Kept, not dropped: "was this theme first seen yesterday?" is a question a
    # replay will want and the columns are already computed upstream.
    "first_seen",
    "latest_seen",
    "selected",
    "novelty_config_version",
)


def write_theme_rollup(
    asof: dt.date,
    rollup: pd.DataFrame,
    *,
    selected: Sequence[str],
    out_dir: Path,
    novelty_config_version: str,
) -> Path | None:
    """Persist the day's FULL theme ranking; return its path, or ``None`` if empty.

    One row per theme in the window — not per theme that was mapped. Selection
    keeps ten and drops the rest, so without this the question "what would a
    different rule have picked on that day?" is unanswerable once the day passes.
    Both scores are stored with their own rank, so any policy that is a function
    of the counts can be replayed offline against real days for the price of a
    parquet read, with no LLM calls and no change to production.

    ``selected`` is the set of themes actually handed to the mapper, which is the
    ratio's top-N AFTER truncation — recording the flag separately means a replay
    never has to reconstruct the truncation rule to know what really happened.
    """
    if rollup is None or rollup.empty:
        return None
    frame = rollup.copy()
    frame["asof"] = asof.isoformat()
    frame["selected"] = frame["theme"].isin(set(selected))
    frame["novelty_config_version"] = novelty_config_version
    # Dense 1-based ranks, highest score first. Computed here rather than left to
    # the reader so a replay compares the ranks that were really produced that day.
    frame["novelty_rank"] = (
        frame["novelty_score"].rank(ascending=False, method="min").astype("Int64")
    )
    frame["rate_surprise_rank"] = (
        frame["rate_surprise"].rank(ascending=False, method="min").astype("Int64")
    )
    frame = frame.reindex(columns=list(THEME_ROLLUP_COLUMNS))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{asof.isoformat()}.parquet"
    write_parquet_atomic(frame, path, index=False)
    return path


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
    "DEFAULT_THEME_ROLLUP_DIR",
    "DEFAULT_WINDOW_DAYS",
    "flag_novel",
    "novelty_config_version",
    "rate_surprise",
    "roll_up",
    "write_theme_rollup",
]
