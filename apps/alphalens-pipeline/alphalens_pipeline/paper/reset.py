"""Broker-agnostic paper-account reset orchestration.

Motivated by an incident: a broken-migration window placed Alpaca orders
the ledger never recorded, leaving orphan orders + positions that the
operator had to clear by hand. This module rebuilds that hand-recovery as
a safe, repeatable, broker-agnostic routine driven only by the
:class:`~alphalens_pipeline.paper.broker.BrokerClient` protocol — never a
vendor-specific bulk-close endpoint.

Flow (orchestrated by ``alphalens paper reset``):
  1. snapshot open orders + positions (``list_open_orders`` /
     ``list_positions``),
  2. cancel every open order (``cancel_order`` per id, best-effort),
  3. flatten every position with an opposite-side market order sized to
     ``abs(qty)`` (long -> SELL, short -> BUY) — never flips a long to
     short,
  4. POLL the broker until BOTH lists are empty or a bounded sweep budget
     is exhausted, re-issuing cancels + flattens each sweep to absorb the
     paper-state lag (Alpaca positions can linger a few seconds after a
     bulk close; ~2 sweeps observed live).

The ledger clear is a SEPARATE step (``ledger.reset_paper_chain``) the CLI
runs after the broker is flat; this module owns only the broker side so it
stays testable with an in-memory stub and free of any sqlite import.

Market-hours note: flattening uses MARKET orders, which Alpaca rejects
outside regular trading hours (RTH). Run the reset during market hours;
when the market is closed the flatten submits are rejected, positions
linger, and the 'NOT fully flat' WARNING is EXPECTED (not a stuck
position). ``ResetResult.n_flatten_rejected`` surfaces the rejected count
so a closed-market run is distinguishable from a genuinely stuck one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from alphalens_pipeline.paper.broker import BrokerClient

logger = logging.getLogger(__name__)

# Bounded retry budget for the converge-to-flat poll. Live observation
# needed ~2 sweeps after a bulk close; 6 leaves generous headroom for a
# slow paper-state propagation without spinning forever on a genuinely
# stuck position (which we WARN about instead of looping).
DEFAULT_MAX_SWEEPS = 6


@dataclass(frozen=True)
class FlattenInstruction:
    """One opposite-side market order that closes a held position.

    ``qty`` is a float: Alpaca paper can hold fractional shares (e.g. 0.5),
    and Alpaca accepts a fractional market-order qty to flatten them. An
    integer truncation here (``int(0.5) -> 0``) would silently drop the
    position from the flatten plan, yet ``list_positions`` would keep
    counting it, burning the whole sweep budget on a false 'NOT fully
    flat' WARNING.
    """

    symbol: str
    qty: float
    side: str  # 'sell' to close a long, 'buy' to cover a short


@dataclass
class ResetSnapshot:
    """Counts captured before the sweep (for the dry-run / pre-flight print)."""

    n_open_orders: int
    n_positions: int
    order_ids: list[str] = field(default_factory=list)
    flatten_plan: list[FlattenInstruction] = field(default_factory=list)


@dataclass
class ResetResult:
    """Outcome of the live (``--yes``) sweep."""

    n_cancel_calls: int
    n_flatten_calls: int
    sweeps_used: int
    final_open_orders: int
    final_positions: int
    # Flatten submits the broker REJECTED across all sweeps. A non-zero
    # count with a non-flat result usually means the market was closed
    # (Alpaca rejects market orders outside RTH) — distinguishes a closed-
    # market run from a genuinely stuck position.
    n_flatten_rejected: int = 0

    @property
    def is_flat(self) -> bool:
        return self.final_open_orders == 0 and self.final_positions == 0


def _position_qty(position: Any) -> float:
    """Signed share count for a broker position object.

    Alpaca reports ``qty`` as a signed string (negative for shorts) that
    may be fractional (e.g. ``"0.5"``). We keep BOTH the sign (so the
    caller can pick the closing side) and the fractional magnitude (so a
    sub-1-share position is still flattened — Alpaca accepts a fractional
    market qty); the order qty itself is always ``abs(...)`` so we never
    flip a long to short.
    """
    raw = getattr(position, "qty", 0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("position has unparseable qty=%r; skipping", raw)
        return 0.0


def _flatten_instruction(position: Any) -> FlattenInstruction | None:
    """Opposite-side close for a single position, or None for a genuinely-
    zero one. A fractional position (0 < |qty| < 1) is flattened, NOT
    skipped — truncating it to 0 would leave it un-flattened while
    ``list_positions`` still counts it."""
    qty = _position_qty(position)
    if qty == 0:
        return None
    symbol = getattr(position, "symbol", None)
    if not symbol:
        logger.warning("position has no symbol (%r); skipping", position)
        return None
    # Sign drives the side; magnitude is always abs(qty) so a long sells
    # EXACTLY the held qty (never over-sells into a short) and a short
    # covers EXACTLY the borrowed qty (never over-buys into a long).
    side = "sell" if qty > 0 else "buy"
    return FlattenInstruction(symbol=str(symbol), qty=abs(qty), side=side)


def snapshot_reset(broker: BrokerClient) -> ResetSnapshot:
    """Read current broker state into a :class:`ResetSnapshot` (no mutation)."""
    orders = list(broker.list_open_orders())
    positions = list(broker.list_positions())
    order_ids = [str(getattr(o, "id", "")) for o in orders]
    flatten_plan = [
        instr for instr in (_flatten_instruction(p) for p in positions) if instr is not None
    ]
    return ResetSnapshot(
        n_open_orders=len(orders),
        n_positions=len(positions),
        order_ids=order_ids,
        flatten_plan=flatten_plan,
    )


def _sweep_once(broker: BrokerClient) -> tuple[int, int, int]:
    """Cancel all open orders + flatten all positions ONCE.

    Returns ``(n_cancel_calls, n_flatten_calls, n_flatten_rejected)``
    issued this sweep. Best-effort: a single broker exception (already-
    cancelled order, fill race, market-closed rejection) is logged and the
    sweep continues — the poll loop re-checks state, so a transient failure
    is retried next sweep rather than aborting the whole reset mid-way.
    """
    n_cancel = 0
    n_flatten = 0
    n_flatten_rejected = 0
    for order in broker.list_open_orders():
        order_id = str(getattr(order, "id", ""))
        if not order_id:
            continue
        try:
            broker.cancel_order(order_id)
            n_cancel += 1
        except Exception:
            logger.warning("cancel_order(%s) failed; continuing", order_id, exc_info=True)
    for position in broker.list_positions():
        instr = _flatten_instruction(position)
        if instr is None:
            continue
        try:
            broker.submit_market_order(
                symbol=instr.symbol,
                qty=instr.qty,
                side=instr.side,
                time_in_force="day",
            )
            n_flatten += 1
        except Exception:
            n_flatten_rejected += 1
            logger.warning(
                "submit_market_order(%s %s %s) failed; continuing",
                instr.side,
                instr.symbol,
                instr.qty,
                exc_info=True,
            )
    return n_cancel, n_flatten, n_flatten_rejected


def execute_reset(broker: BrokerClient, *, max_sweeps: int = DEFAULT_MAX_SWEEPS) -> ResetResult:
    """Cancel + flatten everything, then poll until flat or sweeps exhausted.

    Re-issues cancels + flattens on every sweep so the paper-state lag
    (positions/orders that still appear for a few seconds after a bulk
    close) is absorbed: a position that re-appears on the next poll is
    simply flattened again. Stops as soon as both lists are empty, or
    after ``max_sweeps`` (the caller WARNs loudly if not flat by then).
    """
    total_cancel = 0
    total_flatten = 0
    total_flatten_rejected = 0
    sweeps_used = 0
    final_orders = 0
    final_positions = 0
    for _ in range(max_sweeps):
        sweeps_used += 1
        n_cancel, n_flatten, n_flatten_rejected = _sweep_once(broker)
        total_cancel += n_cancel
        total_flatten += n_flatten
        total_flatten_rejected += n_flatten_rejected
        # Re-read AFTER the sweep to decide convergence; this is the poll
        # that catches the lag (state may still be non-empty here).
        final_orders = len(list(broker.list_open_orders()))
        final_positions = len(list(broker.list_positions()))
        if final_orders == 0 and final_positions == 0:
            break
    return ResetResult(
        n_cancel_calls=total_cancel,
        n_flatten_calls=total_flatten,
        sweeps_used=sweeps_used,
        final_open_orders=final_orders,
        final_positions=final_positions,
        n_flatten_rejected=total_flatten_rejected,
    )


__all__ = [
    "DEFAULT_MAX_SWEEPS",
    "FlattenInstruction",
    "ResetResult",
    "ResetSnapshot",
    "execute_reset",
    "snapshot_reset",
]
