"""JSON response schema for the LLM brief generator.

Shared by Pro and Flash routes — same 4 fields, both models return identical
shape so the orchestrator + renderer don't need to branch on model.

Numerical / quantitative fields (position size, exits, entry levels,
weighted score) are NOT in this schema. Entry/exit price levels are computed
deterministically by ``alphalens_pipeline.thematic.trade_setup`` and persisted
as the ``brief_trade_setup`` JSON; the LLM only composes narrative — the LLM only composes
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
    },
    "required": [
        "tldr",
        "supply_chain_reasoning",
        "bear_summary",
        "catalyst_failure_exit",
    ],
}


__all__ = ["BRIEF_RESPONSE_SCHEMA"]
