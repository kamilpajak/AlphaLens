"""Markdown rendering for Phase E briefs.

Pure functions — no LLM, no I/O. Renders a single ~700-1000 char brief
block per candidate and a day-bundle joiner that concatenates all of a
day's briefs into one ``.md`` file the operator can ``cat`` and forward.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd


def _fmt_num(value: Any, fmt: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    try:
        return format(float(value), fmt)
    except (TypeError, ValueError):
        return "n/a"


def _fmt_pctile(value: Any) -> str:
    return _fmt_num(value, ".0f")


def _fmt_insider_usd(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    try:
        return f"${float(value) / 1000:.0f}k"
    except (TypeError, ValueError):
        return "n/a"


def render_markdown(brief: dict, row: dict | pd.Series) -> str:
    """Assemble one candidate's brief block (~700-1000 chars)."""
    r = dict(row) if not isinstance(row, dict) else row
    weighted = r.get("layer4_weighted_score")
    weighted_str = f"{int(weighted)}/5" if weighted is not None and not _is_nan(weighted) else "n/a"

    return (
        f"## {r.get('ticker')} — {r.get('company_name', '')} (conf {weighted_str})\n"
        f"**Theme**: {r.get('theme', '')} | "
        f"**Industry**: {r.get('industry_name', 'n/a')}"
        f" ({r.get('sector_name', 'n/a')})\n\n"
        f"**Thesis**: {brief.get('tldr', '')}\n\n"
        f"**Supply chain**: {brief.get('supply_chain_reasoning', '')}\n\n"
        f"**Bear case**: {brief.get('bear_summary', '')}\n\n"
        f"**Setup**: entry {brief.get('entry_price_note', '')}"
        f" | size {_fmt_num(_position_pct_from_conf(weighted), '.1f')}%"
        f" | exit 8w | stop -25%\n"
        f"**Catalyst-failure exit**: {brief.get('catalyst_failure_exit', '')}\n\n"
        f"**Signals**: insider {_fmt_insider_usd(r.get('insider_score_usd'))}"
        f" (pctile {_fmt_pctile(r.get('insider_score_sector_percentile'))})"
        f" | FCFF {_fmt_num(r.get('fcff_yield_pct'), '.1f')}%"
        f" (pctile {_fmt_pctile(r.get('fcff_yield_sector_percentile'))})"
        f" | val composite pctile"
        f" {_fmt_pctile(r.get('valuation_composite_sector_percentile'))}"
        f" | {r.get('technicals_summary_str', 'n/a')}\n"
        f"**Verified gates**: {r.get('gates_passed_str', '')}\n"
    )


def _is_nan(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)


def _position_pct_from_conf(weighted_score: Any) -> float:
    """Per memo §2: 1.5% (conf 3), 2.0% (conf 4), 2.5% (conf 5); 1.0% for low."""
    if weighted_score is None or _is_nan(weighted_score):
        return 1.0
    try:
        ws = int(weighted_score)
    except (TypeError, ValueError):
        return 1.0
    if ws >= 5:
        return 2.5
    if ws == 4:
        return 2.0
    if ws == 3:
        return 1.5
    return 1.0


def render_day_bundle(briefs_df: pd.DataFrame, *, asof_str: str) -> str:
    """Concatenate one day's briefs into a single markdown file body."""
    header = f"# Thematic briefs — {asof_str}\n\n"
    if briefs_df is None or briefs_df.empty:
        return header + "_no briefs generated for this date._\n"
    parts: list[str] = [header]
    for _, row in briefs_df.iterrows():
        md = row.get("brief_full_md", "")
        if md:
            parts.append(md.rstrip() + "\n\n---\n\n")
    return "".join(parts).rstrip() + "\n"


__all__ = ["render_day_bundle", "render_markdown"]
