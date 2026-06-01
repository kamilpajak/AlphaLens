"""Golden projection for the brief stage (test-strategy Phase 3, L3).

The golden is NOT a full-row dump (snapshot rot + diff fatigue kill those — memo
§3/§8). It is schema + row-count + the tickers + a small STABLE per-row exemplar
(ticker, theme, model routed, whether a tldr came back, gate count). Volatile
fields (``brief_generated_at``) and the verbose LLM prose itself are excluded so
the golden churns only on a real behaviour change — which then renders as a
reviewable JSON diff.

Both the recorder (``scripts/record_golden_brief.py``) and the replay test use
this one function so the captured golden and the asserted projection cannot drift.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd


def brief_projection(brief: pd.DataFrame) -> dict[str, Any]:
    exemplar = [
        {
            "ticker": str(r["ticker"]),
            "theme": str(r.get("theme", "")),
            "brief_model_used": (
                None if pd.isna(r.get("brief_model_used")) else str(r.get("brief_model_used"))
            ),
            "has_tldr": bool(pd.notna(r.get("brief_tldr")) and str(r.get("brief_tldr")).strip()),
            "n_gates_passed": (
                None if pd.isna(r.get("n_gates_passed")) else int(r.get("n_gates_passed"))
            ),
        }
        for _, r in brief.sort_values("ticker").iterrows()
    ]
    return {
        "row_count": len(brief),
        "columns": sorted(brief.columns),
        "tickers": sorted(brief["ticker"].astype(str)),
        "exemplar": exemplar,
    }


def _themes_nonempty(value: Any) -> bool:
    # ``themes`` round-trips from parquet as a list OR a numpy ndarray under
    # pandas-3.0 infer_string. Guard length, never truthiness (ndarray bool is
    # ambiguous). Template rows carry ``[]``; Flash rows carry the LLM list.
    return value is not None and len(value) > 0


def extract_projection(events: pd.DataFrame) -> dict[str, Any]:
    """Golden projection for the extract stage (Phase 3b).

    Locks the schema + the per-row routing decision (template vs Flash) and
    the typed-field presence — NOT the verbose LLM prose or the volatile
    ``extracted_at`` timestamp. A template that stops firing (predicate /
    entity regression) flips ``extraction_method`` template→flash here; a
    Flash model that returns empty themes flips ``themes_nonempty``; a schema
    drift shows in ``columns``. ``confidence`` is deterministic under cassette
    replay (the recorded Flash response is fixed), so pinning it catches a
    normalisation change.
    """
    rows = []
    for _, r in events.sort_values("news_id").iterrows():
        tfj = r.get("template_fields_json")
        rows.append(
            {
                "news_id": str(r["news_id"]),
                "extraction_method": str(r["extraction_method"]),
                "template_id": (None if pd.isna(r.get("template_id")) else str(r["template_id"])),
                "event_type": str(r["event_type"]),
                "has_template_fields": isinstance(tfj, str) and bool(tfj),
                "themes_nonempty": _themes_nonempty(r.get("themes")),
                # 6dp (not 4dp): deterministic under cassette replay, so the
                # only source of churn is a normalisation change in the code —
                # 6dp catches sub-percent drift a coarser round would mask.
                "confidence": round(float(r["confidence"]), 6),
            }
        )
    return {
        "row_count": len(events),
        "columns": sorted(events.columns),
        "rows": rows,
    }


def _mcap_bucket(value: Any) -> str | None:
    # Bucket the raw market_cap float so the golden locks the bracket-filter
    # decision without pinning a 9-digit snapshot number that invites rot.
    if value is None or pd.isna(value):
        return None
    v = float(value)
    if v < 500e6:
        return "<500M"
    if v < 1e9:
        return "500M-1B"
    if v < 3e9:
        return "1B-3B"
    if v < 10e9:
        return "3B-10B"
    return ">10B"


def map_themes_projection(candidates: pd.DataFrame) -> dict[str, Any]:
    """Golden projection for the map-themes stage (Phase 3b).

    Locks the schema + per-row gate decision (which of tenk/press/insider
    passed) + verified status + an mcap bucket + whether a catalyst was
    resolved. EXCLUDES the volatile/churny fields: LLM prose (``rationale``,
    ``company_name``), ``llm_confidence`` (deterministic under replay but pure
    float churn; row order is already pinned by the ticker tie-break in
    ``map_themes``), the raw ``market_cap`` float (→ bucket), and the catalyst
    prose (``source_event_*`` → the ``has_catalyst`` boolean). The gate verdicts
    are what a verification regression would flip.
    """
    rows = []
    for _, r in candidates.sort_values("ticker").iterrows():
        rows.append(
            {
                "ticker": str(r["ticker"]),
                "theme": str(r["theme"]),
                "verified": bool(r["verified"]),
                "n_gates_passed": int(r["n_gates_passed"]),
                "gates_passed_str": str(r.get("gates_passed_str", "")),
                "n_gates_failed": int(r["n_gates_failed"]),
                "n_gates_unknown": int(r["n_gates_unknown"]),
                "mcap_bucket": _mcap_bucket(r.get("market_cap")),
                "has_catalyst": bool(
                    pd.notna(r.get("source_event_url")) and r.get("source_event_url")
                ),
            }
        )
    return {
        "row_count": len(candidates),
        "columns": sorted(candidates.columns),
        "tickers": sorted(candidates["ticker"].astype(str)) if len(candidates) else [],
        "rows": rows,
    }


def _ticker_list(value: Any) -> list[str]:
    # ``tickers`` round-trips from parquet as a list OR ndarray under pandas-3.0
    # infer_string; NaN floats can appear in empty cells. Length-guard, never
    # truthiness (ndarray bool is ambiguous).
    if value is None:
        return []
    if isinstance(value, float):  # NaN sentinel
        return []
    return sorted(str(t) for t in value)


def _cluster_size(extra: Any) -> int:
    # ``extra`` is a JSON string; cluster_size is the cross-source merge marker
    # stamped by news_ingest. Absent (un-clustered row) → 1.
    if not isinstance(extra, str) or not extra:
        return 1
    try:
        return int(json.loads(extra).get("cluster_size", 1))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 1


def ingest_projection(news: pd.DataFrame) -> dict[str, Any]:
    """Golden projection for the news-ingest stage (Phase 3b).

    Locks the cross-source MERGE outcome: per-source row counts, total count,
    schema, and a per-row exemplar (id / source / ticker tagging / body
    presence / cluster_size). A source parser breaking drops that source from
    ``by_source``; dedup collapsing too much drops ``row_count`` + inflates
    ``cluster_size``; a ``_SOURCE_PRIORITY`` regression flips the surviving
    ``source`` for a deduped URL; a ticker-tagging regression moves
    ``n_tickers_total``. Sorted by ``id`` (content-addressed, stable) — NOT by
    timestamp (EDGAR rows all land at 00:00 UTC → ties). Excludes title/body
    prose, url, keywords, raw timestamp.
    """
    by_source = news.groupby("source").size().to_dict() if len(news) else {}
    rows = []
    for _, r in news.sort_values("id").iterrows():
        tickers = _ticker_list(r.get("tickers"))
        body = r.get("body")
        rows.append(
            {
                "id": str(r["id"]),
                "source": str(r["source"]),
                "n_tickers": len(tickers),
                "tickers": tickers,
                "has_body": isinstance(body, str) and bool(body.strip()),
                "cluster_size": _cluster_size(r.get("extra")),
            }
        )
    return {
        "row_count": len(news),
        "columns": sorted(news.columns),
        "by_source": {str(k): int(v) for k, v in sorted(by_source.items())},
        "n_tickers_total": sum(len(_ticker_list(t)) for t in news.get("tickers", [])),
        "rows": rows,
    }


def _pctl_bucket(value: Any) -> str | None:
    # Coarse percentile bucket so the golden locks the gate decision without
    # pinning a raw percentile float that invites snapshot rot.
    if value is None or pd.isna(value):
        return None
    v = float(value)
    if v < 25.0:
        return "<25"
    if v < 50.0:
        return "25-50"
    if v < 75.0:
        return "50-75"
    return ">=75"


def _int_or_none(value: Any) -> int | None:
    return None if pd.isna(value) else int(value)


def _str_or_none(value: Any) -> str | None:
    return None if pd.isna(value) else str(value)


def score_projection(scored: pd.DataFrame) -> dict[str, Any]:
    """Golden projection for the score stage (Phase 3b).

    Locks the headline ``layer4_weighted_score`` (the integer the whole stage
    composes), the industry-cohort resolution, the magic-formula + drawdown
    decisions, the catalyst event type, and coarse percentile buckets for the
    insider / fcff / valuation signals. EXCLUDES the raw signal floats
    (``insider_score_usd``, ``fcff_yield_pct``, ``valuation_*``, ``roic/roe``,
    ``technical_*`` raw, ``valuation_financials_*``, ``catalyst_confidence``) —
    deterministic under frozen inputs but pure float churn (memo §3/§8). A
    scoring regression flips the integer score; a cohort regression flips
    ``peer_cohort_level``/``industry_id``; a magic-formula change flips the rank.
    """
    rows = []
    for _, r in scored.sort_values("ticker").iterrows():
        rows.append(
            {
                "ticker": str(r["ticker"]),
                "theme": str(r.get("theme", "")),
                "layer4_weighted_score": int(r["layer4_weighted_score"]),
                "industry_id": _int_or_none(r.get("industry_id")),
                "industry_name": _str_or_none(r.get("industry_name")),
                "sector_name": _str_or_none(r.get("sector_name")),
                "peer_cohort_level": str(r["peer_cohort_level"]),
                "insider_pctl_bucket": _pctl_bucket(r.get("insider_score_sector_percentile")),
                "fcff_pctl_bucket": _pctl_bucket(r.get("fcff_yield_sector_percentile")),
                "valuation_pctl_bucket": _pctl_bucket(
                    r.get("valuation_composite_sector_percentile")
                ),
                "magic_formula_rank": _int_or_none(r.get("magic_formula_rank")),
                "magic_formula_health_pass": bool(r["magic_formula_health_pass"]),
                "magic_formula_cohort_n": _int_or_none(r.get("magic_formula_cohort_n")),
                "deep_drawdown_reversal": bool(r["deep_drawdown_reversal"]),
                "catalyst_event_type": _str_or_none(r.get("catalyst_event_type")),
                "catalyst_strength": round(float(r["catalyst_strength"]), 4)
                if pd.notna(r.get("catalyst_strength"))
                else None,
            }
        )
    return {
        "row_count": len(scored),
        "columns": sorted(scored.columns),
        "tickers": sorted(scored["ticker"].astype(str)),
        "rows": rows,
    }
