"""Live-gross WARNING per memo §6.1 Path B.

After each reconcile cycle, compute live position notional from the
Alpaca account snapshot and log a WARNING if it exceeds 100% of equity.
The memo flagged this as the closed-loop check the plan-time gross
guard cannot provide (slow-creep drift over weeks of partial fills).

Implementation: the Alpaca account object exposes:

  account.equity            — full account equity
  account.long_market_value — sum of (qty × current price) over long positions

We use ``long_market_value`` (paper accounts are long-only in our flow)
divided by equity to get the live gross ratio. Anything above 1.0×
indicates the position book has crept past book-size and the operator
should escalate to a Phase-B tightening of STEADY_STATE_GROSS_FRAC.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from alphalens_pipeline.paper.broker import BrokerClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GrossGuardReport:
    """Summary of the gross-guard check after a reconcile cycle."""

    equity: float
    long_market_value: float
    gross_ratio: float
    warning_emitted: bool


def check_live_gross(broker: BrokerClient) -> GrossGuardReport:
    """Pull the account snapshot and check live gross against equity.

    Args:
        broker: an :class:`AlpacaClient` already wired to the
            same profile the reconciler used (main / test).

    Logs a WARNING if ``long_market_value / equity > 1.0``. The check is
    cheap (one ``get_account()`` call); the planner's per-day target keeps
    this well below 1.0 in steady state, so the WARNING is reserved for
    the slow-creep partial-fill drift case documented in memo §6.1.

    Returns a :class:`GrossGuardReport` so the caller (CLI) can surface
    the numbers even when no warning fires.
    """
    account = broker.get_account()
    try:
        equity = float(account.equity)
    except (TypeError, ValueError):
        logger.warning("gross guard: equity not numeric (%r); skipping check", account.equity)
        return GrossGuardReport(
            equity=0.0, long_market_value=0.0, gross_ratio=0.0, warning_emitted=False
        )

    long_market_value_raw = getattr(account, "long_market_value", None) or 0
    try:
        long_market_value = float(long_market_value_raw)
    except (TypeError, ValueError):
        long_market_value = 0.0

    if equity <= 0:
        gross_ratio = 0.0
    else:
        gross_ratio = long_market_value / equity

    warning = gross_ratio > 1.0
    if warning:
        logger.warning(
            "GROSS GUARD: live long_market_value=$%.0f exceeds equity=$%.0f "
            "(ratio=%.2f). Memo §6.1 escalation: consider tightening "
            "STEADY_STATE_GROSS_FRAC in a Phase-B follow-up.",
            long_market_value,
            equity,
            gross_ratio,
        )
    else:
        logger.info(
            "gross guard: long_market_value=$%.0f equity=$%.0f ratio=%.2f (OK)",
            long_market_value,
            equity,
            gross_ratio,
        )

    return GrossGuardReport(
        equity=equity,
        long_market_value=long_market_value,
        gross_ratio=gross_ratio,
        warning_emitted=warning,
    )


__all__ = [
    "GrossGuardReport",
    "check_live_gross",
]
