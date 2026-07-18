"""Shared plumbing for the attended Saxo SIM first-fill experiment drivers.

Scratch-script tier (protocol G2, first-fill experiment memo
``docs/research/saxo_first_fill_experiment_2026_07_18.md``): thin
compositions over the gated broker wrappers. House rules enforced here:

- EVERY placement goes through ``SaxoBroker.place_bracket_order`` (the
  ``ALPHALENS_BROKER_ALLOW_ORDERS`` gate, precheck, tick quantization and
  the single POST all live inside the wrapper — never raw HTTP);
- EVERY placement is journaled via ``build_submission_record`` /
  ``append_submission_record`` so ``alphalens broker reconcile`` stays the
  verdict engine (Python-API placements are otherwise invisible to it);
- EVERY payload lands as numbered JSON in the session scratch dir
  (``$SCRATCH``, default ``~/.alphalens/broker_orders/experiments/...``).

The scripts import this module by script-dir convention (``sys.path[0]``
when run as a file), so the directory can be copied verbatim to
``/tmp/first_fill/`` per the runbook and still work.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from alphalens_pipeline.brokers.contract import (
    BracketOrderRequest,
    Broker,
    BrokerError,
    PlacedOrder,
)
from alphalens_pipeline.brokers.submission_log import (
    append_submission_record,
    build_submission_record,
)

SCRATCH_ENV = "SCRATCH"

_POLL_TIMEOUT_S = 60.0
_POLL_INTERVAL_S = 3.0


def scratch_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the session scratch dir: explicit arg > ``$SCRATCH`` > dated default."""
    if override is not None:
        target = Path(override)
    elif os.environ.get(SCRATCH_ENV):
        target = Path(os.environ[SCRATCH_ENV])
    else:
        today = dt.datetime.now(dt.UTC).date().isoformat()
        target = (
            Path.home() / ".alphalens" / "broker_orders" / "experiments" / f"first_fill_{today}"
        )
    target.mkdir(parents=True, exist_ok=True)
    return target


def dump_payload(scratch: Path, name: str, payload: Any) -> Path:
    """Write one raw payload as ``<scratch>/<name>.json`` (pretty, sorted)."""
    path = scratch / f"{name}.json"
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    print(f"saved {path}")
    return path


def place_and_journal(
    broker: Broker,
    *,
    ticker: str,
    mic: str,
    side: str,
    qty: int,
    entry: float,
    stop: float | None,
    tp: float | None,
    ttl: int,
    brief_date: str,
    note: str,
    scratch: Path,
    out_name: str,
    journal_path: Path | None = None,
) -> PlacedOrder | None:
    """Place ONE bracket through the gated wrapper, journal it, dump the payload.

    Returns the ``PlacedOrder`` on success. On ``BrokerError`` the failure is
    STILL journaled (note-only record, empty brackets — submit-command parity)
    and dumped, then ``None`` is returned so the caller exits non-zero.
    """
    instrument = broker.resolve_instrument(ticker, mic)
    request = BracketOrderRequest(
        instrument=instrument,
        side=side,  # type: ignore[arg-type]
        quantity=qty,
        entry_limit=entry,
        stop_loss=stop,
        take_profit=tp,
        entry_ttl_days=ttl,
        client_request_id=str(uuid.uuid4()),
    )
    request_echo = dataclasses.asdict(request)
    ts_utc = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

    try:
        placed = broker.place_bracket_order(request)
    except BrokerError as exc:
        failure_note = f"{note}; placement failed: {exc}"
        record = build_submission_record(
            brief_date=brief_date,
            ticker=ticker,
            mic=mic,
            uic=instrument.broker_instrument_id,
            brackets=[],
            note=failure_note,
        )
        journal = append_submission_record(record, path=journal_path)
        dump_payload(
            scratch,
            out_name,
            {"ts_utc": ts_utc, "request": request_echo, "error": str(exc)},
        )
        print(f"PLACEMENT FAILED (journaled to {journal}): {exc}")
        return None

    bracket_row = {
        "client_request_id": request.client_request_id,
        "entry_order_id": placed.entry_order_id,
        "exit_order_ids": list(placed.exit_order_ids),
        "qty": qty,
        "entry": entry,
        "stop": stop,
        "tp": tp,
        "ttl": ttl,
    }
    record = build_submission_record(
        brief_date=brief_date,
        ticker=ticker,
        mic=mic,
        uic=instrument.broker_instrument_id,
        brackets=[bracket_row],
        note=note,
    )
    journal = append_submission_record(record, path=journal_path)
    dump_payload(
        scratch,
        out_name,
        {
            "ts_utc": ts_utc,
            "request": request_echo,
            "placed": {
                "entry_order_id": placed.entry_order_id,
                "exit_order_ids": list(placed.exit_order_ids),
            },
            "journal_path": str(journal),
        },
    )
    print(
        f"placed entry={placed.entry_order_id} "
        f"exits={','.join(placed.exit_order_ids) or '-'} "
        f"(request {request.client_request_id}); journaled to {journal}"
    )
    return placed


def poll_until_entry_absent(
    broker: Broker,
    entry_order_id: str,
    *,
    timeout_s: float = _POLL_TIMEOUT_S,
    interval_s: float = _POLL_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> bool:
    """True when the entry left the open-orders view within the budget.

    SIM fills near-instantly but the port/v1 views lag seconds — honor the
    full state machine instead of assuming.
    """
    deadline = now() + timeout_s
    while True:
        open_ids = {state.order_id for state in broker.list_open_orders()}
        if entry_order_id not in open_ids:
            print(f"entry {entry_order_id} left the open-orders view")
            return True
        if now() >= deadline:
            print(
                f"entry {entry_order_id} STILL WORKING after {timeout_s:.0f}s — "
                "poll manually via 'alphalens broker orders' / dump_activities.py"
            )
            return False
        sleep(interval_s)


def make_default_broker() -> Broker:
    """Late-bound factory so hermetic tests never construct a real client."""
    from alphalens_pipeline.brokers.saxo.broker import create_saxo_broker_from_env

    return create_saxo_broker_from_env()


def today_iso() -> str:
    return dt.datetime.now(dt.UTC).date().isoformat()
