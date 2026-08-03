"""Pure exit-geometry leaf.

Single source of truth for the ATR bracket exit computation used today by the
``/edge`` what-if lens (``atr_bracket_1p5`` / "bezpazery") and, later, by the
SIM broker-manager once it is wired for order placement. Stdlib-only — this
is the first module relocated into the shared ``broker_contract`` package
(step 2A-1 of the broker-manager extraction design memo,
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``
§2.1), a dependency-free leaf consumed by both ``alphalens_pipeline`` and
``alphalens_research`` without carrying any research-side or broker-side
dependency along with it.
"""

__status__ = "ACTIVE"

from broker_contract.exit_geometry.policy import (
    AtrBracketPolicy,
    ExitPolicy,
    SetupStaticPolicy,
)
from broker_contract.exit_geometry.registry import resolve_exit_policy

__all__ = [
    "AtrBracketPolicy",
    "ExitPolicy",
    "SetupStaticPolicy",
    "resolve_exit_policy",
]
