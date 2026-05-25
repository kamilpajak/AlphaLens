"""Mom + low-vol scorer adapter.

The adapter selects top-N tickers by ``z(mom_12_1m) - vol_w * z(vol_60d)``
filtered by ADV. Strategy itself FAIL'd phase-robust as
``mom_lowvol_combo_2026_04_29`` (see postmortem failure 7); the scorer
function is retained at RESEARCH_ONLY status because it is the BASE for
the Layer 4 vol-target overlay test (and any future overlay candidate).
Sharing the function from the package proper means experiment scripts
need not import each other through implicit-namespace package magic.
"""

from typing import Literal

from alphalens_research.screeners.momentum_lowvol.scorer import momentum_lowvol_adapter

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "RESEARCH_ONLY"

__all__ = ["momentum_lowvol_adapter"]
