"""What the arming door computes so an author does not have to (#1468).

An author writes the trade: instrument, ladders, stop, size, `meta.source`. The
door derives what is identity or label, because several of those fields steer
behaviour and a hand-computed value is how they go wrong:

* ``meta.armed_ts`` is the idempotency key of the immediate tier (the drain's
  ``_now_already_done`` matches it), so a fresh value on a retry re-sends it.
* ``meta.trade_date`` anchors the day-1 gap gate.
* ``meta.generation`` is pick identity; the queue keeps the latest line per key.
* ``intent_id``, tags and ``r_multiple`` are labels, and a non-finite
  ``r_multiple`` used to leave a take-profit ladder unmanaged.

Everything here is pure: the caller reads the pick fold and the submissions
journal once and passes them in, with the clock. The Typer command maps each
refusal class to its published failure code; the codes themselves are CLI
knowledge and do not appear in this module.

Residual, as for every arming path: the fold is read and then appended to, so
two submitters racing on one ticker can both pass (TOCTOU).
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, cast

from broker_contract.sizing import planned_blended_entry_from_spec
from broker_contract.trade_intent.schema import TradeIntent

from alphalens_pipeline.brokers.automanager.picks import STATUS_ARMED, PickRecord
from alphalens_pipeline.data.alt_data.saxo_exchanges import US_MIC_PROBE_ORDER
from alphalens_pipeline.paper.calendar import session_not_closed

# LEGACY(source_brief) — see broker_contract.trade_intent.legacy
_SOURCE_BRIEF = "brief"
_SOURCE_MANUAL = "manual"
_US_VENUE = "US"


class DoorRefusalError(Exception):
    """A document the door will not arm. ``reason`` is the published
    ``details.reason`` for codes that carry one, ``None`` otherwise."""

    reason: ClassVar[str | None] = None

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class DerivedFieldSuppliedError(DoorRefusalError):
    reason = "derived_field_supplied"


class TradeDateRequiredError(DoorRefusalError):
    reason = "trade_date_required"


class GenerationMalformedError(DoorRefusalError):
    reason = "undecodable"


class TradeDateMalformedError(DoorRefusalError):
    reason = "trade_date_malformed"


class PickAlreadyArmedError(DoorRefusalError):
    pass


class GenerationSpentError(DoorRefusalError):
    reason = "generation_spent"


class AlreadyPlacedError(DoorRefusalError):
    reason = "already_placed"


REFUSALS: tuple[type[DoorRefusalError], ...] = (
    DerivedFieldSuppliedError,
    TradeDateRequiredError,
    GenerationMalformedError,
    TradeDateMalformedError,
    PickAlreadyArmedError,
    GenerationSpentError,
    AlreadyPlacedError,
)


def supplied_derived_paths(document: Any) -> list[str]:
    """Every derived field the author sent, as a path.

    Walked defensively: a container of the wrong kind is the schema's refusal to
    make, with a better message than this one could give.
    """
    if not isinstance(document, Mapping):
        return []
    paths: list[str] = []
    if "intent_id" in document:
        paths.append("intent_id")
    meta = document.get("meta")
    if isinstance(meta, Mapping) and "armed_ts" in meta:
        paths.append("meta.armed_ts")
    spec = document.get("spec")
    tranches = spec.get("tp_tranches") if isinstance(spec, Mapping) else None
    if isinstance(tranches, list):
        paths.extend(
            f"spec.tp_tranches[{index}].r_multiple"
            for index, tranche in enumerate(tranches)
            if isinstance(tranche, Mapping) and "r_multiple" in tranche
        )
    return paths


def refuse_derived_fields(document: Any) -> None:
    paths = supplied_derived_paths(document)
    if paths:
        raise DerivedFieldSuppliedError(
            f"{', '.join(paths)} — the door computes these; remove them from the document",
            paths=paths,
        )


def check_identity_shapes(document: Mapping[str, Any]) -> None:
    """The two identity fields in a form the derivation can use.

    Runs after the JSON Schema and before anything is derived. The schema calls
    ``1.0`` an integer, and the identity strings built from ``generation`` need a
    real one (#1371); a ``trade_date`` is only a string to the schema.
    """
    meta = document["meta"]
    if "generation" in meta:
        generation = meta["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise GenerationMalformedError(
                f"meta.generation must be an integer >= 1, got {generation!r}",
            )
    if "trade_date" in meta:
        _parsed_trade_date(meta["trade_date"])


def _parsed_trade_date(raw: Any) -> dt.date:
    try:
        return dt.date.fromisoformat(str(raw))
    except ValueError as exc:
        raise TradeDateMalformedError(
            f"meta.trade_date {raw!r} is not a YYYY-MM-DD date — the queue fold would read "
            "the armed line as malformed and never drain it",
            value=raw,
        ) from exc


@dataclass(frozen=True)
class Completion:
    """The author's document with every derived and filled field in place."""

    document: dict[str, Any]
    trade_date: dt.date
    generation: int
    # The armed, unplaced line this document replaces, or None for a new pick.
    replaces: PickRecord | None


def _venue_class(mic: Any) -> str:
    return _US_VENUE if mic in US_MIC_PROBE_ORDER else str(mic)


def _recorded_mic(record: PickRecord) -> Any:
    intent = record.record.get("intent")
    instrument = intent.get("instrument") if isinstance(intent, Mapping) else None
    return instrument.get("mic") if isinstance(instrument, Mapping) else None


def _already_armed(
    blocking: PickRecord, *, ticker: str, env: str, generation: int | None
) -> PickAlreadyArmedError:
    date = blocking.trade_date.isoformat()
    return PickAlreadyArmedError(
        f"{ticker} @ {blocking.token} is still armed (generation {blocking.generation}) — "
        "arming beside it would run two live picks on one instrument; run "
        f"`alphalens broker disarm {ticker} --date {date} --env {env}` first",
        armed_trade_date=date,
        armed_generation=blocking.generation,
        submitted_generation=generation,
    )


def _resolved_trade_date(
    meta: Mapping[str, Any], *, source: Any, mic: Any, now_utc: dt.datetime
) -> dt.date:
    """A stated ``trade_date`` as stated; otherwise the next session of ``mic`` that
    has not closed. A brief document must state it."""
    if "trade_date" in meta:
        return _parsed_trade_date(meta["trade_date"])
    if source == _SOURCE_BRIEF:  # LEGACY(source_brief)
        raise TradeDateRequiredError(
            'meta.trade_date is required on a "brief" document: day 1 of a brief pick is '
            "the session after its brief date, which the door cannot know",
        )
    return session_not_closed(now_utc, mic)


def _next_generation(same_day: Sequence[PickRecord], *, ticker: str, env: str) -> int:
    """The generation a document that states none arms under — refused while any
    generation of that day is still armed."""
    live = [record for record in same_day if record.status == STATUS_ARMED]
    if live:
        raise _already_armed(
            max(live, key=lambda record: record.generation),
            ticker=ticker,
            env=env,
            generation=None,
        )
    return 1 + max((record.generation for record in same_day), default=0)


def _stated_generation_target(
    same_day: Sequence[PickRecord],
    generation: int,
    *,
    ticker: str,
    placed_keys: Collection[tuple[str, str]],
    env: str,
) -> PickRecord | None:
    """The armed, unplaced line a stated generation replaces, or None for a new one."""
    current = next((record for record in same_day if record.generation == generation), None)
    if current is not None and current.status != STATUS_ARMED:
        raise GenerationSpentError(
            f"{ticker} @ {current.token} is {current.status} — a spent generation never "
            "comes back; arm the next generation instead",
            pick_key=current.token,
            status=current.status,
        )
    if current is not None and (ticker, current.token) in placed_keys:
        raise AlreadyPlacedError(
            f"{ticker} @ {current.token} is already placed — the drain skips keys it has "
            "submitted, so replacing this line would change the queue and not the market",
            pick_key=current.token,
        )
    siblings = [
        record
        for record in same_day
        if record.generation != generation and record.status == STATUS_ARMED
    ]
    if siblings:
        raise _already_armed(
            max(siblings, key=lambda record: record.generation),
            ticker=ticker,
            env=env,
            generation=generation,
        )
    return current


def _refuse_other_armed_date(
    same_ticker: Sequence[PickRecord],
    *,
    ticker: str,
    venue: str,
    trade_date: dt.date,
    generation: int,
    stated: Any,
    placed_keys: Collection[tuple[str, str]],
    env: str,
) -> None:
    """Refuse while an armed, unplaced pick on the same ticker and venue waits
    under any other key."""
    blockers = [
        record
        for record in same_ticker
        if record.status == STATUS_ARMED
        and (record.ticker, record.token) not in placed_keys
        and (record.trade_date, record.generation) != (trade_date, generation)
        and (_recorded_mic(record) is None or _venue_class(_recorded_mic(record)) == venue)
    ]
    if blockers:
        raise _already_armed(
            max(blockers, key=lambda record: (record.trade_date, record.generation)),
            ticker=ticker,
            env=env,
            generation=stated,
        )


def complete(
    document: Mapping[str, Any],
    *,
    now_utc: dt.datetime,
    records: Sequence[PickRecord],
    placed_keys: Collection[tuple[str, str]],
    env: str,
) -> Completion:
    """Derive identity and labels, or refuse.

    ``records`` is the pick fold and ``placed_keys`` the keys with ANY submission
    (``keys_with_any_submission``, the now half included). The input document is
    left untouched.
    """
    meta = document["meta"]
    ticker = str(document["instrument"]["ticker"]).strip().upper()
    mic = document["instrument"]["mic"]
    source = meta["source"]

    trade_date = _resolved_trade_date(meta, source=source, mic=mic, now_utc=now_utc)

    same_ticker = [record for record in records if record.ticker == ticker]
    same_day = [record for record in same_ticker if record.trade_date == trade_date]
    stated = meta.get("generation")
    replaces: PickRecord | None = None

    if stated is None:
        generation = _next_generation(same_day, ticker=ticker, env=env)
    else:
        generation = int(stated)
        replaces = _stated_generation_target(
            same_day, generation, ticker=ticker, placed_keys=placed_keys, env=env
        )

    _refuse_other_armed_date(
        same_ticker,
        ticker=ticker,
        venue=_venue_class(mic),
        trade_date=trade_date,
        generation=generation,
        stated=stated,
        placed_keys=placed_keys,
        env=env,
    )

    completed = copy.deepcopy(dict(document))
    completed_meta = completed["meta"]
    completed_meta["trade_date"] = trade_date.isoformat()
    completed_meta["generation"] = generation
    completed_meta["armed_ts"] = _armed_ts(replaces, now_utc)
    marker = ":manual" if source == _SOURCE_MANUAL else ""
    suffix = "" if generation == 1 else f"-g{generation}"
    completed["intent_id"] = f"{ticker}:{trade_date.isoformat()}{marker}{suffix}"
    _fill_tags(completed["spec"]["entry_tiers"], prefix="T")
    _fill_tags(completed["spec"]["tp_tranches"], prefix="TP")
    return Completion(
        document=completed, trade_date=trade_date, generation=generation, replaces=replaces
    )


def _armed_ts(replaces: PickRecord | None, now_utc: dt.datetime) -> str:
    """A replace keeps the timestamp of the line it replaces: the immediate
    tier's idempotency is keyed on it, so a new value would re-send that tier."""
    if replaces is not None:
        kept = replaces.record.get("armed_ts")
        if not kept:
            intent = replaces.record.get("intent")
            meta = intent.get("meta") if isinstance(intent, Mapping) else None
            kept = meta.get("armed_ts") if isinstance(meta, Mapping) else None
        if kept:
            return str(kept)
    return now_utc.astimezone(dt.UTC).isoformat(timespec="seconds")


def _fill_tags(rungs: list[Any], *, prefix: str) -> None:
    for index, rung in enumerate(rungs, start=1):
        if isinstance(rung, dict) and "tag" not in rung:
            rung["tag"] = f"{prefix}{index}"


def with_r_multiples(intent: TradeIntent) -> TradeIntent:
    """Every take-profit tranche labelled with its distance in R.

    ``(price - blend) / (blend - stop)``, the blend being the alloc-weighted
    planned entry the daemon and the replay lenses use (#1114). Call it only
    after ``validate_intent``: the stop sits below every tier there, so the
    blend is above the stop and the value is finite.
    """
    spec = intent.spec
    blend = planned_blended_entry_from_spec(spec)
    if blend is None:  # unreachable after validate_intent: every tier is priced
        raise ValueError("the entry ladder yields no planned blend")
    risk = blend - spec.disaster_stop
    tranches = tuple(
        dataclasses.replace(tranche, r_multiple=(tranche.price - blend) / risk)
        for tranche in spec.tp_tranches
    )
    return cast(
        "TradeIntent",
        dataclasses.replace(intent, spec=dataclasses.replace(spec, tp_tranches=tranches)),
    )


def tier_amounts(intent: TradeIntent) -> list[float]:
    """The account-currency amount each entry tier spends."""
    notional = intent.spec.size.notional_acct
    return [notional * tier.alloc_pct / 100.0 for tier in intent.spec.entry_tiers]


__all__ = [
    "REFUSALS",
    "AlreadyPlacedError",
    "Completion",
    "DerivedFieldSuppliedError",
    "DoorRefusalError",
    "GenerationMalformedError",
    "GenerationSpentError",
    "PickAlreadyArmedError",
    "TradeDateMalformedError",
    "TradeDateRequiredError",
    "check_identity_shapes",
    "complete",
    "refuse_derived_fields",
    "supplied_derived_paths",
    "tier_amounts",
    "with_r_multiples",
]
