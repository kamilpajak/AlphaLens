"""Shared test-only glue for the split ``parse_brief_to_spec`` / ``compute_setup_plan``.

PR-5 (broker-manager extraction, memo
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``)
split the pre-split ``compute_setup_plan(*, brief_trade_setup, paper_equity,
scale_factor, fx=None)`` into a brief-parse half and a money-math half.
:func:`plan_from_brief` re-composes the two so the sizing suites keep their
kwarg shape and expected values.

Since #1467 the spec states an amount, and since #1469 the parse takes that
amount. #1552 removed the production parse with the brief producer; the helper
keeps a verbatim copy, :func:`spec_from_brief`. The old ``paper_equity`` and ``scale_factor`` pair becomes
``pct / 100 x equity x scale``, which is what the old chain spent, so every
expected notional stays identical.
"""

from __future__ import annotations

import math

from alphalens_pipeline.paper.sizing import validate_trade_setup
from broker_contract.fx import FxConversion
from broker_contract.sizing import SetupPlan, TradeSetupNotPlannableError, compute_setup_plan
from broker_contract.trade_intent.schema import EntryTierSpec, PickSize, TpTrancheSpec, TradeSpec


def plan_from_brief(
    *,
    brief_trade_setup: dict,
    paper_equity: float,
    scale_factor: float,
    fx: FxConversion | None = None,
) -> SetupPlan:
    """Parse-then-size, mirroring the pre-split ``compute_setup_plan`` signature.

    Raises :class:`~broker_contract.sizing.TradeSetupNotPlannableError`
    via :func:`spec_from_brief` for the same unplannable briefs the
    pre-split function rejected.
    """
    account_currency = fx.account_currency if fx is not None else "USD"
    # Validation first, as the parse does it: an unplannable brief has no
    # percent to multiply.
    pct = validate_trade_setup(brief_trade_setup)
    spec = spec_from_brief(
        brief_trade_setup,
        notional_acct=pct / 100.0 * paper_equity * scale_factor,
        currency=account_currency,
    )
    return compute_setup_plan(spec, fx=fx)


def spec_from_brief(brief_trade_setup: dict, *, notional_acct: float, currency: str) -> TradeSpec:
    """Test-only copy of the ``parse_brief_to_spec`` removed with the brief
    producer in #1552, kept verbatim so the sizing suites exercise
    :func:`~broker_contract.sizing.compute_setup_plan` over the same inputs they
    always did. Not production code: nothing outside these tests builds a spec
    from a brief any more."""
    validate_trade_setup(brief_trade_setup)
    if not math.isfinite(notional_acct) or notional_acct <= 0:
        raise TradeSetupNotPlannableError(
            f"notional_acct={notional_acct!r} must be a positive finite amount"
        )

    entry_tiers_raw = brief_trade_setup["entry_tiers"]
    entry_tiers = tuple(
        EntryTierSpec(
            limit_price=float(raw["limit"]),
            alloc_pct=float(raw.get("alloc_pct", 0.0)),
            tag=str(raw.get("tag", "")),
        )
        for raw in entry_tiers_raw
    )

    tp_tranches_raw = brief_trade_setup.get("tp_tranches") or ()
    tp_tranches = tuple(
        TpTrancheSpec(
            price=float(raw["target"]),
            tranche_pct=float(raw.get("tranche_pct", 0.0)),
            r_multiple=float(raw.get("r_multiple", 0.0)),
            tag=str(raw.get("tag", "")),
        )
        for raw in tp_tranches_raw
    )

    disaster_stop = float(brief_trade_setup["disaster_stop"])
    order_ttl_days = int(
        brief_trade_setup.get("order_ttl_days") or 0
    )  # 0 sentinel preserved — must NOT fall through to TradeSpec's default (7)

    return TradeSpec(
        entry_tiers=entry_tiers,
        disaster_stop=disaster_stop,
        tp_tranches=tp_tranches,
        size=PickSize(notional_acct=notional_acct, currency=currency),
        order_ttl_days=order_ttl_days,
        side="long",
    )
