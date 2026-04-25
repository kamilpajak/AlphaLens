"""Format themed pipeline output for Telegram delivery."""

from __future__ import annotations

import pandas as pd

_METRIC_SHORT = {
    "near_high_score": "near-high",
    "pct_20d_score": "20d",
    "volume_surge_score": "vol",
    "rel_strength_score": "RS",
    "rsi_score": "RSI",
    "adx_score": "ADX",
    "macd_score": "MACD",
}

CONCENTRATION_WARNING_THRESHOLD = 0.70


def _theme_concentration(df: pd.DataFrame) -> tuple[dict[str, float], str | None, bool]:
    """Zlicz wagi per theme dla top-N picks (split na tickery z wieloma themes).

    Zwraca: (theme_weights dict, dominujący theme lub None, czy flag > threshold).
    """
    counts: dict[str, float] = {}
    total = 0.0
    for _, row in df.iterrows():
        themes = row.get("themes") or []
        if not isinstance(themes, list) or not themes:
            continue
        share = 1.0 / len(themes)
        for t in themes:
            counts[t] = counts.get(t, 0.0) + share
        total += 1.0
    if total == 0:
        return {}, None, False
    weights = {k: v / total for k, v in counts.items()}
    dominant = max(weights, key=weights.get) if weights else None
    flag = bool(dominant and weights[dominant] > CONCENTRATION_WARNING_THRESHOLD)
    return weights, dominant, flag


def format_telegram_report(df: pd.DataFrame, curr_date: str) -> str:
    """Render a compact Markdown report for Telegram. Safe on empty frames."""
    header = f"📈 *Momentum Report — {curr_date}*"

    if df.empty:
        return f"{header}\n\n_No momentum candidates passed guardrails + scoring._"

    # Factor-aware header: theme breakdown + alert if one theme dominates
    weights, dominant, concentration_flag = _theme_concentration(df)
    theme_line = None
    if weights:
        parts = [
            f"{theme}: {weight * 100:.0f}%"
            for theme, weight in sorted(weights.items(), key=lambda kv: -kv[1])
        ]
        theme_line = "_Themes: " + " · ".join(parts) + "_"

    lines = [header]
    if theme_line:
        lines.append(theme_line)
    if concentration_flag:
        lines.append(
            f"⚠️ _Koncentracja {dominant} > {int(CONCENTRATION_WARNING_THRESHOLD * 100)}% "
            f"— single-theme bet, nie diversified basket._"
        )
    lines.append("")

    for _, row in df.iterrows():
        ticker = row["ticker"]
        score = float(row["momentum_score"])
        themes = row.get("themes", [])
        themes_str = ", ".join(themes) if isinstance(themes, list) else str(themes)
        lines.append(f"*{ticker}* — score {score:.2f}  _({themes_str})_")

        breakdown = []
        for col, short in _METRIC_SHORT.items():
            if col in row and pd.notna(row[col]):
                breakdown.append(f"{short}={float(row[col]):.2f}")
        if breakdown:
            lines.append("  " + " · ".join(breakdown))
        lines.append("")

    return "\n".join(lines).rstrip()
