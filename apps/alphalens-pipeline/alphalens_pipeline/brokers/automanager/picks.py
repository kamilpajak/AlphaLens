"""Append-only pick queue for the Saxo auto-manager.

One JSON line per `alphalens broker arm` under
~/.alphalens/broker_orders/<env>/picks.jsonl (per-environment path,
state_paths.picks_path, ADR 0016) — the durable human-intent inbox the
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

There is deliberately NO ``placed`` status: the drain decides what to place by
joining the armed picks against the submissions journal on
:func:`pick_key` / :func:`submitted_pick_keys`, so placement has exactly one
record (the submission) and this journal cannot disagree with it. The cost is
that ``picks.jsonl`` read ALONE cannot answer "what is still pending" — every
long-placed pick still reads ``armed`` forever. :func:`read_pick_fold` is the
reader's half of that join (issue #1197); `alphalens broker picks` performs it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from broker_contract.trade_intent.codec import (
    TradeIntentDecodeError,
    intent_from_jsonable,
    intent_to_jsonable,
)
from broker_contract.trade_intent.schema import TradeIntent

from alphalens_pipeline.brokers.automanager import state_paths

logger = logging.getLogger(__name__)

STATUS_ARMED = "armed"
STATUS_REFUSED = "refused"
STATUS_DISARMED = "disarmed"


def _append_record(record: dict, path: Path | None) -> None:
    """Append one JSON line (append-only; never rewrites)."""
    target = path or state_paths.picks_path()
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


def mark_disarmed(
    ticker: str, date: dt.date, *, note: str | None = None, path: Path | None = None
) -> None:
    """Append one TERMINAL 'disarmed' line retiring the (ticker, date) pick.

    The OPERATOR terminal (`alphalens broker disarm`), sibling of the daemon's
    ``mark_refused``: latest-wins retires the pick from ``iter_picks`` with no
    daemon change. Re-arming via `alphalens broker arm` is the explicit human
    path back QUEUE-side — but note the entry-trail side is stickier: a
    ``cancelled`` crid never leaves the terminal state and crids are
    deterministic per (ticker, date, tier), so a re-armed pick for the SAME
    (ticker, date) will not re-open its watch. A fresh brief date is the real
    path back."""
    _append_record(
        {
            "ticker": ticker.upper(),
            "date": date.isoformat(),
            "disarmed_ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "note": note,
            "status": STATUS_DISARMED,
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


@dataclass(frozen=True)
class PickRecord:
    """One pick's LATEST status line, decoded only as far as the fold key.

    ``record`` is the raw journal line, so a reader can surface the fields
    only some statuses carry (``reason`` on refused, ``note`` on disarmed,
    ``armed_ts`` on armed) without this module enumerating them.
    """

    ticker: str
    brief_date: dt.date
    status: str
    record: Mapping[str, Any]


@dataclass(frozen=True)
class PickFold:
    """Latest-per-(ticker, date) records + the count of malformed lines.

    Mirrors the sibling :class:`~alphalens_pipeline.brokers.automanager.entry_trails.EntryTrailFold`:
    ``malformed`` counts non-JSON / non-object / undated lines and is
    SURFACED rather than silently dropped, so a reader can say how much of
    the journal it could not account for. Blank lines are not malformed —
    they are ordinary trailing newlines in an append-only file.
    """

    records: list[PickRecord]
    malformed: int


def read_pick_fold(*, path: Path | None = None) -> PickFold:
    """Fold the journal to one :class:`PickRecord` per (ticker, date).

    The LATEST status line per key wins — the same rule :func:`iter_picks`
    applies, computed here ONCE so the queue view and the drain can never
    disagree about which line is current. Records keep first-seen key order
    (the order the CLI renders). A missing file folds empty.
    """
    target = path or state_paths.picks_path()
    if not target.exists():
        return PickFold(records=[], malformed=0)
    latest: dict[tuple[str, dt.date], dict] = {}
    malformed = 0
    with target.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            if not raw_line.strip():
                continue
            parsed = _parse_record(raw_line)
            if parsed is None:
                malformed += 1
                continue
            key, record = parsed
            latest[key] = record
    records = [
        PickRecord(
            ticker=ticker,
            brief_date=parsed_date,
            status=str(record.get("status", "")),
            record=record,
        )
        for (ticker, parsed_date), record in latest.items()
    ]
    return PickFold(records=records, malformed=malformed)


def pick_key(intent: TradeIntent) -> tuple[str, str]:
    """The (ticker, brief_date) join key for one armed intent."""
    return (str(intent.instrument.ticker).upper(), str(intent.meta.brief_date))


def submitted_pick_keys(records: Iterable[Mapping[str, Any]]) -> set[tuple[str, str]]:
    """The (ticker, brief_date) pairs already present in the submissions journal.

    Design section Data-flow step 4: the drain places only picks NOT yet
    joined to submissions.jsonl. Without this join every armed pick is
    re-submitted on every tick with a fresh client_request_id (execution.py
    mints uuid4 per bracket), which Saxo's 15 s x-request-id dedup cannot
    catch."""
    keys: set[tuple[str, str]] = set()
    for record in records:
        if record.get("tranche") == "now":
            # #1247: the now half's records (write-ahead, per-tier, refusal)
            # never retire the pick — the pullback half's record does. The
            # now half's own idempotency is the armed_ts scan in the drain.
            continue
        ticker = record.get("ticker")
        brief_date = record.get("brief_date")
        if ticker and brief_date:
            keys.add((str(ticker).upper(), str(brief_date)))
    return keys


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
    for pick in read_pick_fold(path=path).records:
        ticker, parsed_date = pick.ticker, pick.brief_date
        if pick.status != STATUS_ARMED:
            continue
        raw_intent = pick.record.get("intent")
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
    "STATUS_ARMED",
    "STATUS_DISARMED",
    "STATUS_REFUSED",
    "PickFold",
    "PickRecord",
    "arm_pick",
    "iter_picks",
    "mark_disarmed",
    "mark_refused",
    "pick_key",
    "read_pick_fold",
    "submitted_pick_keys",
]
