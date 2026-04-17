"""Format momentum pipeline output for Telegram delivery."""

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


def format_telegram_report(df: pd.DataFrame, curr_date: str) -> str:
    """Render a compact Markdown report for Telegram. Safe on empty frames."""
    header = f"📈 *Momentum Report — {curr_date}*"

    if df.empty:
        return f"{header}\n\n_No momentum candidates passed guardrails + scoring._"

    lines = [header, ""]
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
