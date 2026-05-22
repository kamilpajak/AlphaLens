"""Layer 2 event-driven PEAD screener with Sloan accrual quality conditioning.

Locked into pre-reg ``event_drift_v3_pead_quality_clean`` (2026-05-03):

  - Foster (1977) SUE first-filed PIT, top-quintile cohort filter
  - Sloan (1996) total accruals ratio, below-median quality filter
  - Bernard-Thomas day-1 sign confirmation gate
  - 8-K Item 2.02 announcement timestamp + after-hours T0 rule
  - Single-active-window invariant per ticker, hold [d+2, d+60]
  - Universe ex Financials/Utilities (GICS 40/55)
  - Threshold |t|>=3.50 (program meta-Bonferroni n=20)

Class status governed by tests/test_layer_status.py LAYERS_WITH_STATUS.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "RESEARCH_ONLY"
