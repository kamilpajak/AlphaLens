"""Time-series position-sizing overlays on portfolio returns.

Operates downstream of any AlphaLens Scorer + BacktestEngine — takes the
realized portfolio-returns Series and rescales it via a sizing rule that
sees only past-and-current returns (no look-ahead). Sibling to
``alphalens/gates/``: gate modifies *selection*, overlay modifies
*size*.

Concrete sizing rules live in their own modules. The package exports the
protocol and the post-processor function.
"""

from typing import Literal

from alphalens.overlays.drawdown_control import (
    DrawdownControlConfig,
    DrawdownControlOverlay,
    apply_drawdown_control,
)
from alphalens.overlays.vol_target import (
    RealizedVolEstimator,
    VolTargeter,
    apply_vol_target,
)

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "RESEARCH_ONLY"

__all__ = [
    "DrawdownControlConfig",
    "DrawdownControlOverlay",
    "RealizedVolEstimator",
    "VolTargeter",
    "apply_drawdown_control",
    "apply_vol_target",
]
