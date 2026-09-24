"""The brief producer: one brief row as a TradeIntent author document (#1469).

The arming door (``alphalens broker arm``) is the one way a pick enters a
broker inbox. This module is what turns a thematic brief row into the document
that door takes, so the broker group never reads a brief:

    alphalens thematic intent TICKER --date D --frame F --currency C --exit trail \\
      | alphalens broker arm - --env sim|live

The document states the TRADE and nothing the door computes: no ``intent_id``,
``armed_ts`` or ``r_multiple`` (derived), and no ``generation``, so every send is
a new pick and a re-run is refused while the first one is armed rather than
silently replacing it. ``meta.trade_date`` is the brief date, which the door
requires on a ``"brief"`` document: a brief's day 1 is the session after it.

The exit is the author's choice, stated on every call (#1530). It used to be
added silently, so an operator armed a trailing stop no card or output showed.
``"trail"`` declares the deployment's trail, read off the registry by
``build_exit_declaration`` so its numbers cannot drift; ``"none"`` declares no
stop management, which the daemon reads as "never move the stop".

A pure function over decoded brief rows. It imports nothing from
``alphalens_pipeline.brokers`` (pinned by ``test_module_dependencies.py``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from broker_contract.sizing import TradeSetupNotPlannableError
from broker_contract.trade_intent.codec import author_jsonable
from broker_contract.trade_intent.schema import ExitGeometrySpec, InstrumentHint, TradeSpec

from alphalens_pipeline.paper.brief_loader import CandidateBrief
from alphalens_pipeline.paper.sizing import (
    build_exit_declaration,
    parse_brief_to_spec,
    validate_trade_setup,
)

# A brief row names no exchange, and the brief universe is US-listed. The daemon
# resolves the real instrument across the US venues it probes together, and the
# door counts XNYS, XNAS and XASE as one venue, so this MIC is the venue class
# rather than the listing.
BRIEF_INSTRUMENT_MIC: Final = "XNYS"

_SOURCE_BRIEF: Final = "brief"
_WHOLE_FRAME_PCT: Final = 100.0

EXIT_TRAIL: Final = "trail"
EXIT_NONE: Final = "none"
EXIT_CHOICES: Final = (EXIT_TRAIL, EXIT_NONE)


class BriefIntentRefusedError(Exception):
    """This brief row cannot be written as a document."""


class BriefRowNotFoundError(BriefIntentRefusedError):
    """The brief has no row for the ticker."""


def brief_notional(pct: float, frame: float) -> float:
    """``pct`` percent of ``frame``, in the frame's currency."""
    return pct / 100.0 * frame


def _amount(trade_setup: dict, *, frame: float | None, notional: float | None) -> float:
    if (frame is None) == (notional is None):
        raise ValueError("state exactly one of frame and notional")
    pct = validate_trade_setup(trade_setup)
    if notional is not None:
        return notional
    assert frame is not None
    if not 0 < pct <= _WHOLE_FRAME_PCT:
        raise BriefIntentRefusedError(
            f"suggested_size_pct={pct:g} is outside (0, 100]: more than the whole frame"
        )
    return brief_notional(pct, frame)


def _exit_declaration(exit_policy: str) -> ExitGeometrySpec | None:
    if exit_policy == EXIT_TRAIL:
        return build_exit_declaration()
    if exit_policy == EXIT_NONE:
        return None
    raise ValueError(f"exit_policy must be one of {EXIT_CHOICES}, got {exit_policy!r}")


@dataclass(frozen=True)
class BriefIntent:
    """The document, plus the typed spec and exit it was rendered from, so a
    caller can describe the exit to the author without decoding the JSON back."""

    document: dict[str, Any]
    spec: TradeSpec
    exit: ExitGeometrySpec | None


def build_brief_intent(
    candidates: Iterable[CandidateBrief],
    *,
    ticker: str,
    brief_date: dt.date,
    currency: str,
    exit_policy: str,
    frame: float | None = None,
    notional: float | None = None,
) -> dict[str, Any]:
    """The author document for ``ticker``'s row in the ``brief_date`` brief, with its parts.

    Sized from exactly one of ``frame`` (the brief's percent of it) and
    ``notional`` (the amount as given), in ``currency``. ``exit_policy`` is one
    of :data:`EXIT_CHOICES` and has no default. Raises
    :class:`BriefRowNotFoundError` when the brief has no such ticker and
    :class:`BriefIntentRefusedError` when the row cannot be planned.
    """
    exit_declaration = _exit_declaration(exit_policy)
    rows = list(candidates)
    wanted = ticker.upper()
    candidate = next((c for c in rows if c.ticker.upper() == wanted), None)
    if candidate is None:
        raise BriefRowNotFoundError(
            f"{wanted} not in the {brief_date} brief ({len(rows)} candidates)"
        )
    if candidate.trade_setup is None:
        raise BriefIntentRefusedError(f"{wanted}: no plannable trade_setup on {brief_date}")

    try:
        amount = _amount(candidate.trade_setup, frame=frame, notional=notional)
        spec = parse_brief_to_spec(candidate.trade_setup, notional_acct=amount, currency=currency)
    except TradeSetupNotPlannableError as exc:
        raise BriefIntentRefusedError(f"{wanted}: trade_setup not plannable — {exc}") from exc

    document = {
        "instrument": author_jsonable(InstrumentHint(ticker=wanted, mic=BRIEF_INSTRUMENT_MIC)),
        "spec": author_jsonable(spec),
        "exit": None if exit_declaration is None else author_jsonable(exit_declaration),
        "meta": {"source": _SOURCE_BRIEF, "trade_date": brief_date.isoformat()},
    }
    return BriefIntent(document=document, spec=spec, exit=exit_declaration)


def brief_intent_document(
    candidates: Iterable[CandidateBrief],
    *,
    ticker: str,
    brief_date: dt.date,
    currency: str,
    exit_policy: str,
    frame: float | None = None,
    notional: float | None = None,
) -> dict[str, Any]:
    """Just the document of :func:`build_brief_intent`."""
    return build_brief_intent(
        candidates,
        ticker=ticker,
        brief_date=brief_date,
        currency=currency,
        exit_policy=exit_policy,
        frame=frame,
        notional=notional,
    ).document


__all__ = [
    "BRIEF_INSTRUMENT_MIC",
    "EXIT_CHOICES",
    "EXIT_NONE",
    "EXIT_TRAIL",
    "BriefIntent",
    "BriefIntentRefusedError",
    "BriefRowNotFoundError",
    "brief_intent_document",
    "brief_notional",
    "build_brief_intent",
]
