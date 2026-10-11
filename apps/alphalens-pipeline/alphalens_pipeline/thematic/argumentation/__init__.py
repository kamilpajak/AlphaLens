"""Layer 5 argumentation + brief generator.

Per design memo §2 Layer 5 + §14 locks #3/#5/#7: compose a mid-format
markdown brief per Phase D-scored candidate. Pro for weighted_score ≥ 4,
Flash for ≤ 3 (cost discipline). All numerical / real-time data is
pre-computed by Phase D and INJECTED into the prompt per doctrine
`fb_llm_cutoff_260517` — the LLM composes narrative only.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
