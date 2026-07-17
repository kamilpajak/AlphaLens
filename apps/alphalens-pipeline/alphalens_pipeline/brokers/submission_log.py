"""Append-only submission journal for broker order placement (P2).

One JSON line per ``broker submit --execute`` run under
``~/.alphalens/broker_orders/submissions.jsonl`` — the FIRST execution
output, and the P3 reconciler's input. Every record is stamped with
:func:`~alphalens_pipeline.brokers.execution.execution_config_version`
(ADR 0013 R3): a policy bump is a cohort boundary, existing lines are never
restamped, and analyses never pool across tokens (T8 — live fills are a new
measurement source, never merged with broker-free replays).

Record shape (frozen with the P2 token; changing it costs a schema bump)::

    {
        "execution_config_version": "execution-v1-...",
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
        "note": "...",           # optional (e.g. partial-run failure note)
    }
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from alphalens_pipeline.brokers.execution import execution_config_version

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
) -> dict[str, Any]:
    """Assemble one journal record, stamping the token + a UTC timestamp."""
    record: dict[str, Any] = {
        "execution_config_version": execution_config_version(),
        "ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "brief_date": brief_date,
        "ticker": ticker,
        "mic": mic,
        "uic": uic,
        "brackets": brackets,
        "precheck": precheck or [],
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


__all__ = [
    "DEFAULT_SUBMISSIONS_PATH",
    "append_submission_record",
    "build_submission_record",
]
