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

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# Recognised paper-trading platforms. Application-level enforcement at the
# factory + at ledger insert time (mirrors VALID_ACCOUNTS in paper.ledger);
# the schema CHECK is informational only (SQLite ALTER can't add CHECK
# retroactively, so operator-migrated DBs may lack it).
VALID_PLATFORMS = frozenset({"alpaca"})

_DEFAULT_PLATFORM = "alpaca"


@dataclass(frozen=True)
class ExitTranche:
    """One take-profit tranche to attach to an open position.

    Broker-neutral INTENT, not a broker order: "sell ``qty`` shares of the
    held position if price reaches ``take_profit_limit``". A caller hands the
    adapter a sequence of these (the TP ladder) plus a single disaster stop;
    the adapter decides how to realize the structure on its venue.

    ``qty`` is WHOLE shares — Alpaca OCO/bracket legs reject fractional qty,
    and a broker-neutral intent should not assume any venue supports them.
    The whole-share + positivity invariant is venue-independent, so it is
    enforced HERE at the intent layer (``__post_init__``) rather than deferred
    to a broker 422 — a fractional / zero / negative ``qty`` is a caller bug,
    not a venue quirk.
    """

    qty: int
    take_profit_limit: float

    def __post_init__(self) -> None:
        # bool is an int subclass; reject it explicitly so True/False can't
        # masquerade as a 1-share / 0-share tranche.
        if isinstance(self.qty, bool) or not isinstance(self.qty, int):
            raise TypeError(
                f"ExitTranche.qty must be a whole-share int, got {self.qty!r} "
                f"({type(self.qty).__name__}); fractional qty is rejected by "
                "Alpaca OCO/bracket legs and unsupported by the intent."
            )
        if self.qty <= 0:
            raise ValueError(
                f"ExitTranche.qty must be > 0, got {self.qty!r}; a zero/negative "
                "tranche cannot sell shares and would create an invalid order."
            )


@dataclass(frozen=True)
class ExitLadderLeg:
    """Broker-neutral handle for ONE attached take-profit + stop-loss pair.

    Carries BOTH broker order ids on purpose. A broker's open-orders
    enumeration may surface only the PARENT of an attached pair — Alpaca's
    ``list_open_orders`` returns the take-profit limit but NOT the sibling
    stop-loss leg (it is reachable only via the parent's ``legs`` at submit
    time). So the caller MUST persist + poll both ids captured here rather
    than rediscover them later: ``tp_order_id`` is the take-profit order,
    ``sl_order_id`` is the stop-loss order. ``stop_price`` is the single
    disaster stop shared by every tranche in the ladder (see
    :meth:`BrokerClient.attach_exit_ladder`).

    ``take_profit_limit`` / ``stop_price`` are the prices ACTUALLY SUBMITTED
    to the broker (e.g. already venue-tick-rounded), not the caller's raw
    intent — so a future reconciler can compare a leg directly against the
    broker's order/fill price with no spurious sub-tick drift.

    For Alpaca each tranche carries its OWN ``sl_order_id`` (M OCO groups → M
    distinct stop legs at the same ``stop_price``). A future broker with a
    native one-stop-many-targets / reduce-only primitive would repeat its
    single shared stop id across every leg's ``sl_order_id``, so a reconciler
    that cancels stops MUST dedup ``sl_order_id`` across legs before issuing
    cancels.
    """

    tranche_index: int
    qty: int
    take_profit_limit: float
    stop_price: float
    tp_order_id: str
    sl_order_id: str


@runtime_checkable
class BrokerClient(Protocol):
    """Structural surface the paper harness needs from a broker.

    The 7 order/account/position primitives the planner / submitter /
    reconciler / exit_manager call, PLUS 2 READ-only enumerate primitives
    and 1 intent-level exit-ladder primitive (10 methods total):
    ``list_open_orders`` / ``list_positions`` — the
    ``alphalens paper reset`` tool uses these to sweep + verify-flat
    without any vendor-specific
    bulk-close endpoint. Reset orchestrates cancellation + flattening from
    these enumerate reads + the existing ``cancel_order`` +
    ``submit_market_order`` — the protocol stays minimal + broker-agnostic
    (no Alpaca ``DELETE /v2/positions?cancel_orders=true`` leaks in here).
    ``attach_exit_ladder`` is the broker-neutral OCO-ladder exit intent: the
    harness expresses M take-profit tranches + one disaster stop and each
    adapter decomposes it its own way (no OCO/bracket mechanics leak here).

    Returns are ``Any`` — call sites duck-type the order / account /
    position objects (`.id`, `.status`, `.filled_qty`, `.filled_avg_price`,
    `.equity`, `.long_market_value`, `.qty`, `.symbol`, `.side`). The
    enumerate primitives return ``list`` of those same broker-native
    objects; ``attach_exit_ladder`` returns broker-neutral
    :class:`ExitLadderLeg` handles, not raw vendor orders. Deliberately
    EXCLUDES submit_bracket_order / get_orders / trading_client (unused by
    the harness).

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

    def attach_exit_ladder(
        self,
        *,
        symbol: str,
        tranches: Sequence[ExitTranche],
        stop_price: float,
        time_in_force: str = "gtc",
    ) -> list[ExitLadderLeg]:
        """Attach M take-profit tranches + ONE disaster stop to an open
        position, as a broker-neutral INTENT.

        Contract (intent level, NOT a venue order shape): for each
        :class:`ExitTranche` the caller wants to sell ``tranche.qty`` shares
        if price reaches ``tranche.take_profit_limit``; if instead price
        falls to ``stop_price`` the WHOLE aggregate position should exit.
        Every tranche becomes an ATOMIC take-profit / stop-loss pair sharing
        the SAME ``stop_price`` — when either side of a pair fills the broker
        auto-cancels the sibling. The single disaster stop is therefore
        realized as M stop legs at one price (so the held position is fully
        protected regardless of which tranche fills first), never as one
        independent full-size stop (which would block the take-profit legs).

        BROKER-NEUTRAL BOUNDARY: the reconciler / ledger must NOT learn how a
        venue implements this. The Alpaca adapter submits M OCO orders; a
        richer broker may use a native one-stop-many-targets / reduce-only
        primitive. Both return the same :class:`ExitLadderLeg` list — one per
        tranche, carrying BOTH order ids so the caller can persist + poll the
        take-profit AND the stop-loss leg independently (a broker open-orders
        enumeration may surface only the parent — see :class:`ExitLadderLeg`).

        ``tranches`` MUST be non-empty: a ladder with no take-profit tranches
        attaches NO protective stop, which is the exact "silently drop the
        stop" failure the empty-``legs`` guard defends against, only at the
        input boundary. An empty sequence is a caller bug — the adapter raises
        rather than returning a no-op ``[]``.
        """
        ...


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
