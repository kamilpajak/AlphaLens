"""JSON response schema for the Gemini brief generator.

Shared by Pro and Flash routes — same 5 fields, both models return identical
shape so the orchestrator + renderer don't need to branch on model.

Numerical / quantitative fields (position size, time exit, entry price,
weighted score) are NOT in this schema because they're pre-computed by
Phase D and injected into the prompt as facts — the LLM only composes
narrative around them. See doctrine
``feedback_llm_training_cutoff_numerical_data_2026_05_17``.
"""

from __future__ import annotations

BRIEF_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "tldr": {"type": "string"},
        "supply_chain_reasoning": {"type": "string"},
        "bear_summary": {"type": "string"},
        "catalyst_failure_exit": {"type": "string"},
        "entry_price_note": {"type": "string"},
    },
    "required": [
        "tldr",
        "supply_chain_reasoning",
        "bear_summary",
        "catalyst_failure_exit",
        "entry_price_note",
    ],
}


__all__ = ["BRIEF_RESPONSE_SCHEMA"]
