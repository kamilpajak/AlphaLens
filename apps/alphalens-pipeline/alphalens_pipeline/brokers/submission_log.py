"""Append-only submission journal for broker order placement (P2).

One JSON line per ``broker submit --execute`` run under
``~/.alphalens/broker_orders/submissions.jsonl`` — the FIRST execution
output, and the P3 reconciler's input. Every record is stamped with
:func:`~alphalens_pipeline.brokers.execution.execution_config_version`
(ADR 0013 R3): a policy bump is a cohort boundary, existing lines are never
restamped, and analyses never pool across tokens (T8 — live fills are a new
measurement source, never merged with broker-free replays).

Record shape (frozen with the token's ``_STAMP_SCHEMA``; changing it costs a
schema bump — schema "2" ADDED the FX provenance keys, FX-leg design memo
§4.3 item 8)::

    {
        "execution_config_version": "execution-v2-...",
        "ts": "<UTC ISO-8601>",
        "brief_date": "YYYY-MM-DD",
        "ticker": "KO",
        "mic": "XNYS",          # the RESOLVED venue (routing decision)
        "uic": "307",           # broker instrument id
        "brackets": [
            {"client_request_id": ..., "entry_order_id": ..,
             "exit_order_ids": [...], "qty": .., "entry": .., "stop": ..,
             "tp": .., "ttl": ..},
            ...
        ],
        "precheck": {...},       # per-bracket precheck summary
        "sizing_currency": "EUR" | null,     # account ccy the budget was in
        "instrument_currency": "PLN" | null, # resolved instrument ccy
        "sizing_equity": 1000000.0 | null,   # equity the sizing used (acct ccy)
        "fx_rate": 4.34 | null,   # REAL null on same-currency (a fake 1.0
                                  # would masquerade as a quote); acct->instr
        "fx_rate_bid": ... | null,
        "fx_rate_ask": ... | null,
        "fx_rate_price_type": "Tradable" | null,
        "fx_rate_source": "saxo-fxspot-uic-1343-mid" | null,
        "fx_rate_asof": "<UTC ISO-8601>" | null,
        "precheck_conversion_rate": 0.2304 | null,  # Saxo's independent
                                  # InstrumentToAccountConversionRate
        "note": "...",           # optional (e.g. partial-run failure note)
    }

Forward compat: readers (``iter_submission_records`` + the reconciler) treat
schema-1 lines — which simply LACK the fx keys — as the same-currency no-op
era. The journal is never back-migrated (append-only cohort boundary).
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from alphalens_pipeline.brokers.execution import execution_config_version
from alphalens_pipeline.paper.fx import FxConversion

DEFAULT_SUBMISSIONS_PATH = Path.home() / ".alphalens" / "broker_orders" / "submissions.jsonl"


def build_submission_record(
    *,
    brief_date: str,
    ticker: str,
    mic: str,
    uic: str,
    brackets: list[dict[str, Any]],
    precheck: list[dict[str, Any]] | None = None,
    note: str | None = None,
    sizing_currency: str | None = None,
    instrument_currency: str | None = None,
    sizing_equity: float | None = None,
    fx: FxConversion | None = None,
    precheck_conversion_rate: float | None = None,
) -> dict[str, Any]:
    """Assemble one journal record, stamping the token + a UTC timestamp.

    Schema-2 shape: the fx keys are ALWAYS present. ``fx=None`` (the
    same-currency no-op, and explicit-qty callers that never sized) writes
    REAL nulls — never a fake 1.0 rate. Cross-currency callers pass the ONE
    :class:`FxConversion` their sizing used; the journal is the only place
    the sizing rate survives (ClosedPosition does not expose the settlement
    rate), so the fields are stamped verbatim.
    """
    record: dict[str, Any] = {
        "execution_config_version": execution_config_version(),
        "ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "brief_date": brief_date,
        "ticker": ticker,
        "mic": mic,
        "uic": uic,
        "brackets": brackets,
        "precheck": precheck or [],
        "sizing_currency": sizing_currency,
        "instrument_currency": instrument_currency,
        "sizing_equity": sizing_equity,
        "fx_rate": fx.rate if fx is not None else None,
        "fx_rate_bid": fx.bid if fx is not None else None,
        "fx_rate_ask": fx.ask if fx is not None else None,
        "fx_rate_price_type": fx.price_type if fx is not None else None,
        "fx_rate_source": fx.source if fx is not None else None,
        "fx_rate_asof": fx.asof.isoformat(timespec="seconds") if fx is not None else None,
        "precheck_conversion_rate": precheck_conversion_rate,
    }
    if note:
        record["note"] = note
    return record


def append_submission_record(record: dict[str, Any], *, path: Path | None = None) -> Path:
    """Append ``record`` as one JSON line (append-only journal; never rewrites)."""
    target = path or DEFAULT_SUBMISSIONS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, default=str)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return target


def iter_submission_records(
    path: Path | None = None,
    *,
    malformed: list[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield parsed journal records in append order (the P3 reconciler input).

    Read-side counterpart of :func:`append_submission_record` — the journal
    itself is never rewritten (append-only SoT; verdicts are computed at
    read time). Malformed lines (broken JSON, non-object rows) are SKIPPED,
    never fatal: one corrupt line must not hide every other bracket from
    reconciliation. Pass a ``malformed`` list to collect the skipped raw
    lines so the caller can report the count. A missing journal yields
    nothing (no submissions is a valid, honest state).
    """
    target = path or DEFAULT_SUBMISSIONS_PATH
    if not target.exists():
        return
    with target.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                record = None
            if not isinstance(record, dict):
                if malformed is not None:
                    malformed.append(line)
                continue
            yield record


__all__ = [
    "DEFAULT_SUBMISSIONS_PATH",
    "append_submission_record",
    "build_submission_record",
    "iter_submission_records",
]
