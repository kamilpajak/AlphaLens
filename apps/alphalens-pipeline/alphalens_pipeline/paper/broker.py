"""Broker-agnostic paper-trading client protocol (issue #388).

The paper harness (planner / submitter / reconciler / exit_manager /
gross_guard) talks to a *broker* only through the small structural
:class:`BrokerClient` protocol declared here — never against a concrete
vendor SDK type. Alpaca is the sole implementation today
(:class:`alphalens_pipeline.data.alt_data.alpaca_client.AlpacaClient`
satisfies the protocol structurally, no subclassing); a second paper
platform drops in by satisfying the same 9 methods.

Why a protocol, not a base class: every harness call site already took
``alpaca_client: Any`` and duck-typed it. This just names the duck and
makes conformance testable. ``shadow_return`` (Polygon) is
broker-independent, so swapping the broker only moves execution stats —
see docs/research/feedback_ledger_counterfactual_design_2026_06_02.md
(a broker switch is an EXECUTION-REGIME break; never pool execution
stats across platforms).

``platform`` is a SEPARATE axis from ``account``: platform selects the
broker ('alpaca'); account/profile ('main' / 'test') selects the
credential set WITHIN a platform. The ledger tags rows with both.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# Recognised paper-trading platforms. Application-level enforcement at the
# factory + at ledger insert time (mirrors VALID_ACCOUNTS in paper.ledger);
# the schema CHECK is informational only (SQLite ALTER can't add CHECK
# retroactively, so operator-migrated DBs may lack it).
VALID_PLATFORMS = frozenset({"alpaca"})

_DEFAULT_PLATFORM = "alpaca"


@runtime_checkable
class BrokerClient(Protocol):
    """Structural surface the paper harness needs from a broker.

    The 7 order/account/position primitives the planner / submitter /
    reconciler / exit_manager call, PLUS 2 READ-only enumerate primitives
    (9 methods total): ``list_open_orders`` / ``list_positions`` — the
    ``alphalens paper reset`` tool uses these to sweep + verify-flat
    without any vendor-specific
    bulk-close endpoint. Reset orchestrates cancellation + flattening from
    these enumerate reads + the existing ``cancel_order`` +
    ``submit_market_order`` — the protocol stays minimal + broker-agnostic
    (no Alpaca ``DELETE /v2/positions?cancel_orders=true`` leaks in here).

    Returns are ``Any`` — call sites duck-type the order / account /
    position objects (`.id`, `.status`, `.filled_qty`, `.filled_avg_price`,
    `.equity`, `.long_market_value`, `.qty`, `.symbol`, `.side`). The
    enumerate primitives return ``list`` of those same broker-native
    objects. Deliberately EXCLUDES submit_bracket_order / get_orders /
    trading_client (unused by the harness).

    NOTE: ``@runtime_checkable`` isinstance() validates method NAMES only,
    never signatures — conformance tests assert via ``inspect.signature``.
    """

    def submit_limit_order(
        self,
        *,
        symbol: str,
        qty: int | float,
        limit_price: float,
        side: str = "buy",
        time_in_force: str = "gtc",
    ) -> Any: ...

    def submit_stop_order(
        self,
        *,
        symbol: str,
        qty: int | float,
        stop_price: float,
        side: str = "sell",
        time_in_force: str = "gtc",
    ) -> Any: ...

    def submit_market_order(
        self,
        *,
        symbol: str,
        qty: int | float,
        side: str = "sell",
        time_in_force: str = "day",
    ) -> Any: ...

    def get_account(self) -> Any: ...

    def get_position(self, symbol: str) -> Any | None:
        """Return the live open position for ``symbol``, or ``None``.

        CONTRACT (relied on by exit_manager's ledger<->broker desync guard):
          * Return ``None`` ONLY when the broker DEFINITIVELY confirms there
            is no open position (the flat / absent state — e.g. a 404 / "does
            not exist" / "position not found" response). ``None`` is a
            POSITIVE assertion that the position is flat, never an "unknown".
          * RAISE on any transient / non-definitive failure (timeout, 401,
            5xx, connection error). A transient read is NOT flat.

        The harness treats ``None`` as authoritative truth: a broker-confirmed
        flat position while the ledger believes the plan is filled is a
        DESYNC the harness surfaces (it stops submitting protective orders for
        shares that do not exist). Conflating a transient error with ``None``
        would mask a real position as flat and DROP its disaster-stop, so an
        implementation MUST keep the two distinct.
        """
        ...

    def get_order(self, order_id: str) -> Any: ...

    def cancel_order(self, order_id: str) -> None: ...

    def list_open_orders(self) -> list: ...

    def list_positions(self) -> list: ...


def get_default_broker_client(
    *, platform: str = _DEFAULT_PLATFORM, profile: str = "main"
) -> BrokerClient:
    """Return the process-wide default :class:`BrokerClient` for the given
    platform + credential profile.

    ``platform`` selects the broker (only 'alpaca' today); ``profile``
    selects the credential set within it ('main' vs 'test') — the same
    account axis the ledger tags rows with. The factory owns the
    platform->client mapping so the vendor client never needs to know its
    own platform string.

    The alpaca import is lazy to keep CLI startup fast and avoid a
    top-level pipeline -> vendor-SDK import edge.
    """
    if platform not in VALID_PLATFORMS:
        raise ValueError(f"platform={platform!r} not in {sorted(VALID_PLATFORMS)}")
    if platform == "alpaca":
        from alphalens_pipeline.data.alt_data.alpaca_client import (
            get_default_alpaca_client,
        )

        return get_default_alpaca_client(profile=profile)
    # Unreachable while VALID_PLATFORMS == {"alpaca"}; guards a future
    # platform string added to the set without a dispatch arm here.
    raise ValueError(  # pragma: no cover
        f"no broker client wired for platform={platform!r}"
    )
