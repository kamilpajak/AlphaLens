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

Pick identity (#1371) is (ticker, date, generation). ``generation`` is the
same-day re-arm counter: 1 for every line written before the field existed
(the key is OMITTED on generation-1 lines, so today's journal shape is
unchanged) and 1 + the highest generation already queued for (ticker, date)
on a later `alphalens broker arm-manual`. :func:`identity_token` renders the
generation into every downstream identity string — ``pick_key`` (colon form),
the entry-watch crid, the stop refs — so a disarmed generation's terminal
markers never shadow its successor.

Queue semantics: the LATEST status line per (ticker, date, generation) wins. A terminal
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

FIRST_GENERATION = 1
_GENERATION_KEY = "generation"
_GENERATION_SUFFIX = "-g"


def _validate_generation(generation: object) -> int:
    """``generation`` as an int >= 1; ``ValueError`` for anything else (a bool
    is not a generation even though it is an int)."""
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError(f"generation must be an int >= 1, got {generation!r}")
    return generation


def identity_token(trade_date: str, generation: int) -> str:
    """The date half of every pick identity string: the bare ``trade_date`` for
    generation 1 (byte-identical to the pre-#1371 identity), ``<date>-g<N>``
    for a same-day re-arm. Only ``[A-Za-z0-9-]`` so the token is safe inside
    a Saxo ``ExternalReference`` (the entry-watch crid and stop refs carry it)."""
    generation = _validate_generation(generation)
    if generation == FIRST_GENERATION:
        return str(trade_date)
    return f"{trade_date}{_GENERATION_SUFFIX}{generation}"


def pick_key_str(ticker: str, trade_date: str, generation: int) -> str:
    """The colon-form pick key the daemon journals on ``watch_open`` /
    ``tranche_plan`` lines and `disarm` matches: ``TICKER:<identity_token>``."""
    return f"{str(ticker).upper()}:{identity_token(trade_date, generation)}"


def generation_of(record: Mapping[str, Any]) -> int:
    """The generation a journal record carries: absent / null = 1 (every line
    written before #1371); otherwise validated as an int >= 1 (``ValueError``
    for a malformed value — the caller decides whether that makes the record
    malformed or folds it conservatively)."""
    raw = record.get(_GENERATION_KEY)
    if raw is None:
        return FIRST_GENERATION
    return _validate_generation(raw)


def _generation_fields(generation: int) -> dict[str, int]:
    """The journal-line fields for ``generation``: NOTHING for generation 1
    (today's line shape stays byte-identical), the key for any later one."""
    generation = _validate_generation(generation)
    return {} if generation == FIRST_GENERATION else {_GENERATION_KEY: generation}


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
    top-level ``ticker``/``date`` (and ``generation`` when > 1, #1371) the
    latest-per-(ticker, date, generation) fold + refused correlation key on.
    """
    _append_record(
        {
            "ticker": intent.instrument.ticker.upper(),
            "date": intent.meta.trade_date,
            "armed_ts": intent.meta.armed_ts,
            "status": STATUS_ARMED,
            "intent": intent_to_jsonable(intent),
            **_generation_fields(intent.meta.generation),
        },
        path,
    )


def mark_refused(
    ticker: str,
    date: dt.date,
    reason: str,
    *,
    generation: int = FIRST_GENERATION,
    path: Path | None = None,
) -> None:
    """Append one TERMINAL 'refused' line retiring the (ticker, date, generation) pick.

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
            **_generation_fields(generation),
        },
        path,
    )


def mark_disarmed(
    ticker: str,
    date: dt.date,
    *,
    note: str | None = None,
    generation: int = FIRST_GENERATION,
    path: Path | None = None,
) -> None:
    """Append one TERMINAL 'disarmed' line retiring the (ticker, date, generation) pick.

    The OPERATOR terminal (`alphalens broker disarm`), sibling of the daemon's
    ``mark_refused``: latest-wins retires the pick from ``iter_picks`` with no
    daemon change. Re-arming via `alphalens broker arm` is the explicit human
    path back QUEUE-side — but note the entry-trail side is stickier: a
    ``cancelled`` crid never leaves the terminal state and crids are
    deterministic per (ticker, date, generation, tier), so a re-armed pick for
    the SAME identity will not re-open its watch. The path back is a NEW
    generation (`arm-manual` assigns it, #1371) or a fresh brief date."""
    _append_record(
        {
            "ticker": ticker.upper(),
            "date": date.isoformat(),
            "disarmed_ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "note": note,
            "status": STATUS_DISARMED,
            **_generation_fields(generation),
        },
        path,
    )


