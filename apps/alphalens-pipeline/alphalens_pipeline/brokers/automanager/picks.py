"""Append-only pick queue for the Saxo auto-manager.

One JSON line per `alphalens broker arm` under
~/.alphalens/broker_orders/picks.jsonl — the durable human-intent inbox the
control loop drains. Mirrors submission_log.py: the file is NEVER rewritten;
status is a recorded fact per line (T8 cohort discipline). Malformed/undated
lines are skipped; a missing file yields nothing.

PR-7 (broker-manager extraction memo section 5): ``arm_pick`` now persists the
FULL :class:`~broker_contract.trade_intent.schema.TradeIntent` on the armed
line (under the ``"intent"`` key) — ``arm_command`` already parses the brief
into a ``TradeIntent`` at arm time, and ``iter_picks`` decodes it back so the
daemon never touches a brief. No back-compat for the old bare
(ticker, date) armed line shape (solo-project doctrine): an armed line missing
the ``"intent"`` key, or carrying an undecodable one, is skipped exactly like
any other malformed line — re-arming via `alphalens broker arm` is the
explicit human path back.

Queue semantics: the LATEST status line per (ticker, date) wins. A terminal
``refused`` line (capacity/cap safety refusal) retires the pick so the drain
never retries it — re-arming via `alphalens broker arm` appends a fresh armed
line and is the explicit human path back.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterator
from pathlib import Path

from broker_contract.trade_intent.codec import (
    TradeIntentDecodeError,
    intent_from_jsonable,
    intent_to_jsonable,
)
from broker_contract.trade_intent.schema import TradeIntent

logger = logging.getLogger(__name__)

DEFAULT_PICKS_PATH = Path.home() / ".alphalens" / "broker_orders" / "picks.jsonl"

STATUS_ARMED = "armed"
STATUS_REFUSED = "refused"


def _append_record(record: dict, path: Path | None) -> None:
    """Append one JSON line (append-only; never rewrites)."""
    target = path or DEFAULT_PICKS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def arm_pick(intent: TradeIntent, *, path: Path | None = None) -> None:
    """Append one 'armed' intent line (append-only; never rewrites).

    Persists the full ``intent`` under the ``"intent"`` key (PR-7) plus the
    top-level ``ticker``/``date`` the latest-per-(ticker,date) fold + refused
    correlation key on.
    """
    _append_record(
        {
            "ticker": intent.instrument.ticker.upper(),
            "date": intent.meta.brief_date,
            "armed_ts": intent.meta.armed_ts,
            "status": STATUS_ARMED,
            "intent": intent_to_jsonable(intent),
        },
        path,
    )


def mark_refused(ticker: str, date: dt.date, reason: str, *, path: Path | None = None) -> None:
    """Append one TERMINAL 'refused' line retiring the (ticker, date) pick.

    Written when safety.check refuses placement (open-legs cap / portfolio
    gross cap) — without it the armed pick retries every tick and self-places
    a stale brief signal days later once capacity frees. Re-arming via
    `alphalens broker arm` is the explicit human path back."""
    _append_record(
        {
            "ticker": ticker.upper(),
            "date": date.isoformat(),
            "refused_ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "reason": reason,
            "status": STATUS_REFUSED,
        },
        path,
    )


def _parse_record(raw_line: str) -> tuple[tuple[str, dt.date], dict] | None:
    """One well-formed status line -> ((TICKER, date), record); None if malformed."""
    line = raw_line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    try:
        parsed_date = dt.date.fromisoformat(str(record["date"]))
    except (KeyError, ValueError):
        return None
    return (str(record.get("ticker", "")).upper(), parsed_date), record


def iter_picks(*, path: Path | None = None) -> Iterator[TradeIntent]:
    """Yield ARMED picks as decoded :class:`TradeIntent`; the LATEST status
    line per (ticker, date) wins.

    Malformed/undated lines are skipped, and a pick whose latest line is
    non-armed (refused / cancelled / filled / expired) is never yielded — the
    control-loop drain places whatever this emits, so the ARMED filter lives
    here (defence in depth against re-placing a retired intent). An armed line
    with no ``"intent"`` key (the pre-PR-7 bare shape) or an undecodable one
    is skipped with a warning — no back-compat, re-arm is the human path
    back."""
    target = path or DEFAULT_PICKS_PATH
    if not target.exists():
        return
    latest: dict[tuple[str, dt.date], dict] = {}
    with target.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            parsed = _parse_record(raw_line)
            if parsed is None:
                continue
            key, record = parsed
            latest[key] = record
    for (ticker, parsed_date), record in latest.items():
        if str(record.get("status", "")) != STATUS_ARMED:
            continue
        raw_intent = record.get("intent")
        if raw_intent is None:
            # DEBUG, not WARNING: the manager daemon re-reads picks.jsonl every
            # ~45s tick, so a WARNING per bare line per tick floods the journal
            # for an expected, inert, self-healing condition (a pre-PR-7 armed
            # line is skipped forever until re-armed — never placed). Keep it at
            # DEBUG so troubleshooting can still surface it on demand.
            logger.debug(
                "iter_picks %s/%s: armed line has no 'intent' (pre-PR-7 bare shape) — "
                "skipped, re-arm via `alphalens broker arm`",
                ticker,
                parsed_date,
            )
            continue
        try:
            yield intent_from_jsonable(raw_intent)
        except TradeIntentDecodeError as exc:
            logger.warning(
                "iter_picks %s/%s: armed line's intent failed to decode — skipped: %s",
                ticker,
                parsed_date,
                exc,
            )
            continue


__all__ = [
    "DEFAULT_PICKS_PATH",
    "STATUS_ARMED",
    "STATUS_REFUSED",
    "arm_pick",
    "iter_picks",
    "mark_refused",
]
