"""Compound: insider_form4_opportunistic x pc_abnormal_volume (2-way EW z-score average).

Pre-reg: insider_pc_compound_2026_05_10 (Bonferroni effective n=34, |t| >= 2.974).
Design memo: docs/research/insider_pc_compound_design_2026_05_10.md
"""

__status__ = "RESEARCH_ONLY"

from alphalens.screeners.compound_insider_pc.zscore_compound import (
    compound_score_from_components,
)

__all__ = ["compound_score_from_components"]