_PickFoldKey = tuple[str, dt.date, int]


def _parse_record(raw_line: str) -> tuple[_PickFoldKey, dict] | None:
    """One well-formed status line -> ((TICKER, date, generation), record);
    None if malformed (non-JSON, non-object, undated, or a generation that is
    not an int >= 1)."""
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
        generation = generation_of(record)
    except (KeyError, ValueError):
        return None
    return (str(record.get("ticker", "")).upper(), parsed_date, generation), record


@dataclass(frozen=True)
class PickRecord:
    """One pick's LATEST status line, decoded only as far as the fold key.

    ``record`` is the raw journal line, so a reader can surface the fields
    only some statuses carry (``reason`` on refused, ``note`` on disarmed,
    ``armed_ts`` on armed) without this module enumerating them.
    """

    ticker: str
    trade_date: dt.date
    status: str
    record: Mapping[str, Any]
    generation: int = FIRST_GENERATION

    @property
    def token(self) -> str:
        """The identity token (``2026-09-08`` / ``2026-09-08-g2``) — the
        second half of the join key and what the CLI renders as the date."""
        return identity_token(self.trade_date.isoformat(), self.generation)


@dataclass(frozen=True)
class PickFold:
    """Latest-per-(ticker, date, generation) records + the count of malformed lines.

    Mirrors the sibling :class:`~alphalens_pipeline.brokers.automanager.entry_trails.EntryTrailFold`:
    ``malformed`` counts non-JSON / non-object / undated lines and is
    SURFACED rather than silently dropped, so a reader can say how much of
    the journal it could not account for. Blank lines are not malformed —
    they are ordinary trailing newlines in an append-only file.
    """

    records: list[PickRecord]
    malformed: int


def read_pick_fold(*, path: Path | None = None) -> PickFold:
    """Fold the journal to one :class:`PickRecord` per (ticker, date, generation).

    The LATEST status line per key wins — the same rule :func:`iter_picks`
    applies, computed here ONCE so the queue view and the drain can never
    disagree about which line is current. Records keep first-seen key order
    (the order the CLI renders). A missing file folds empty.
    """
    target = path or state_paths.picks_path()
    if not target.exists():
        return PickFold(records=[], malformed=0)
    latest: dict[_PickFoldKey, dict] = {}
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
            trade_date=parsed_date,
            status=str(record.get("status", "")),
            record=record,
            generation=generation,
        )
        for (ticker, parsed_date, generation), record in latest.items()
    ]
    return PickFold(records=records, malformed=malformed)


def next_generation(ticker: str, date: dt.date, *, path: Path | None = None) -> int:
    """The generation `arm-manual` assigns to a new pick on (ticker, date):
    1 + the highest generation on ANY line of that key (whatever its status —
    a disarmed or refused generation is spent, it never comes back), 1 for a
    key the queue has never seen."""
    wanted = ticker.upper()
    highest = 0
    for record in read_pick_fold(path=path).records:
        if record.ticker == wanted and record.trade_date == date:
            highest = max(highest, record.generation)
    return highest + 1


def pick_key(intent: TradeIntent) -> tuple[str, str]:
    """The (ticker, identity token) join key for one armed intent — the token
    is the bare trade_date for generation 1, ``<date>-g<N>`` after a same-day
    re-arm (#1371), so two generations of one ticker never join to each
    other's submission."""
    # getattr: a pre-#1371 payload or a structural test double carries no
    # `generation` — the first generation, never an AttributeError in the drain.
    generation = getattr(intent.meta, "generation", FIRST_GENERATION)
    return (
        str(intent.instrument.ticker).upper(),
        identity_token(str(intent.meta.trade_date), generation),
    )


