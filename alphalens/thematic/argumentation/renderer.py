"""Markdown rendering for Phase E briefs.

Pure functions — no LLM, no I/O. ``render_markdown(row, brief)`` always
renders deterministic facts (ticker header, catalyst, numeric signal
panel, verified gates, setup line) from ``row``; LLM-composed prose
sections (Thesis / Supply chain / Bear case / Catalyst-failure exit /
entry note) come from ``brief`` and degrade to italic placeholders when
missing. This is the "graceful degradation" pattern recommended by
Perplexity 2026-05-17: a Flash truncation must NEVER cause the operator
to lose visibility on the deterministic data already computed by
Phase C/D.

``render_day_bundle`` concatenates the per-row markdown blocks into a
single ``.md`` file the operator can ``cat`` and forward.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from alphalens.thematic.argumentation._common import (
    DISASTER_STOP_PCT,
    TIME_EXIT_DEFAULT_WEEKS,
    position_pct_from_conf,
)

_PROSE_UNAVAILABLE = "_unavailable_"
_BRIEF_DEGRADED_NOTE = "> _LLM brief unavailable — review quantitative signals and catalyst above._"
_PROSE_FIELDS = (
    "tldr",
    "supply_chain_reasoning",
    "bear_summary",
    "catalyst_failure_exit",
    "entry_price_note",
)


def _fmt_num(value: Any, fmt: str) -> str:
    if pd.isna(value):
        return "n/a"
    try:
        return format(float(value), fmt)
    except (TypeError, ValueError):
        return "n/a"


def _fmt_pctile(value: Any) -> str:
    return _fmt_num(value, ".0f")


def _fmt_insider_usd(value: Any) -> str:
    if pd.isna(value):
        return "n/a"
    try:
        return f"${float(value) / 1000:.0f}k"
    except (TypeError, ValueError):
        return "n/a"


def _prose_or_placeholder(value: Any) -> str:
    """Return non-empty string value as-is; otherwise return the placeholder.

    Uses ``pd.isna`` so all pandas-flavoured null types (None / NaN / NaT /
    pd.NA) collapse to the placeholder; without this a NaT round-tripped
    via parquet would render as the literal string ``"NaT"`` (zen review
    2026-05-17 M1 finding).
    """
    if pd.isna(value):
        return _PROSE_UNAVAILABLE
    s = str(value).strip()
    return s if s else _PROSE_UNAVAILABLE


def render_markdown(row: dict | pd.Series, brief: dict | None = None) -> str:
    """Assemble one candidate's brief block.

    ``row`` is the Phase D scored record (dict or ``pd.Series``) and is
    the source of truth for deterministic facts — always rendered.
    ``brief`` is the Phase E LLM-composed prose dict (``None`` when
    generation failed); missing prose fields render italic placeholders
    so the block keeps its structure and the operator never loses
    visibility on the quantitative signals.
    """
    r = dict(row) if not isinstance(row, dict) else row
    b = brief or {}

    weighted = r.get("layer4_weighted_score")
    weighted_str = f"{int(weighted)}/5" if not pd.isna(weighted) else "n/a"

    # --- Deterministic header ----------------------------------------------
    header = (
        f"## {r.get('ticker')} — {r.get('company_name', '')} (conf {weighted_str})\n"
        f"**Theme**: {r.get('theme', '')} | "
        f"**Industry**: {r.get('industry_name', 'n/a')}"
        f" ({r.get('sector_name', 'n/a')})\n"
    )

    # --- Deterministic catalyst line ---------------------------------------
    catalyst_line = ""
    src_url = r.get("source_event_url")
    if src_url and pd.notna(src_url) and str(src_url).strip().lower() != "nan":
        title = r.get("source_event_title") or ""
        published = r.get("source_event_published_at") or ""
        catalyst_line = f"**Catalyst**: {title} ({published}) {src_url}\n"

    # --- LLM-composed prose with placeholders ------------------------------
    prose = {k: _prose_or_placeholder(b.get(k)) for k in _PROSE_FIELDS}

    # --- Deterministic signal panel ----------------------------------------
    age_days = r.get("valuation_financials_age_days")
    age_tag = f" | financials age {int(age_days)}d" if not pd.isna(age_days) else ""
    next_earnings = b.get("next_earnings_date") or r.get("next_earnings_date")
    earnings_tag = f" | next earnings {next_earnings}" if next_earnings else ""
    signal_panel = (
        f"**Signals**: insider {_fmt_insider_usd(r.get('insider_score_usd'))}"
        f" (pctile {_fmt_pctile(r.get('insider_score_sector_percentile'))})"
        f" | FCFF {_fmt_num(r.get('fcff_yield_pct'), '.1f')}%"
        f" (pctile {_fmt_pctile(r.get('fcff_yield_sector_percentile'))})"
        f" | val composite pctile"
        f" {_fmt_pctile(r.get('valuation_composite_sector_percentile'))}"
        f"{age_tag}"
        f" | {r.get('technicals_summary_str', 'n/a')}"
        f"{earnings_tag}\n"
        f"**Verified gates**: {r.get('gates_passed_str', '')}\n"
    )

    # --- Setup line: position size + exit are deterministic; entry from LLM
    setup_line = (
        f"**Setup**: entry {prose['entry_price_note']}"
        f" | size {_fmt_num(position_pct_from_conf(weighted), '.1f')}%"
        f" | exit {TIME_EXIT_DEFAULT_WEEKS}w | stop {DISASTER_STOP_PCT:.0f}%\n"
    )

    block = (
        f"{header}"
        f"{catalyst_line}\n"
        f"**Thesis**: {prose['tldr']}\n\n"
        f"**Supply chain**: {prose['supply_chain_reasoning']}\n\n"
        f"**Bear case**: {prose['bear_summary']}\n\n"
        f"{setup_line}"
        f"**Catalyst-failure exit**: {prose['catalyst_failure_exit']}\n\n"
        f"{signal_panel}"
    )

    # Single operator-facing note when the LLM contribution was empty. We
    # detect "everything empty" rather than "brief is None" so a partial
    # response (e.g., only `tldr` recovered via json-repair) doesn't emit
    # the global note. Iterating _PROSE_FIELDS keeps this in sync with
    # the per-section rendering if a 6th field is ever added.
    if all(v == _PROSE_UNAVAILABLE for v in prose.values()):
        block += f"\n{_BRIEF_DEGRADED_NOTE}\n"

    return block


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
