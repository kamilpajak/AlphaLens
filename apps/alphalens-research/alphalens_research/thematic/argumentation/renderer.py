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

import math
from typing import Any

import pandas as pd

from alphalens_research.thematic.argumentation._common import (
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

# PROVISIONAL pattern thresholds (per 2026-05-17 NVDA→QUBT post-mortem,
# N=4 cohort only): deep_drawdown candidates (off ≥ 30% from 52w high)
# drove the 4-week winners in the live cohort (QUBT +44.6%, QBTS +30.4%,
# RGTI +14.2%); extended candidates (at 52w high + parabolic MA200)
# flat-lined or faded (FORM -0.2%). These thresholds are empirical
# observations from a single cohort, NOT structurally-locked rules —
# revise as more live data accumulates (see feedback ledger).
_DEEP_DRAWDOWN_PCT_OFF_HIGH = -30.0
_EXTENDED_PCT_OFF_HIGH = -5.0
_EXTENDED_MA200_DISTANCE_PCT = 50.0


def classify_setup_pattern(row: dict | pd.Series) -> str:
    """Classify the technical setup as ``deep_drawdown`` / ``extended`` /
    ``neutral`` / ``unknown``.

    ``unknown`` when the row lacks the 52w / MA200 inputs (typically
    short OHLCV history). The operator can spot ``deep_drawdown`` at a
    glance as the mean-reversion bucket worth fundamental + insider
    corroboration; ``extended`` is the already-rallied bucket where
    further upside requires a fresh catalyst beyond the original one.
    """
    r = dict(row) if not isinstance(row, dict) else row
    pct_off_high = r.get("technical_pct_off_52w_high")
    ma200_dist = r.get("technical_ma200_distance_pct")
    if pd.isna(pct_off_high) or pd.isna(ma200_dist):
        return "unknown"
    if pct_off_high <= _DEEP_DRAWDOWN_PCT_OFF_HIGH:
        return "deep_drawdown"
    if pct_off_high >= _EXTENDED_PCT_OFF_HIGH and ma200_dist >= _EXTENDED_MA200_DISTANCE_PCT:
        return "extended"
    return "neutral"


_PATTERN_LABEL = {
    "deep_drawdown": "deep drawdown",
    "extended": "extended",
    "neutral": "neutral",
}


def _format_pattern_line(row: dict | pd.Series) -> str:
    """One-line **Pattern** descriptor for the markdown brief.

    Returns "" when the underlying inputs are missing — we'd rather
    skip the line than print a useless "Pattern: unknown".
    """
    pattern = classify_setup_pattern(row)
    if pattern == "unknown":
        return ""
    pct_off = row.get("technical_pct_off_52w_high")
    ma200_dist = row.get("technical_ma200_distance_pct")
    slope = row.get("technical_ma200_slope_pct_per_day")
    label = _PATTERN_LABEL[pattern]
    pieces = [f"{label}"]
    if not pd.isna(pct_off):
        pieces.append(f"{pct_off:.0f}% off 52w high")
    if not pd.isna(ma200_dist):
        slope_str = ""
        if not pd.isna(slope):
            sign = "+" if slope >= 0 else ""
            slope_str = f", slope {sign}{slope:.2f}%/d"
        pieces.append(f"MA200 {'+' if ma200_dist >= 0 else ''}{ma200_dist:.0f}%{slope_str}")
    return f"**Pattern**: {' · '.join(pieces)}\n"


def _fmt_num(value: Any, fmt: str) -> str:
    if pd.isna(value):
        return "n/a"
    try:
        rendered = format(float(value), fmt)
    except (TypeError, ValueError):
        return "n/a"
    # IEEE-754 negative zero survives ``f`` / ``g`` formatting as a
    # leading minus (issue #172 Bug 3a: SOUN ROE -3e-6% rendered as
    # ``-0.0``). When the rendered string evaluates to zero, strip the
    # leading minus so the brief shows ``0.0`` not ``-0.0``.
    if rendered.startswith("-"):
        try:
            # NOSONAR (S1244): IEEE-754 negative-zero detection requires
            # exact float equality — `-0.0 == 0.0` is True by spec and is
            # precisely the property we test (issue #172 Bug 3a).
            if float(rendered) == 0.0:  # NOSONAR
                return rendered[1:]
        except ValueError:
            pass
    return rendered


def _fmt_pctile(value: Any) -> str:
    return _fmt_num(value, ".0f")


def _fmt_insider_usd(value: Any) -> str:
    if pd.isna(value):
        return "n/a"
    try:
        return f"${float(value) / 1000:.0f}k"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_insider_usd_compact(value: Any) -> str | None:
    """Header-friendly compact form: ``$1.2M``, ``$250k``, or None for 0/NaN.

    Returns None when there's no positive opportunistic buy to surface so
    the header can omit the chip cleanly rather than rendering noise.
    """
    if pd.isna(value):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    return f"${v / 1000:.0f}k"


def _format_insider_cell(score: Any, pctile: Any) -> str:
    """Render the insider table cell with score-aware suffix.

    Three cases (bug 6, 2026-05-18 audit):
    - ``score`` is NaN/None → ``n/a (pctile n/a)`` — Form-4 data missing.
    - ``score`` is zero → ``$0k (no opportunistic buys)`` — window had
      no qualifying buys. The mathematical pctile (often 96-100 when the
      cohort is dominated by tied zeros) reads as a positive signal in
      the UI; the descriptor is more honest.
    - ``score`` is positive → ``$Xk (pctile Y)`` — meaningful ranking.
    """
    score_str = _fmt_insider_usd(score)
    if score_str == "n/a":
        return f"{score_str} (pctile {_fmt_pctile(pctile)})"
    try:
        is_zero = math.isclose(float(score), 0.0, abs_tol=1e-9)
    except (TypeError, ValueError):
        is_zero = False
    if is_zero:
        return f"{score_str} (no opportunistic buys)"
    return f"{score_str} (pctile {_fmt_pctile(pctile)})"


def _format_magic_formula_cell(
    rank: Any, cohort_n: Any, health_pass: Any, fcff_yield_pct: Any, roic_pct: Any
) -> str:
    """Render the Magic Formula table cell.

    Three cases:
    - health_pass is False → ``health-gate fail`` (EBIT≤0 or net_debt/EBIT≥5)
    - rank is NaN AND health passed → ``rank n/a (cohort n=N)`` (small-cohort
      guard: fewer than 3 survivors means rank-sum carries no information)
    - rank is integer → ``rank R/N · FCFF X.X% · ROIC Y.Y%``
    """
    # Short-circuit on pd.isna first — bool(pd.NA) raises TypeError, so
    # checking NA-ness before truthiness avoids exception-driven control flow.
    try:
        passed = not pd.isna(health_pass) and bool(health_pass)
    except (TypeError, ValueError):
        passed = False
    if not passed:
        return "health-gate fail"
    if pd.isna(rank):
        try:
            n = int(cohort_n) if not pd.isna(cohort_n) else 0
        except (TypeError, ValueError):
            n = 0
        return f"rank n/a (cohort n={n})"
    try:
        rank_int = int(rank)
        cohort_int = int(cohort_n)
    except (TypeError, ValueError):
        return "n/a"
    fcff_str = _fmt_num(fcff_yield_pct, ".1f")
    roic_str = _fmt_num(roic_pct, ".1f")
    return f"rank {rank_int}/{cohort_int} · FCFF {fcff_str}% · ROIC {roic_str}%"


def _format_catalyst_strength_cell(strength: Any, event_type: Any) -> str:
    """Render catalyst strength as ``0.78 strong (product_launch)`` etc.

    Bucket labels mirror the catalyst_floor thresholds: ≥0.70 strong (+2),
    ≥0.25 moderate (+1), else weak (+0). Operator sees which catalysts
    earn cohort lift vs which don't.
    """
    s = _fmt_num(strength, ".2f")
    if s == "n/a":
        return "n/a"
    try:
        f = float(strength)
    except (TypeError, ValueError):
        return "n/a"
    if f >= 0.70:
        bucket = "strong"
    elif f >= 0.25:
        bucket = "moderate"
    else:
        bucket = "weak"
    et = str(event_type) if event_type and not pd.isna(event_type) else "?"
    return f"{s} {bucket} ({et})"


def _format_reversal_cell(value: Any) -> str:
    """Yes/no flag for deep_drawdown_reversal."""
    if pd.isna(value):
        return "n/a"
    return "yes" if bool(value) else "no"


def _format_magic_formula_detail(pe: Any, ev_ebitda: Any, ps: Any, roe_pct: Any) -> str:
    """Render the secondary detail row exposing the underlying mults."""
    return (
        f"PE {_fmt_num(pe, '.1f')}"
        f" · EV/EBITDA {_fmt_num(ev_ebitda, '.1f')}"
        f" · PS {_fmt_num(ps, '.1f')}"
        f" · ROE {_fmt_num(roe_pct, '.1f')}%"
    )


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


def _rank_chip(rank: Any, cohort_size: Any) -> str | None:
    if rank is None or pd.isna(rank):
        return None
    try:
        n = int(cohort_size) if cohort_size is not None and not pd.isna(cohort_size) else 0
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return f"rank {int(rank)}/{n}"


def _catalyst_chip(catalyst: Any) -> str | None:
    if catalyst is None or pd.isna(catalyst):
        return None
    try:
        cf = float(catalyst)
    except (TypeError, ValueError):
        return None
    return f"catalyst {cf:.2f}" if cf > 0 else None


def _reversal_chip(reversal: Any) -> str | None:
    try:
        if reversal is not None and not pd.isna(reversal) and bool(reversal):
            return "reversal"
    except (TypeError, ValueError):
        pass
    return None


def _build_header_chips(r: dict, weighted_str: str) -> str:
    """Header chips — only render keys that carry signal.

    Built dynamically so single-theme tickers / weak catalysts / zero insider
    all hide their chips cleanly instead of rendering noise like ``catalyst n/a``.
    """
    chips: list[str] = []
    chip = _rank_chip(r.get("rank_in_day"), r.get("cohort_size_in_day"))
    if chip:
        chips.append(chip)
    chips.append(f"conf {weighted_str}")
    chip = _catalyst_chip(r.get("catalyst_strength"))
    if chip:
        chips.append(chip)
    insider_chip = _fmt_insider_usd_compact(r.get("insider_score_usd"))
    if insider_chip:
        chips.append(f"{insider_chip} insider")
    chip = _reversal_chip(r.get("deep_drawdown_reversal"))
    if chip:
        chips.append(chip)
    return " · ".join(chips)


def _theme_line(r: dict) -> str:
    """Compose ``**Theme**: ... | **Industry**: ...`` line with multi-theme badge."""
    line = (
        f"**Theme**: {r.get('theme', '')} | "
        f"**Industry**: {r.get('industry_name', 'n/a')}"
        f" ({r.get('sector_name', 'n/a')})"
    )
    # Multi-theme badge: surface OTHER themes the ticker hit when sort+dedup
    # collapsed them. Operator sees the multi-thematic signal without us
    # spawning duplicate brief blocks.
    also = r.get("also_in_themes")
    if also is None:
        return line
    try:
        also_list = [t for t in also if t]
    except TypeError:
        return line
    if also_list:
        line += f" | **also in**: {', '.join(also_list)}"
    return line


def _catalyst_line(r: dict) -> str | None:
    """``**Catalyst**: ...`` line, or None when source URL is missing/NaN."""
    src_url = r.get("source_event_url")
    if not (src_url and pd.notna(src_url) and str(src_url).strip().lower() != "nan"):
        return None
    title = r.get("source_event_title") or ""
    published = r.get("source_event_published_at") or ""
    return f"**Catalyst**: {title} ({published}) {src_url}"


def _head_sections(r: dict, chip_str: str) -> list[str]:
    """Deterministic head sections: ticker, theme, catalyst, pattern.

    The head bucket carries scan-cues the operator's eye should land on
    one at a time. Blank lines between them mirror the bullet structure
    recommended in PR 2026-05-17 brief-layout pass.
    """
    sections: list[str] = [
        f"## {r.get('ticker')} — {r.get('company_name', '')} ({chip_str})",
        _theme_line(r),
    ]
    catalyst = _catalyst_line(r)
    if catalyst:
        sections.append(catalyst)
    pattern_line = _format_pattern_line(r).rstrip()
    if pattern_line:
        sections.append(pattern_line)
    return sections


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

    chip_str = _build_header_chips(r, weighted_str)
    head = "\n\n".join(_head_sections(r, chip_str)) + "\n\n"

    # --- LLM-composed prose with placeholders ------------------------------
    prose = {k: _prose_or_placeholder(b.get(k)) for k in _PROSE_FIELDS}

    # --- Deterministic signal panel (markdown table) -----------------------
    # 2-col table — operator can scan label↔value vertically instead of
    # parsing a long inline bar. Each row is independent so missing
    # signals stay hidden rather than rendering empty cells.
    signal_rows: list[tuple[str, str]] = [
        (
            "Insider 90d opportunistic",
            _format_insider_cell(
                r.get("insider_score_usd"),
                r.get("insider_score_sector_percentile"),
            ),
        ),
        (
            "FCFF yield",
            f"{_fmt_num(r.get('fcff_yield_pct'), '.1f')}%"
            f" (pctile {_fmt_pctile(r.get('fcff_yield_sector_percentile'))})",
        ),
        (
            "Magic Formula",
            _format_magic_formula_cell(
                r.get("magic_formula_rank"),
                r.get("magic_formula_cohort_n"),
                r.get("magic_formula_health_pass"),
                r.get("fcff_yield_pct"),
                r.get("roic_pct"),
            ),
        ),
        (
            "Mults & ROE",
            _format_magic_formula_detail(
                r.get("valuation_pe"),
                r.get("valuation_ev_ebitda"),
                r.get("valuation_ps"),
                r.get("roe_pct"),
            ),
        ),
        (
            "Valuation (sector pctile)",
            f"pctile {_fmt_pctile(r.get('valuation_composite_sector_percentile'))}",
        ),
        (
            "Catalyst strength",
            _format_catalyst_strength_cell(
                r.get("catalyst_strength"), r.get("catalyst_event_type")
            ),
        ),
        (
            "Reversal setup",
            _format_reversal_cell(r.get("deep_drawdown_reversal")),
        ),
    ]
    age_days = r.get("valuation_financials_age_days")
    if not pd.isna(age_days):
        signal_rows.append(("Financials age", f"{int(age_days)}d"))
    signal_rows.append(("Technicals", str(r.get("technicals_summary_str", "n/a"))))
    next_earnings = b.get("next_earnings_date") or r.get("next_earnings_date")
    if next_earnings:
        signal_rows.append(("Next earnings", str(next_earnings)))
    signal_rows.append(("Verified gates", str(r.get("gates_passed_str", ""))))

    signal_table_lines = ["| Signal | Value |", "|---|---|"]
    signal_table_lines.extend(f"| {label} | {value} |" for label, value in signal_rows)
    signal_panel = "\n".join(signal_table_lines) + "\n"

    # --- Setup line: position size + exit are deterministic; entry from LLM
    setup_line = (
        f"**Setup**: entry {prose['entry_price_note']}"
        f" | size {_fmt_num(position_pct_from_conf(weighted), '.1f')}%"
        f" | exit {TIME_EXIT_DEFAULT_WEEKS}w | stop {DISASTER_STOP_PCT:.0f}%\n\n"
    )

    block = (
        f"{head}"
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
    """Concatenate one day's briefs into a single markdown file body.

    Preserves upstream input order — sort responsibility moved to
    ``orchestrator._sort_and_dedup_for_brief`` which runs the full
    7-key zen-revised tiebreaker chain before this renderer ever sees
    the frame. The legacy cherry-pick sort (technical_pct_off_52w_high
    ASC) is gone; that signal now feeds the upstream chain via the
    ``deep_drawdown_reversal`` flag.
    """
    header = f"# Thematic briefs — {asof_str}\n\n"
    if briefs_df is None or briefs_df.empty:
        return header + "_no briefs generated for this date._\n"
    parts: list[str] = [header]
    for _, row in briefs_df.iterrows():
        md = row.get("brief_full_md", "")
        if md:
            parts.append(md.rstrip() + "\n\n---\n\n")
    return "".join(parts).rstrip() + "\n"


__all__ = ["classify_setup_pattern", "render_day_bundle", "render_markdown"]