def _submission_join_key(record: Mapping[str, Any]) -> tuple[str, str] | None:
    """The (ticker, identity token) this submission record joins on, or None.

    Shared by the two questions below so they can differ in exactly ONE place —
    which records count — rather than drifting on how a key is built.
    """
    ticker = record.get("ticker")
    # #1252: the journal date key was renamed brief_date -> trade_date. Legacy
    # records on disk still carry the old key (append-only journals are never
    # rewritten), so fall back to it.
    trade_date = record.get("trade_date") or record.get("brief_date")
    if not ticker or not trade_date:
        return None
    # #1371: a malformed generation on a record that exists still proves
    # SOMETHING was submitted for the key — fold it to generation 1 rather than
    # drop it (never under-count the join).
    try:
        generation = generation_of(record)
    except ValueError:
        # DEBUG, not WARNING: the drain re-reads the journal every ~45 s tick,
        # and this line is a durable fact of the file — a WARNING per tick would
        # flood journald (iter_picks precedent).
        logger.debug(
            "submission join %s/%s: malformed generation %r — folded to "
            "generation 1 (the queue fold treats the same value as malformed)",
            ticker,
            trade_date,
            record.get(_GENERATION_KEY),
        )
        generation = FIRST_GENERATION
    return (str(ticker).upper(), identity_token(str(trade_date), generation))


def keys_with_any_submission(records: Iterable[Mapping[str, Any]]) -> set[tuple[str, str]]:
    """Every key the broker has seen an order for — the now half INCLUDED.

    A different question from :func:`submitted_pick_keys`, and the difference is
    exactly the ``tranche == "now"`` skip. That skip exists so the now half does
    not RETIRE a pick before its pullback half is placed, which is right for the
    drain and wrong for anyone asking "has anything already reached the broker
    for this key". `broker arm-intent` asks the second question before it lets a
    document replace a queued pick: a pick whose immediate tier already rests at
    the broker must not be rewritten, or the queue and the market disagree.

    Measured 2026-09-11: the SIM journal holds one key whose ONLY submission
    record is the now half, so this is a state that occurs, not a race window.
    """
    return {key for record in records if (key := _submission_join_key(record)) is not None}


def submitted_pick_keys(records: Iterable[Mapping[str, Any]]) -> set[tuple[str, str]]:
    """The (ticker, identity token) pairs whose pick the drain considers RETIRED.

    Design section Data-flow step 4: the drain places only picks NOT yet
    joined to submissions.jsonl. Without this join every armed pick is
    re-submitted on every tick with a fresh client_request_id (execution.py
    mints uuid4 per bracket), which Saxo's 15 s x-request-id dedup cannot
    catch.

    #1247: the now half's records (write-ahead, per-tier, refusal) never retire
    the pick — the pullback half's record does. The now half's own idempotency
    is the armed_ts scan in the drain. Anyone asking whether an order EXISTS for
    a key wants :func:`keys_with_any_submission` instead.
    """
    return {
        key
        for record in records
        if record.get("tranche") != "now" and (key := _submission_join_key(record)) is not None
    }


def iter_picks(*, path: Path | None = None) -> Iterator[TradeIntent]:
    """Yield ARMED picks as decoded :class:`TradeIntent`; the LATEST status
    line per (ticker, date, generation) wins.

    Malformed/undated lines are skipped, and a pick whose latest line is
    non-armed (refused / cancelled / filled / expired) is never yielded — the
    control-loop drain places whatever this emits, so the ARMED filter lives
    here (defence in depth against re-placing a retired intent). An armed line
    with no ``"intent"`` key (the pre-PR-7 bare shape) or an undecodable one
    is skipped with a warning — no back-compat, re-arm is the human path
    back."""
    for pick in read_pick_fold(path=path).records:
        ticker, parsed_date = pick.ticker, pick.trade_date
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
    "FIRST_GENERATION",
    "STATUS_ARMED",
    "STATUS_DISARMED",
    "STATUS_REFUSED",
    "PickFold",
    "PickRecord",
    "arm_pick",
    "generation_of",
    "identity_token",
    "iter_picks",
    "mark_disarmed",
    "mark_refused",
    "next_generation",
    "pick_key",
    "pick_key_str",
    "read_pick_fold",
    "submitted_pick_keys",
]
