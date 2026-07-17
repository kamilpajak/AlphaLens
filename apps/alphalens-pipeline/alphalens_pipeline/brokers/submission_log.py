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
from collections.abc import Iterator
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
