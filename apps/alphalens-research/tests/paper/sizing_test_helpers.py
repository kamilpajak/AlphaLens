"""Shared test-only glue for the split ``parse_brief_to_spec`` / ``compute_setup_plan``.

PR-5 (broker-manager extraction, memo
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``)
splits the pre-split ``compute_setup_plan(*, brief_trade_setup, paper_equity,
scale_factor, fx=None)`` into a brief-parse half (``parse_brief_to_spec``,
emitting a :class:`~broker_contract.trade_intent.schema.TradeSpec`) and a
money-math half (``compute_setup_plan(spec, ...)``). :func:`plan_from_brief`
re-composes the two so the existing sizing test suites keep calling with the
SAME ``brief_trade_setup=`` kwarg shape they always did, without rewriting
any expected value — the behavior-preservation guard for this split.
"""

from __future__ import annotations

from alphalens_pipeline.paper.sizing import parse_brief_to_spec
from broker_contract.fx import FxConversion
from broker_contract.sizing import SetupPlan, compute_setup_plan


def plan_from_brief(
    *,
    brief_trade_setup: dict,
    paper_equity: float,
    scale_factor: float,
    fx: FxConversion | None = None,
) -> SetupPlan:
    """Parse-then-size, mirroring the pre-split ``compute_setup_plan`` signature.

    Raises :class:`~broker_contract.sizing.TradeSetupNotPlannableError`
    via :func:`parse_brief_to_spec` for the same unplannable briefs the
    pre-split function rejected.
    """
    spec = parse_brief_to_spec(brief_trade_setup)
    return compute_setup_plan(
        spec,
        paper_equity=paper_equity,
        scale_factor=scale_factor,
        fx=fx,
    )
