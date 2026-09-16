"""Shared test-only glue for the split ``parse_brief_to_spec`` / ``compute_setup_plan``.

PR-5 (broker-manager extraction, memo
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``)
split the pre-split ``compute_setup_plan(*, brief_trade_setup, paper_equity,
scale_factor, fx=None)`` into a brief-parse half and a money-math half.
:func:`plan_from_brief` re-composes the two so the sizing suites keep their
kwarg shape and expected values.

Since #1467 the spec states an amount, and since #1469 ``parse_brief_to_spec``
takes that amount. The old ``paper_equity`` and ``scale_factor`` pair becomes
``pct / 100 x equity x scale``, which is what the old chain spent, so every
expected notional stays identical.
"""

from __future__ import annotations

from alphalens_pipeline.paper.sizing import parse_brief_to_spec, validate_trade_setup
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
    account_currency = fx.account_currency if fx is not None else "USD"
    # Validation first, as the parse does it: an unplannable brief has no
    # percent to multiply.
    pct = validate_trade_setup(brief_trade_setup)
    spec = parse_brief_to_spec(
        brief_trade_setup,
        notional_acct=pct / 100.0 * paper_equity * scale_factor,
        currency=account_currency,
    )
    return compute_setup_plan(spec, fx=fx)
