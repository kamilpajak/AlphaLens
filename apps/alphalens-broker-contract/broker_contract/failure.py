"""The one machine-readable failure shape reported by every broker surface (#1389).

The shape is NOT new. ``alphalens_cli/commands/broker.py`` has written
``{code, message, retryable, details, suggestions:[{argv, why}]}`` to stderr for
``stream-status``'s not-found case since #1173. What was missing is a name for
that object and a place both the CLI and the contract package can reach — so
#1404's ``validate_intent`` raises THIS instead of a second error shape invented
inside the contract.

What ``retryable`` means
------------------------
**Safe to re-run this operation without reconciling first.** Not "the cause was
transient". The two come apart in the case this package exists to get right: a
5xx after a POST has a transient cause and MUST NOT be retried, because the write
may already have landed (``brokers/saxo/client.py`` refuses exactly that:
*"request may have been sent — NOT retried (never blind-retry a POST)"*). That
case is ``write_outcome_unknown``, and it is the reason ``code`` — not
``retryable`` — is the field a client branches on.

Why a machine still gets out of a ``retryable: false``
------------------------------------------------------
A client reads ``retryable: false`` as "drop it". Where a runnable recovery
exists, the code declares :attr:`FailureCode.needs_suggestion` and every emitter
must attach a ``suggestions`` entry whose ``argv`` is a command the caller can
execute — the repo CLI doctrine's *"the agent should get a runnable next command,
not a sentence to parse"*. Without that, ``write_outcome_unknown`` silently means
"lose the intent".

Which codes live here
---------------------
Only the ones the CONTRACT owns: the broker error taxonomy in
:mod:`broker_contract.contract`, plus ``intent_invalid`` (raised by the intent
validator). The CLI's own vocabulary (``usage``, ``env_ambiguous``,
``live_refused``, ``state_layout``, ``policy_refused``, ``pick_already_armed``,
``not_found``, ``unclassified``) stays with the CLI. That split is the #1122
decision applied again: the Saxo fee card was moved out of this package because
*"the ADAPTER reports, never the contract decides"*, and a CLI concept defined
here would be inherited by a second consumer just as silently. Pinned by
``tests/brokers/test_broker_contract_has_no_cli_codes.py``.

Stdlib only, like the rest of the package (``dependencies = []`` on purpose).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

__all__ = [
    "CONTRACT_FAILURE_CODES",
    "ContractError",
    "Failure",
    "FailureCode",
    "Suggestion",
]


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One runnable next command, plus why it is the next command.

    ``argv`` is an array, never a shell string: the caller may be an agent or a
    subprocess launcher, and a sentence to parse is not a recovery path. It is a
    tuple so that a consumer which already read it cannot have it edited
    underneath.
    """

    argv: tuple[str, ...]
    why: str

    def to_jsonable(self) -> dict[str, Any]:
        return {"argv": list(self.argv), "why": self.why}


@dataclass(frozen=True, slots=True)
class Failure:
    """A refusal or breakage, in the form a machine consumer reads.

    ``details`` is DIAGNOSTIC and unstable: it carries whatever helps a human or
    a log, and its keys are not part of the contract unless the published table
    names them for that code. Only ``code`` is guaranteed stable. Stated here
    because a client that finds a useful key WILL branch on it otherwise.
    """

    code: str
    message: str
    retryable: bool
    details: Mapping[str, Any] = field(default_factory=dict)
    suggestions: tuple[Suggestion, ...] = ()

    def to_jsonable(self) -> dict[str, Any]:
        """The five doctrine keys, always all five.

        An empty ``details`` / ``suggestions`` renders as an empty container
        rather than being omitted: a consumer must never have to branch on the
        absence of a key to read a failure.
        """
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
            "suggestions": [s.to_jsonable() for s in self.suggestions],
        }


class ContractError(Exception):
    """Base for every contract-side error that carries a :class:`Failure`.

    Deliberately NOT a parent of :class:`~broker_contract.contract.BrokerError`:
    the broker taxonomy is the vendor boundary and has its own inheritance that
    adapters already rely on. The two hierarchies share the reported SHAPE, not
    an ancestor.
    """

    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.message)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class FailureCode:
    """One published code: what it means, and how a client may act on it.

    ``needs_suggestion`` marks a non-retryable code whose recovery is mechanical,
    so an emitter that attaches no ``argv`` is a defect rather than a style
    choice.
    """

    name: str
    retryable: bool
    meaning: str
    needs_suggestion: bool = False


def _registry(*codes: FailureCode) -> Mapping[str, FailureCode]:
    return MappingProxyType({code.name: code for code in codes})


CONTRACT_FAILURE_CODES: Final[Mapping[str, FailureCode]] = _registry(
    FailureCode(
        name="broker_transient",
        retryable=True,
        meaning=(
            "The broker could not be reached and the request provably never landed "
            "(network retries exhausted, or a 5xx on an idempotent verb). Re-run when "
            "the outage clears."
        ),
    ),
    FailureCode(
        name="broker_rate_limited",
        retryable=True,
        meaning="The broker's throttle stayed exhausted after retries. Re-run later.",
    ),
    FailureCode(
        name="write_outcome_unknown",
        retryable=False,
        meaning=(
            "A write failed AFTER it may have reached the broker (5xx or a network error "
            "on a non-idempotent verb). It may have landed. Reconcile against live broker "
            "state before re-running; never blind-retry."
        ),
        needs_suggestion=True,
    ),
    FailureCode(
        name="broker_auth",
        retryable=False,
        meaning="Credentials are invalid or the refresh chain is dead. Re-authenticate.",
        needs_suggestion=True,
    ),
    FailureCode(
        name="instrument_not_found",
        retryable=False,
        meaning="Instrument resolution failed for this (ticker, MIC) — a miss or an ambiguity.",
    ),
    FailureCode(
        name="order_rejected",
        retryable=False,
        meaning=(
            "The broker rejected the order. The vendor's structured error code, when the "
            "adapter could attach one, appears in details.error_code."
        ),
    ),
    FailureCode(
        name="broker_unsupported",
        retryable=False,
        meaning="This broker does not offer the capability the operation needs.",
    ),
    FailureCode(
        name="broker_failed",
        retryable=False,
        meaning="A broker failure that is neither transient, throttled, nor ambiguous.",
    ),
    FailureCode(
        name="intent_invalid",
        retryable=False,
        meaning=(
            "The submitted TradeIntent is internally inconsistent (allocations, sizing, "
            "tier vocabulary). Nothing was queued; fix the document and submit again."
        ),
    ),
)


def _assert_registry_is_self_consistent() -> None:
    """Cheap import-time guard: a retryable code must not demand a suggestion.

    The two flags answer different questions, and an entry claiming both would
    mean "just run it again, but here is the command to run instead" — which is
    the shape a client cannot act on.
    """
    for code in CONTRACT_FAILURE_CODES.values():
        if code.retryable and code.needs_suggestion:
            raise ValueError(f"{code.name}: retryable codes need no suggestion")


_assert_registry_is_self_consistent()
