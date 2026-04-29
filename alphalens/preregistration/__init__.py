"""Pre-registration ledger — tracks tested hypotheses per signal class.

Closes Gap #1 from `docs/research/strategy_validation_playbook.md`. Solves
the multiple-comparisons problem: every strategy candidate gets a frozen
record of (hypothesis, params, periods, success_criteria) BEFORE the
multi-phase audit, and a one-shot completion record AFTER. The count of
hypotheses in a signal class drives the Bonferroni denominator (Harvey-
Liu-Zhu 2016).
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
