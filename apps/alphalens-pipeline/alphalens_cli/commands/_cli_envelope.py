"""The CLI's JSON envelope and failure contract, shared by the broker-family groups.

Moved verbatim out of ``commands/broker.py`` (#1689) when a second group —
``alphalens cashbook`` — needed the same envelope and the same failure
reporting. Nothing here is new: the five blocks are the ``--format`` vocabulary
and the exit-status set, the CLI-owned failure-code registry and its
suggestions, the per-invocation output-format handle, the five failure
emitters, and the ``--format`` option with the envelope / render / emit trio.

**Why a shared module rather than an import from ``broker.py``.** A group that
imported the other group's module would pay for its whole command surface and
would make the two groups' import order load-bearing. The house precedent for
an underscore-prefixed shared CLI helper is ``commands/_openrouter_gate.py``.

**The code registry stays SPLIT.** ``broker_contract/failure.py`` owns the nine
codes the contract owns; this module owns the CLI concepts (#1122: the ADAPTER
reports, never the contract decides). The published table in
``apps/alphalens-broker-contract/README.md`` covers both halves and is gated
against both registries.

**``_OUTPUT_FORMAT`` is a ContextVar OBJECT, and that is why the group
callbacks can stay in their own modules.** Each group's Typer callback calls
``.set()`` on the one object the emitters here read, so a reset in
``commands/broker.py`` or ``commands/cashbook.py`` reaches this module's
``_fail_with`` without either group owning the handle.

Lazy-CLI budget: this module imports ``typer`` and ``broker_contract.failure``
only, both of which every consumer already pays for at module level with the
measured note in ``commands/broker.py``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import typer
from broker_contract.failure import CONTRACT_FAILURE_CODES, Failure, FailureCode, Suggestion

logger = logging.getLogger(__name__)

# The two `--format` values, defined here because the failure emitter below is
# the first thing in the module that needs them (the `_FORMAT_OPTION` help
# text further down is their other consumer).
_FORMAT_HUMAN = "human"
_FORMAT_JSON = "json"

# Exit statuses, from the repo CLI doctrine (0 ok / 2 usage / 4 not found /
# 7 transient). They live CLI-side, not in `broker_contract`: a process status is
# a property of running a command, not of the contract (#1122 split, #1389).
_EXIT_FAILED = 1
_EXIT_USAGE = 2
_EXIT_NOT_FOUND = 4
_EXIT_TRANSIENT = 7

# Codes this CLI owns. The broker taxonomy's own codes come from the contract
# package; these are the refusals a CLI makes and a library never would.
_CLI_FAILURE_CODES: Mapping[str, FailureCode] = {
    code.name: code
    for code in (
        FailureCode(
            name="usage",
            retryable=False,
            meaning="The invocation is malformed (unknown option value, bad date, bad bound).",
        ),
        FailureCode(
            name="env_ambiguous",
            retryable=False,
            meaning=(
                "The shell names one broker instance and the command defaults to another, "
                "and the command writes to a queue. Pass --env explicitly."
            ),
        ),
        FailureCode(
            name="live_refused",
            retryable=False,
            meaning=(
                "A LIVE broker could not be built: the LIVE rails or auth surface "
                "are not present in this process (ADR 0017)."
            ),
        ),
        FailureCode(
            name="state_layout",
            retryable=False,
            meaning=(
                "Durable broker state is still in the pre-migration flat layout "
                "(ADR 0016 D4); the command refuses rather than treat itself as having "
                "no prior state."
            ),
        ),
        FailureCode(
            name="pick_already_armed",
            retryable=False,
            meaning=(
                "Another pick on this ticker is still armed: a live generation of the same "
                "(ticker, trade date), or an unplaced pick on the same venue under any "
                "date. Disarm it first; a re-arm then takes the next generation."
            ),
            needs_suggestion=True,
        ),
        FailureCode(
            name="pick_not_writable",
            retryable=False,
            meaning=(
                "This pick key cannot take a write: the generation was disarmed or "
                "refused (a spent generation never comes back), or the daemon has "
                "already placed it, so a replacement would change the queue and not "
                "the market. `details.reason` names which."
            ),
            needs_suggestion=True,
        ),
        FailureCode(
            name="intent_malformed",
            retryable=False,
            meaning=(
                "The submitted document does not match the published input contract: "
                "not JSON, a duplicate key, a field the door derives, an unsupported "
                "schema_version, a schema violation, a brief without its trade date, "
                "undecodable, or carrying a key the decoder would discard. "
                "`details.reason` names which."
            ),
        ),
        FailureCode(
            name="venue_unsupported",
            retryable=False,
            meaning=(
                "The document is well formed; this deployment does not trade that "
                "MIC. A venue list is broker-deployment knowledge, so it is never a "
                "document rule (#1122, #1404)."
            ),
        ),
        FailureCode(
            name="queue_write_failed",
            retryable=True,
            meaning=(
                "Appending to an append-only journal failed (disk full, permissions) — "
                "a broker queue journal, or the account cash book. No broker order can "
                "be in flight either way: the commands that report this either write "
                "only to the queue or place nothing at all, and the cash book's append "
                "order leaves a partial sweep a re-sweep closes, so the same command "
                "may simply be re-run once the cause clears."
            ),
        ),
        FailureCode(
            name="cashbook_sync_refused",
            retryable=False,
            meaning=(
                "The sweep will not read or store this account's cash. "
                "`details.reason` names which rule refused it."
            ),
        ),
        FailureCode(
            name="cashbook_sweep_busy",
            retryable=True,
            meaning=(
                "Another sweep holds this instance's cash-book lock. Nothing was "
                "appended, so re-running is safe. `details.lock` names the file."
            ),
        ),
        FailureCode(
            name="stream_metrics_missing",
            retryable=False,
            meaning=(
                "The stream gauge textfile does not exist for this instance — the daemon "
                "never ticked with streaming on, or --env names another instance."
            ),
            needs_suggestion=True,
        ),
        FailureCode(
            name="unclassified",
            retryable=False,
            meaning=(
                "A refusal that has not been given a code yet. NOT a branch condition: "
                "a site graduating out of this to a specific code is expected and is not "
                "a breaking change. Treat it as 'a human must look'."
            ),
        ),
    )
}

_FAILURE_CODES: Mapping[str, FailureCode] = {**CONTRACT_FAILURE_CODES, **_CLI_FAILURE_CODES}

# The runnable recovery for every code that declares `needs_suggestion`. Held
# here rather than at the call sites so the guarantee is structural: a machine
# reads `retryable: false` as "drop it", and a code that promises a way out must
# not depend on each emitter remembering to attach one.
_SUGGESTIONS_BY_CODE: Mapping[str, tuple[Suggestion, ...]] = {
    "write_outcome_unknown": (
        Suggestion(
            argv=("alphalens", "broker", "orders", "--format", "json"),
            why=(
                "the write may already rest at the broker; read live order state before "
                "re-running, never blind-retry"
            ),
        ),
    ),
    "broker_auth": (
        Suggestion(
            argv=("alphalens", "broker", "auth"),
            why="the OAuth refresh chain is dead or absent; re-authenticate",
        ),
    ),
    "pick_already_armed": (
        Suggestion(
            argv=("alphalens", "broker", "picks", "--format", "json"),
            why="read which generation is still armed, then disarm it before re-arming",
        ),
    ),
    "pick_not_writable": (
        Suggestion(
            argv=("alphalens", "broker", "picks", "--format", "json"),
            why="read the state of this (ticker, date, generation) before rewriting it",
        ),
    ),
    "stream_metrics_missing": (
        Suggestion(
            argv=("systemctl", "--user", "status", "alphalens-broker-manager.service"),
            why=(
                "the daemon writes the gauges every tick only while it runs with "
                "ALPHALENS_BROKER_STREAMING_ENABLED=1"
            ),
        ),
    ),
}

# How this invocation renders a failure. CLI-ONLY: `broker manage` imports this
# module, and the daemon must never set this — it has no `--format`, its output
# is the journal and the log, and a daemon that flipped this would change how
# every subsequent in-process refusal renders. A module handle rather than a parameter
# because `_fail` is reached from helpers that never see the command's options
# (`_guard_state_layout`, `_apply_env_option`), and threading it through 81 call
# sites would be a worse trade. Set by `_resolve_format`, which every command
# with `--format` calls BEFORE anything that can fail; reset per invocation by
# the group callback so one command's JSON mode cannot leak into the next
# in-process caller.
_OUTPUT_FORMAT: ContextVar[str] = ContextVar("broker_output_format", default=_FORMAT_HUMAN)


def _exit_code_for(entry: FailureCode) -> int:
    """Map a failure code onto the small documented exit-status set.

    Deliberately COARSE: the domain detail belongs in `error.code`, so this set
    does not grow one status per code. `retryable` picks 7 because "wait and
    run it again" is the one thing a shell caller can act on without parsing.
    """
    if entry.name == "usage":
        return _EXIT_USAGE
    if entry.name == "stream_metrics_missing":
        return _EXIT_NOT_FOUND
    return _EXIT_TRANSIENT if entry.retryable else _EXIT_FAILED


def _fail_with(
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
    suggestions: tuple[Suggestion, ...] = (),
) -> typer.Exit:
    """Report a classified failure and return the Exit the caller raises.

    Rendering follows the resolved `--format` and nothing else: prose for a
    human, exactly the failure object on stderr for a machine. stdout is
    untouched either way, so a failed JSON-mode command leaves it empty.
    """
    # Never raise from the error path: a code that is somehow not registered
    # must still produce a readable refusal rather than a KeyError traceback on
    # top of whatever already went wrong. The AST gate in
    # `test_broker_failure_contract_cli.py` is what keeps this from happening —
    # but the gate only runs in CI, so the fallback also says so out loud rather
    # than quietly publishing `unclassified` for what is really a typo.
    entry = _FAILURE_CODES.get(code)
    if entry is None:
        logger.warning("unregistered failure code %r — reporting it as unclassified", code)
        entry = _FAILURE_CODES["unclassified"]
    failure = Failure(
        code=entry.name,
        message=message,
        retryable=entry.retryable,
        details=dict(details or {}),
        suggestions=suggestions or _SUGGESTIONS_BY_CODE.get(entry.name, ()),
    )
    if _OUTPUT_FORMAT.get() == _FORMAT_JSON:
        typer.echo(json.dumps(failure.to_jsonable()), err=True)
    else:
        typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code=_exit_code_for(entry))


def _fail(message: str) -> typer.Exit:
    """A refusal that has not been given a code yet.

    Kept so classification stays incremental instead of being a precondition for
    the contract to exist. A machine caller still gets an object rather than
    prose; it just gets `unclassified`, which the published table tells it never
    to branch on.
    """
    return _fail_with("unclassified", message)


def _fail_from_broker_error(exc: Exception, context: str) -> typer.Exit:
    """Classify a broker-taxonomy exception by its class, not by the call site.

    Most refusals in this module are `_fail(str(exc))` pass-throughs, so the code
    is a property of what was raised. `failure_code` is a ClassVar on the base,
    so an exception raised outside an adapter boundary still classifies.
    """
    code = getattr(type(exc), "failure_code", "unclassified")
    details: dict[str, Any] = {}
    vendor_code = getattr(exc, "error_code", None)
    if vendor_code is not None:
        details["error_code"] = vendor_code
    return _fail_with(code, f"{context}: {exc}", details=details)


def _queue_write_failed(exc: Exception, *, journal: Path) -> typer.Exit:
    """A journal append failed, so nothing was queued (#1421).

    Classified off the exception CLASS like the rest of this group, and shared
    by the four queue-writing commands so they cannot drift into two answers
    for one event. Retryable: these commands touch only the queue, and the
    shared appender repairs a predecessor torn by the failed write, so the
    retry does not land on damaged bytes.

    ``alphalens cashbook sync`` reports the same CODE and deliberately does not
    call this helper (#1689). It writes no queue, so this message would name the
    wrong store; it distinguishes a failed journal append from a lock it could
    not take, which one message cannot carry; and its ``details.journal`` is
    ``None`` when the failure named no file, which a ``Path`` parameter cannot
    express. The published row in ``apps/alphalens-broker-contract/README.md``
    covers both spellings, and that row is what keeps them one answer.
    """
    return _fail_with(
        "queue_write_failed",
        f"could not append to the queue journal: {exc}",
        details={"journal": str(journal)},
    )


_FORMAT_OPTION = typer.Option(
    None,
    "--format",
    help="Output format: human|json (json = exactly one JSON value on stdout). [default: human]",
)
"""One shared option object for every command that can render JSON (#1379).

The default is ``None`` rather than ``"human"`` so ``_resolve_format`` can tell
"the caller asked for human" from "the caller said nothing" — which is what
makes the ``--json`` alias below refusable instead of silently overridden.
"""


def _resolve_format(output_format: str | None, *, json_alias: bool = False) -> str:
    """Settle ``--format`` and the legacy ``--json`` alias into one value.

    ``--json`` shipped on ``reconcile`` / ``reconcile-fills`` before the group
    had ``--format``, and the runbook plus the first-fill memo call it, so it
    stays as a documented alias (the CLI doctrine allows exactly that).
    ``--json`` together with ``--format human`` is refused rather than resolved
    by a precedence rule nobody would remember.

    Scope of that refusal: click collapses a repeated scalar option to its LAST
    value before this function runs, so ``--format human --format json --json``
    is accepted and renders JSON — the earlier ``human`` is already gone. Only
    the effective ``--format`` can be checked here.
    """
    if output_format is not None and output_format not in (_FORMAT_HUMAN, _FORMAT_JSON):
        # Rendered as PROSE even though the caller may have meant JSON: the value
        # they passed is not a format this CLI knows, so there is no format to
        # honour. The only place in the group where that is the right answer.
        raise _fail_with(
            "usage",
            f"unknown --format {output_format!r} (expected human|json)",
            details={"option": "--format", "value": output_format},
        )
    if json_alias:
        if output_format == _FORMAT_HUMAN:
            raise _fail_with(
                "usage", "--json and --format human ask for different things — pass one"
            )
        _OUTPUT_FORMAT.set(_FORMAT_JSON)
        return _FORMAT_JSON
    resolved = output_format or _FORMAT_HUMAN
    # Every failure after this point renders the way the caller asked. The
    # commands that take `--format` all call this before anything that can fail.
    _OUTPUT_FORMAT.set(resolved)
    return resolved


def _envelope(schema: str, env: str, **payload: Any) -> dict[str, Any]:
    """The broker group's JSON envelope: ``schema`` and ``env``, then the body.

    ``env`` names the BROKER INSTANCE the command read. It is the one field a
    machine consumer cannot reconstruct from the rest, and getting SIM and
    LIVE the wrong way round is the expensive mistake this group can make. A
    command pointed at an explicit file (``reconcile --journal <path>``)
    reports that path separately as ``journal``: the instance and the source
    file are two facts, and only the operator can make them disagree.
    """
    return {"schema": schema, "env": env, **payload}


def _render_json(payload: Mapping[str, Any]) -> str:
    """Serialise the envelope strictly, or refuse.

    Split out of :func:`_emit_json` so a command that MUTATES can render before
    it writes. Rendering after the write would mean an unrenderable payload
    reports a failure for something that already happened — which is exactly the
    ``write_outcome_unknown`` shape this ticket exists to keep out of a client's
    hands.
    """
    try:
        return json.dumps(payload, default=str, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _fail_with("unclassified", f"cannot render this result as JSON: {exc}") from exc


def _emit_json(payload: Mapping[str, Any]) -> None:
    """Write the envelope as exactly one STRICT JSON value on stdout.

    ``allow_nan=False`` because Python's default emits bare ``NaN`` /
    ``Infinity``, which are not JSON and are rejected by readers outside
    Python. A lowercase ``nan`` / ``inf`` in a textfile is exactly the spelling
    ``_PROM_LINE_RE`` accepts and ``float()`` parses (the regex has no capital
    ``N`` or ``I``, so Prometheus' own ``NaN`` / ``+Inf`` would be skipped), and
    such a value used to land in the ``stream-status`` envelope with exit 0.
    Until #1462 our own ``observability.textfile`` produced that spelling; it
    now drops a non-finite value instead, so this guard covers the other
    writers of the directory (the bash hook, a hand-edited file, a future
    emitter).

    A payload that cannot be rendered strictly is REFUSED, so stdout is either
    one valid JSON value or empty. Serialising half-valid output would defeat
    the contract the rest of this module exists to hold.
    """
    typer.echo(_render_json(payload))
