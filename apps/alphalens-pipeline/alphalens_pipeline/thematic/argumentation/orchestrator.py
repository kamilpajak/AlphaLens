"""Phase E orchestrator — loop Phase D-scored candidates, emit briefs.

Reads a Phase D-scored DataFrame, filters to ``verified=True`` rows,
hoists one Pro client + one Flash client, calls
``generator.generate_brief`` per row (routing handled there), assembles
the enriched DataFrame, and writes parquet + markdown bundle to
``output_dir``. Per-row exceptions are absorbed so one bad LLM call
doesn't abort the batch (mirrors Phase D ``_safe_signal`` pattern).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path

import pandas as pd

from alphalens_pipeline.thematic.argumentation import generator
from alphalens_pipeline.thematic.argumentation._common import (
    DISASTER_STOP_PCT,
    TIME_EXIT_DEFAULT_WEEKS,
    TIME_EXIT_ON_CATALYST_FAILURE_WEEKS,
    position_pct_from_conf,
)
from alphalens_pipeline.thematic.argumentation.renderer import render_day_bundle, render_markdown

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path.home() / ".alphalens" / "thematic_briefs"

_BRIEF_NUMERIC_FIELDS = (
    "insider_score_usd",
    "insider_score_sector_percentile",
    "fcff_yield_pct",
    "fcff_yield_sector_percentile",
    "valuation_ps",
    "valuation_ev_rev",
    "valuation_fcf_margin",
    "valuation_composite_sector_percentile",
    "valuation_financials_age_days",
    "market_cap",
    "technical_pct_off_52w_high",
    "technical_pct_off_52w_low",
    "technical_ma200_distance_pct",
    "technical_ma200_slope_pct_per_day",
)


def _row_to_facts(row: pd.Series) -> dict:
    """Project Phase D row → flat facts dict for the prompt template."""
    weighted = row.get("layer4_weighted_score")
    facts = {
        "ticker": str(row["ticker"]),
        "company_name": row.get("company_name", ""),
        "theme": row.get("theme", ""),
        "industry_name": row.get("industry_name") or "n/a",
        "sector_name": row.get("sector_name") or "n/a",
        "weighted_score": int(weighted) if weighted is not None and not pd.isna(weighted) else 1,
        "rationale": row.get("rationale", ""),
        "gates_passed_str": row.get("gates_passed_str", ""),
        "technicals_summary_str": row.get("technicals_summary_str", "n/a"),
        "position_pct": position_pct_from_conf(weighted),
        "time_exit_weeks": TIME_EXIT_DEFAULT_WEEKS,
        # Catalyst / news provenance (Z6 — explains "why surfaced").
        "source_event_url": row.get("source_event_url") or None,
        "source_event_title": row.get("source_event_title") or None,
        "source_event_published_at": row.get("source_event_published_at") or None,
        # Freshness telemetry (Z2).
        "financials_publish_date": row.get("valuation_financials_publish_date") or None,
        # Earnings calendar lookup (Z3) — done at orchestrator level so the
        # row_to_facts doesn't need to know about yfinance.
        "next_earnings_date": None,
    }
    for field in _BRIEF_NUMERIC_FIELDS:
        value = row.get(field)
        facts[field] = None if value is None or pd.isna(value) else float(value)
    return facts


def _enrich_facts_with_earnings(facts: dict, asof: dt.date) -> dict:
    """Add next_earnings_date to facts via yfinance.calendar lookup (PIT)."""
    from alphalens_pipeline.thematic.sources.earnings_calendar import fetch_next_earnings

    try:
        next_date = fetch_next_earnings(ticker=facts["ticker"], asof=asof)
    except Exception:
        next_date = None
    facts["next_earnings_date"] = next_date.isoformat() if next_date else None
    return facts


def _brief_for_row(
    row: pd.Series,
    *,
    gemini_client_pro,
    gemini_client_flash,
    asof: dt.date | None = None,
) -> tuple[dict | None, str | None]:
    """Single-row LLM call with per-row exception absorption.

    Returns ``(brief_dict, next_earnings_date_iso)``. The earnings date is
    surfaced separately so the orchestrator can persist it to the brief
    parquet AND pass it to the renderer — without this split it was only
    reaching the LLM prompt and getting dropped before reaching the
    operator (bug 2026-05-18: next_earnings_date column was always None).

    Uses ``generator.generate_brief_with_retry`` so a Flash truncation
    (``finish_reason == MAX_TOKENS``) auto-retries once with double
    ``max_output_tokens`` + ``temperature=0`` before giving up. Other
    failure kinds (MALFORMED_JSON, SAFETY, TRANSPORT) do not retry.
    """
    facts = _row_to_facts(row)
    if asof is not None:
        facts = _enrich_facts_with_earnings(facts, asof)
    next_earnings = facts.get("next_earnings_date")
    try:
        brief = generator.generate_brief_with_retry(
            facts,
            gemini_client_pro=gemini_client_pro,
            gemini_client_flash=gemini_client_flash,
        )
    except Exception as exc:
        logger.warning("brief generation raised for %s: %s", row.get("ticker"), exc, exc_info=True)
        brief = None
    return brief, next_earnings


def _build_clients(api_key: str | None):
    """Hoist one shared GeminiClient (Pro + Flash share it). Returns
    ``(pro_client, flash_client)`` or ``(None, None)`` when no key is
    available so the orchestrator can still write placeholder rows
    (used by tests that patch ``_brief_for_row`` wholesale)."""
    from alphalens_pipeline.data.alt_data.gemini_client import (
        GeminiClient,
        get_default_gemini_client,
    )

    key = api_key or os.environ.get("GOOGLE_API_KEY") or ""
    if not key:
        return None, None
    try:
        client = GeminiClient(api_key=key) if api_key else get_default_gemini_client()
    except RuntimeError as exc:
        logger.warning("google-genai SDK missing; cannot generate briefs: %s", exc)
        return None, None
    return client, client  # Same client serves both Pro and Flash models.


_EMPTY_OUT_COLUMNS = (
    "theme",
    "ticker",
    "verified",
    "next_earnings_date",
    "brief_model_used",
    "brief_tldr",
    "brief_supply_chain_md",
    "brief_bear_summary_md",
    "brief_catalyst_failure_exit",
    "brief_entry_price_note",
    "brief_position_pct",
    "brief_time_exit_weeks",
    "brief_time_exit_on_catalyst_failure_weeks",
    "brief_disaster_stop_pct",
    "brief_full_md",
    "brief_generated_at",
)


def _write_sidecar(output_dir: Path, asof: dt.date, n_pro: int, n_flash: int) -> None:
    """Persist per-model counts to a JSON sidecar (parquet drops df.attrs)."""
    sidecar = output_dir / f"{asof.isoformat()}.meta.json"
    sidecar.write_text(
        json.dumps(
            {"asof": asof.isoformat(), "n_pro": n_pro, "n_flash": n_flash},
            indent=2,
        )
    )


# Sort priorities for the brief-render chain (zen-revised 2026-05-18).
# Primary on layer4_weighted_score; continuous tiebreakers (catalyst_strength,
# insider_score_usd) precede the binary deep_drawdown_reversal flag because
# a strong continuous catalyst is structurally safer than a weak catalyst
# with an oversold setup. Magic Formula rank is ASCENDING (1 = best); all
# other keys are DESCENDING. Neutral defaults backfill missing columns so
# older parquets and partial enrichments don't crash the sort.
_BRIEF_SORT_KEYS: tuple[tuple[str, bool, float | int | bool], ...] = (
    ("layer4_weighted_score", False, 0.0),
    ("catalyst_strength", False, 0.0),
    ("insider_score_usd", False, 0.0),
    ("deep_drawdown_reversal", False, False),
    # ascending=True for magic_formula_rank (1 = best); float("inf") so any
    # missing rank sorts to the absolute bottom rather than the top.
    ("magic_formula_rank", True, float("inf")),
    ("n_gates_passed", False, 0),
    ("gemini_confidence", False, 0.0),
)


def _sort_and_dedup_for_brief(verified: pd.DataFrame) -> pd.DataFrame:
    """Sort by the zen-revised 7-key chain, attach also_in_themes, dedup.

    Returns the sorted, deduped DataFrame with two new columns:
    - ``also_in_themes``: list[str] of OTHER themes the ticker hit (empty
      list for single-theme tickers); operator sees the multi-thematic
      signal even though we collapse to one row per ticker.
    - ``rank_in_day``: 1-based position after dedup so the renderer can
      surface ``rank N/M`` in the header.

    Sort order encoded in ``_BRIEF_SORT_KEYS``. Missing columns are
    backfilled with neutral defaults — older Phase D parquets and partial
    enrichments don't crash the sort. Dedup happens AFTER sort so the
    strongest-context row per ticker survives.
    """
    if verified.empty:
        return verified

    # Build TEMP sort columns alongside the originals so the synthetic
    # fillna defaults (e.g. ``float("inf")`` for missing magic_formula_rank)
    # never leak into the returned frame. Downstream renderer must see the
    # original NaN values — otherwise ``int(rank)`` crashes with
    # OverflowError (empirical 2026-05-18 incident on first dogfooding).
    work = verified.copy()
    sort_keys: list[str] = []
    ascending: list[bool] = []
    for col, asc, default in _BRIEF_SORT_KEYS:
        tmp = f"__sort_key__{col}"
        if col in work.columns:
            work[tmp] = work[col].fillna(default)
        else:
            work[tmp] = default
        sort_keys.append(tmp)
        ascending.append(asc)

    work = (
        work.sort_values(sort_keys, ascending=ascending, kind="mergesort")
        .drop(columns=sort_keys)
        .reset_index(drop=True)
    )

    # Collect cross-theme appearances BEFORE dedup so the kept row carries
    # the badge. Group ticker → themes; subtract the kept row's own theme.
    theme_groups: dict[str, list[str]] = {}
    if "theme" in work.columns:
        for ticker, group in work.groupby("ticker", sort=False)["theme"]:
            theme_groups[ticker] = list(group)

    deduped = work.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)

    def _others(row: pd.Series) -> list[str]:
        all_themes = theme_groups.get(row["ticker"], [])
        own = row.get("theme")
        # ``dict.fromkeys`` dedupes case where a (ticker, theme) pair is
        # repeated upstream — keeps the badge "also in: AI_models,
        # quantum_computing" instead of "also in: AI_models, AI_models,
        # quantum_computing" (zen pre-merge LOW finding).
        return list(dict.fromkeys(t for t in all_themes if t != own))

    deduped["also_in_themes"] = deduped.apply(_others, axis=1)
    deduped["rank_in_day"] = range(1, len(deduped) + 1)
    deduped["cohort_size_in_day"] = len(deduped)
    return deduped


def _empty_output(output_dir: Path, asof: dt.date) -> pd.DataFrame:
    """Write a typed-empty parquet + empty bundle + zero-counts sidecar."""
    empty = pd.DataFrame({c: pd.Series(dtype="object") for c in _EMPTY_OUT_COLUMNS})
    empty.to_parquet(output_dir / f"{asof.isoformat()}.parquet", index=False)
    (output_dir / f"{asof.isoformat()}.md").write_text(
        render_day_bundle(empty, asof_str=asof.isoformat())
    )
    _write_sidecar(output_dir, asof, n_pro=0, n_flash=0)
    return empty


def generate_briefs(
    scored: pd.DataFrame,
    *,
    asof: dt.date,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    api_key: str | None = None,
) -> pd.DataFrame:
    """Enrich Phase D-scored candidates with composed briefs; persist."""
    output_dir.mkdir(parents=True, exist_ok=True)

    if scored is None or scored.empty:
        return _empty_output(output_dir, asof)

    verified = scored[scored["verified"].astype(bool)].copy().reset_index(drop=True)
    if verified.empty:
        return _empty_output(output_dir, asof)
    # Sort by the brief-render chain BEFORE dedup so the strongest-context
    # row per ticker survives. Without sort-first, a ticker hitting multiple
    # themes with different catalysts could lose the strong-catalyst row to
    # the weak one on index-order fallback (zen pre-design HIGH finding).
    verified = _sort_and_dedup_for_brief(verified)

    client_pro, client_flash = _build_clients(api_key)

    rows: list[dict] = []
    n_pro = 0
    n_flash = 0
    for _, row in verified.iterrows():
        brief, next_earnings = _brief_for_row(
            row,
            gemini_client_pro=client_pro,
            gemini_client_flash=client_flash,
            asof=asof,
        )
        if brief is not None:
            if brief.get("model_used") == generator.PRO_MODEL:
                n_pro += 1
            else:
                n_flash += 1
        # Inject next_earnings_date into the row copy so the renderer's
        # `r.get("next_earnings_date")` path picks it up (single source of
        # truth — same field is also persisted to parquet below).
        row_with_earnings = row.copy()
        row_with_earnings["next_earnings_date"] = next_earnings
        # Graceful degradation: render always — when brief is None the
        # renderer emits per-section placeholders so the operator never
        # loses the deterministic catalyst + signal panel (Perplexity
        # 2026-05-17 recommendation; QUBT 2026-04-14 incident showed the
        # legacy "(brief unavailable)" fallback hid the winning signal).
        md = render_markdown(row_with_earnings, brief)
        b = brief or {}
        rows.append(
            {
                "ticker": row["ticker"],
                "next_earnings_date": next_earnings,
                "brief_model_used": b.get("model_used"),
                "brief_tldr": b.get("tldr"),
                "brief_supply_chain_md": b.get("supply_chain_reasoning"),
                "brief_bear_summary_md": b.get("bear_summary"),
                "brief_catalyst_failure_exit": b.get("catalyst_failure_exit"),
                "brief_entry_price_note": b.get("entry_price_note"),
                "brief_position_pct": position_pct_from_conf(row.get("layer4_weighted_score")),
                "brief_time_exit_weeks": TIME_EXIT_DEFAULT_WEEKS,
                "brief_time_exit_on_catalyst_failure_weeks": TIME_EXIT_ON_CATALYST_FAILURE_WEEKS,
                "brief_disaster_stop_pct": DISASTER_STOP_PCT,
                "brief_full_md": md,
                "brief_generated_at": pd.Timestamp.now(tz="UTC"),
            }
        )

    enrichment = pd.DataFrame(rows).drop_duplicates(subset=["ticker"], keep="first")
    merged = verified.merge(enrichment, on="ticker", how="left")
    merged.attrs["n_pro"] = n_pro
    merged.attrs["n_flash"] = n_flash
    out_path = output_dir / f"{asof.isoformat()}.parquet"
    merged.to_parquet(out_path, index=False)
    (output_dir / f"{asof.isoformat()}.md").write_text(
        render_day_bundle(merged, asof_str=asof.isoformat())
    )
    _write_sidecar(output_dir, asof, n_pro=n_pro, n_flash=n_flash)
    logger.info(
        "generate_briefs %s: wrote %d briefs (Pro=%d, Flash=%d) → %s",
        asof.isoformat(),
        len(merged),
        n_pro,
        n_flash,
        out_path,
    )
    return merged


__all__ = ["DEFAULT_OUTPUT_DIR", "generate_briefs"]
