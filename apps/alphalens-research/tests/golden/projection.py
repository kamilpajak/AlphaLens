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
