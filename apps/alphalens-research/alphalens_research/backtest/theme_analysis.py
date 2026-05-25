# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUnknownLambdaType=false
"""Theme concentration analysis over time — factor-aware monitoring.

Backtest `rebalance_results` carry the top-N tickers each day. This module maps
tickers to themes (from the curated `universe.yaml` basket) and computes:

- daily theme weights (% of portfolio per theme)
- Herfindahl concentration index (HHI) at the theme level
- dominant theme per day
- aggregates: mean weight per theme, fraction of days a single theme exceeds
  a threshold

Used in two places:

1. **Backtest report** — detects whether Sharpe depended on a single thematic
   regime (e.g. the 2024 AI bull) and whether concentration drifted over time.
2. **Production Layer 2b** — emits a Telegram alert when today's top-N picks
   are too concentrated in one theme.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class ThemeSnapshot:
    """Single-day view of thematic allocation."""

    date: pd.Timestamp
    top_n_tickers: tuple[str, ...]
    theme_weights: Mapping[str, float]  # normalized, sum ≤ 1.0 (less when names are unclassified)
    dominant_theme: str | None
    hhi: float  # Herfindahl index over theme weights, [0, 1]
    unclassified_fraction: float  # names with no theme mapping


def _resolve_position_weights(
    tickers: list[str], position_weights: Iterable[float] | None
) -> list[float]:
    if position_weights is None:
        return [1.0 / len(tickers)] * len(tickers)
    weights = list(position_weights)
    if len(weights) != len(tickers):
        raise ValueError(f"position_weights length {len(weights)} != tickers {len(tickers)}")
    return weights


def _accumulate_theme_weights(
    tickers: list[str],
    weights: list[float],
    themes_map: Mapping[str, list[str]],
) -> tuple[dict[str, float], float]:
    """Build {theme → summed weight} + unclassified bucket. Multi-theme tickers split equally."""
    theme_sums: dict[str, float] = {}
    unclassified = 0.0
    for ticker, weight in zip(tickers, weights, strict=False):
        themes = themes_map.get(ticker, [])
        if not themes:
            unclassified += weight
            continue
        split = weight / len(themes)
        for theme in themes:
            theme_sums[theme] = theme_sums.get(theme, 0.0) + split
    return theme_sums, unclassified


def _empty_snapshot(date: pd.Timestamp | None) -> ThemeSnapshot:
    return ThemeSnapshot(
        date=date or pd.Timestamp(0),
        top_n_tickers=(),
        theme_weights={},
        dominant_theme=None,
        hhi=0.0,
        unclassified_fraction=0.0,
    )


def snapshot_themes(
    top_n_tickers: Iterable[str],
    themes_map: Mapping[str, list[str]],
    date: pd.Timestamp | None = None,
    position_weights: Iterable[float] | None = None,
) -> ThemeSnapshot:
    """Map tickers to themes and compute theme weights.

    `themes_map`: {ticker → [list of themes]}, e.g. `flatten_universe()` output.
    Multi-theme tickers split their weight equally (e.g. FORM may be in both
    quantum and semis → 50/50 across both).

    `position_weights` is optional — if provided, replaces equal-weight per
    ticker. Must have the same length as `top_n_tickers`.
    """
    tickers = list(top_n_tickers)
    if not tickers:
        return _empty_snapshot(date)

    weights = _resolve_position_weights(tickers, position_weights)
    theme_sums, unclassified = _accumulate_theme_weights(tickers, weights, themes_map)

    total = sum(theme_sums.values()) + unclassified
    if total <= 0:
        theme_weights: dict[str, float] = {}
        uncl_frac = 0.0
    else:
        theme_weights = {k: v / total for k, v in theme_sums.items()}
        uncl_frac = unclassified / total

    dominant = max(theme_weights, key=lambda k: theme_weights[k]) if theme_weights else None
    # HHI includes the unclassified bucket so it's not free to grow.
    all_weights = [*theme_weights.values(), uncl_frac] if total > 0 else []
    hhi = sum(w * w for w in all_weights)

    return ThemeSnapshot(
        date=date or pd.Timestamp(0),
        top_n_tickers=tuple(tickers),
        theme_weights=theme_weights,
        dominant_theme=dominant,
        hhi=hhi,
        unclassified_fraction=uncl_frac,
    )


@dataclass(frozen=True)
class ThemeSeriesStats:
    """Theme concentration aggregates across the full backtest window."""

    all_themes: tuple[str, ...]
    mean_weights: Mapping[str, float]  # mean weight per theme over the window
    days_dominant: Mapping[str, int]  # days the theme was dominant
    mean_hhi: float  # mean HHI over the window
    concentration_alert_days: int  # days where max theme > threshold
    concentration_threshold: float


def theme_series(
    snapshots: Iterable[ThemeSnapshot],
    concentration_threshold: float = 0.70,
) -> tuple[pd.DataFrame, ThemeSeriesStats]:
    """Collect a list of daily snapshots into a DataFrame (date × theme → weight).

    Returns two objects:
    - DataFrame with one column per theme (plus `unclassified`, `hhi`)
    - `ThemeSeriesStats` with aggregates
    """
    rows: list[dict[str, Any]] = []
    for snap in snapshots:
        row: dict[str, Any] = {"date": snap.date, **snap.theme_weights}
        row["unclassified"] = snap.unclassified_fraction
        row["hhi"] = snap.hhi
        row["dominant_theme"] = snap.dominant_theme or ""
        rows.append(row)
    df = pd.DataFrame(rows)

    if df.empty:
        return df, ThemeSeriesStats(
            all_themes=(),
            mean_weights={},
            days_dominant={},
            mean_hhi=0.0,
            concentration_alert_days=0,
            concentration_threshold=concentration_threshold,
        )

    df = df.set_index("date") if "date" in df.columns else df

    # Theme columns = everything except metadata
    meta = {"unclassified", "hhi", "dominant_theme"}
    theme_cols = tuple(c for c in df.columns if c not in meta)

    mean_weights = {c: float(df[c].fillna(0.0).mean()) for c in theme_cols}
    if "dominant_theme" in df.columns:
        raw_days_dominant = df["dominant_theme"].value_counts().to_dict()
    else:
        raw_days_dominant = {}
    # Drop the empty string entry (days with no dominant theme); cast Hashable
    # keys back to str (value_counts on a string column always yields str keys
    # at runtime but pandas-stubs declares the dict as Hashable -> Any).
    days_dominant: dict[str, int] = {str(k): int(v) for k, v in raw_days_dominant.items() if k}

    mean_hhi = float(df["hhi"].mean()) if "hhi" in df.columns else 0.0

    alert_days = 0
    if theme_cols:
        max_theme = df[list(theme_cols)].fillna(0.0).max(axis=1)
        alert_days = int((max_theme > concentration_threshold).sum())

    stats = ThemeSeriesStats(
        all_themes=theme_cols,
        mean_weights=mean_weights,
        days_dominant=days_dominant,
        mean_hhi=mean_hhi,
        concentration_alert_days=alert_days,
        concentration_threshold=concentration_threshold,
    )
    return df, stats


def format_theme_summary(stats: ThemeSeriesStats, n_total_days: int) -> str:
    """Human-readable report for the markdown summary section."""
    lines = []
    lines.append(
        f"Backtest window: {n_total_days} days, "
        f"concentration threshold = {stats.concentration_threshold * 100:.0f}%"
    )
    lines.append(f"Mean HHI: {stats.mean_hhi:.3f} (0 = perfect diversification, 1 = single theme)")
    lines.append(
        f"Days with max theme > {stats.concentration_threshold * 100:.0f}%: "
        f"{stats.concentration_alert_days} / {n_total_days} "
        f"({stats.concentration_alert_days / max(n_total_days, 1) * 100:.1f}%)"
    )
    lines.append("")
    lines.append("| Theme | Mean weight | Days dominant |")
    lines.append("|---|---:|---:|")
    for theme in sorted(
        stats.all_themes, key=lambda t: stats.mean_weights.get(t, 0.0), reverse=True
    ):
        avg = stats.mean_weights.get(theme, 0.0)
        dom = stats.days_dominant.get(theme, 0)
        lines.append(
            f"| {theme} | {avg * 100:.1f}% | {dom} ({dom / max(n_total_days, 1) * 100:.1f}%) |"
        )
    return "\n".join(lines)


def snapshots_from_backtest(
    rebalance_results: Iterable[Any],
    themes_map: Mapping[str, list[str]],
) -> list[ThemeSnapshot]:
    """Convenience: build snapshots from `BacktestReport.rebalance_results`.

    Uses score-equal-weight (ignores linear/conviction). For an accurate
    weighted theme breakdown, pass `position_weights` per snap explicitly.
    """
    out = []
    for r in rebalance_results:
        out.append(
            snapshot_themes(
                top_n_tickers=r.top_n_tickers,
                themes_map=themes_map,
                date=r.date,
            )
        )
    return out
