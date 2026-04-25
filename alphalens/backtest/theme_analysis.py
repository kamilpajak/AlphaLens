"""Analiza koncentracji tematów w czasie — factor-aware monitoring.

Backtest'owe `daily_results` trzymają listy top-N tickerów każdego dnia. Ten
moduł mapuje tickery do tematów (z `universe.yaml` curated basketa) i liczy:

- daily theme weights (% portfolio w każdym temacie)
- Herfindahl concentration index (HHI) na poziomie tematu
- dominujący temat per dzień
- agregaty: średnia wag per theme, % dni gdzie jeden theme > próg

Używany w dwóch miejscach:

1. **Backtest raport** — wykrywa czy Sharpe zależał od jednej konkretnej
   sytuacji tematycznej (np. 2024 AI bull) i czy koncentracja drift'owała
   w czasie.
2. **Produkcyjny Layer 2b** — emituje ostrzeżenie do Telegrama gdy dzisiejsza
   top-N picks jest zbyt skoncentrowana w jednym temacie.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class ThemeSnapshot:
    """Jednodniowy widok alokacji tematycznej."""

    date: pd.Timestamp
    top_n_tickers: tuple[str, ...]
    theme_weights: Mapping[str, float]  # normalized, sum ≤ 1.0 (może < 1.0 gdy niesklasyfikowane)
    dominant_theme: str | None
    hhi: float  # Herfindahl index w przestrzeni wag tematów, [0, 1]
    unclassified_fraction: float  # nazwy bez żadnego theme mapping'u


def snapshot_themes(
    top_n_tickers: Iterable[str],
    themes_map: Mapping[str, list[str]],
    date: pd.Timestamp | None = None,
    position_weights: Iterable[float] | None = None,
) -> ThemeSnapshot:
    """Zmap tickery na tematy i oblicz wagi tematów.

    `themes_map`: {ticker → [lista tematów]}, np. output `flatten_universe()`.
    Jeśli ticker ma wiele tematów, dzielimy jego wagę równo (np. FORM może
    być w quantum i semis → 50/50 na oba).

    `position_weights` opcjonalne — jeśli podane, użyjemy ich zamiast equal-weight
    dla każdego tickera. Musi mieć tę samą długość co `top_n_tickers`.
    """
    tickers = list(top_n_tickers)
    if not tickers:
        return ThemeSnapshot(
            date=date or pd.Timestamp(0),
            top_n_tickers=(),
            theme_weights={},
            dominant_theme=None,
            hhi=0.0,
            unclassified_fraction=0.0,
        )

    if position_weights is None:
        position_weights = [1.0 / len(tickers)] * len(tickers)
    else:
        position_weights = list(position_weights)
        if len(position_weights) != len(tickers):
            raise ValueError(
                f"position_weights length {len(position_weights)} != tickers {len(tickers)}"
            )

    theme_sums: dict[str, float] = {}
    unclassified = 0.0
    for ticker, weight in zip(tickers, position_weights):
        themes = themes_map.get(ticker, [])
        if not themes:
            unclassified += weight
            continue
        split = weight / len(themes)
        for theme in themes:
            theme_sums[theme] = theme_sums.get(theme, 0.0) + split

    total_classified = sum(theme_sums.values())
    if total_classified > 0:
        theme_weights = {k: v / (total_classified + unclassified) for k, v in theme_sums.items()}
    else:
        theme_weights = {}

    dominant = None
    if theme_weights:
        dominant = max(theme_weights, key=theme_weights.get)

    # HHI w przestrzeni tematów + unclassified bucket (żeby unclassified liczyło się też).
    all_weights = list(theme_weights.values())
    if total_classified + unclassified > 0:
        all_weights.append(unclassified / (total_classified + unclassified))
    hhi = sum(w * w for w in all_weights) if all_weights else 0.0
    uncl_frac = (
        unclassified / (total_classified + unclassified)
        if (total_classified + unclassified) > 0
        else 0.0
    )

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
    """Agregaty koncentracji tematów przez całe okno backtestu."""

    all_themes: tuple[str, ...]
    mean_weights: Mapping[str, float]  # średnia waga per theme przez okno
    days_dominant: Mapping[str, int]  # ile dni dany theme był dominujący
    mean_hhi: float  # średnia HHI przez okno
    concentration_alert_days: int  # dni gdzie max theme > threshold
    concentration_threshold: float


def theme_series(
    snapshots: Iterable[ThemeSnapshot],
    concentration_threshold: float = 0.70,
) -> tuple[pd.DataFrame, ThemeSeriesStats]:
    """Zbierz listę snapshotów dziennych w DataFrame (date × theme → weight).

    Zwraca dwa obiekty:
    - DataFrame z kolumnami per theme (plus 'unclassified', 'hhi')
    - `ThemeSeriesStats` z agregatami
    """
    rows: list[dict] = []
    for snap in snapshots:
        row = {"date": snap.date, **snap.theme_weights}
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

    # Theme columns = wszystko poza metadata
    meta = {"unclassified", "hhi", "dominant_theme"}
    theme_cols = tuple(c for c in df.columns if c not in meta)

    mean_weights = {c: float(df[c].fillna(0.0).mean()) for c in theme_cols}
    days_dominant = (
        df["dominant_theme"].value_counts().to_dict() if "dominant_theme" in df.columns else {}
    )
    # Odrzuć pusty string (dni bez dominanty)
    days_dominant = {k: int(v) for k, v in days_dominant.items() if k}

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
    """Ludzko-czytelny raport dla markdown sekcji."""
    lines = []
    lines.append(
        f"Okno backtestu: {n_total_days} dni, próg koncentracji = {stats.concentration_threshold * 100:.0f}%"
    )
    lines.append(f"Średni HHI: {stats.mean_hhi:.3f} (0 = idealna dywersyfikacja, 1 = jeden theme)")
    lines.append(
        f"Dni z max theme > {stats.concentration_threshold * 100:.0f}%: "
        f"{stats.concentration_alert_days} / {n_total_days} "
        f"({stats.concentration_alert_days / max(n_total_days, 1) * 100:.1f}%)"
    )
    lines.append("")
    lines.append("| Theme | Średnia waga | Dni dominujący |")
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
    daily_results: Iterable,
    themes_map: Mapping[str, list[str]],
) -> list[ThemeSnapshot]:
    """Convenience: zbuduj snapshots z `BacktestReport.daily_results`.

    Używa score-equal-weight (nie honoruje linear/conviction). Dla dokładnego
    weighted theme breakdown dodaj `position_weights` per snap recznie.
    """
    out = []
    for r in daily_results:
        out.append(
            snapshot_themes(
                top_n_tickers=r.top_n_tickers,
                themes_map=themes_map,
                date=r.date,
            )
        )
    return out
