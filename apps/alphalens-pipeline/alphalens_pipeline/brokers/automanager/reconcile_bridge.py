"""Reconcile-bridge — the auto-manager's adapter over the read-only engine.

One thin delegate to the shipped reconcile_brackets (design memo, Components
§10): the control loop and the crash-recovery start-up path both call verdicts
instead of reaching into brokers.reconcile directly, so the loop depends on a
single automanager seam. It adds NO reclassification — reconcile stays the sole
source of truth. today is forwarded verbatim so the trading-day PAST-TTL sweep
is pinnable from the caller.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import Any

from alphalens_pipeline.brokers.contract import Broker
from alphalens_pipeline.brokers.reconcile import ReconcileVerdict, reconcile_brackets


def verdicts(
    records: Iterable[Mapping[str, Any]],
    broker: Broker,
    *,
    today: dt.date | None = None,
) -> list[ReconcileVerdict]:
    """Recompute every journal bracket's verdict (loop tick + crash recovery)."""
    return reconcile_brackets(records, broker, today=today)


__all__ = ["verdicts"]
