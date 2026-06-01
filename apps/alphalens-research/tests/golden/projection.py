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
