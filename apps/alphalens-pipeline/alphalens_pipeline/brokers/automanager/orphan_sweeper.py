"""Orphan-sweeper — read-only detector for the place-before-journal crash window.

_place_and_record journals AFTER placement, so a crash between the POST and the
journal write leaves an order/position at Saxo the append-only journal never
recorded. On start-of-process the sweeper flags them (design memo, Components
§12): an open ORDER whose id is absent from the journal's known entry + exit
order ids, and an open POSITION whose ExternalReference (the bracket
client_request_id) is absent from the journal's client_request_ids. STRICTLY
read-only + alert-only — never cancels an unrecorded order. The position arm is
gated on SupportsFillCrossCheck, so a broker without it degrades to an
order-only sweep (mirrors the reconcile engine's capability degradation).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from alphalens_pipeline.brokers.contract import Broker
from alphalens_pipeline.brokers.reconcile import SupportsFillCrossCheck

OrphanKind = Literal["order", "position"]


@dataclass(frozen=True)
class Orphan:
    order_id: str
    external_reference: str
    kind: OrphanKind


def _journal_index(journal: Iterable[Mapping[str, Any]]) -> tuple[set[str], set[str]]:
    known_order_ids: set[str] = set()
    known_refs: set[str] = set()
    for record in journal:
        for bracket in record.get("brackets") or []:
            entry_id = bracket.get("entry_order_id")
            if entry_id:
                known_order_ids.add(str(entry_id))
            for exit_id in bracket.get("exit_order_ids") or []:
                if exit_id:
                    known_order_ids.add(str(exit_id))
            request_id = bracket.get("client_request_id")
            if request_id:
                known_refs.add(str(request_id))
    return known_order_ids, known_refs


def sweep(broker: Broker, journal: Iterable[Mapping[str, Any]]) -> list[Orphan]:
    """Flag open orders/positions at the broker that the journal never recorded."""
    known_order_ids, known_refs = _journal_index(journal)
    orphans: list[Orphan] = []
    for state in broker.list_open_orders():
        if str(state.order_id) not in known_order_ids:
            orphans.append(
                Orphan(order_id=str(state.order_id), external_reference="", kind="order")
            )
    if isinstance(broker, SupportsFillCrossCheck):
        for reference in broker.get_open_position_references():
            if str(reference) not in known_refs:
                orphans.append(
                    Orphan(order_id="", external_reference=str(reference), kind="position")
                )
    return orphans


__all__ = ["Orphan", "OrphanKind", "sweep"]
