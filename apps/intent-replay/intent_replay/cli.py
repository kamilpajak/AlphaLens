"""``intent-replay``: the one-shot command (spec sections 5.3 and 5.4).

A versioned API with a machine renderer. ONE metadata source, :data:`COMMANDS`,
builds the argument parser, the ``--help`` text and the ``schema`` manifest, so
the three cannot drift. Output rules, in the order a caller meets them:

* ``--format json|ndjson``; there is no human renderer in v1 (section 5.3), so
  the option takes the name and nothing else.
* stdout carries the result only. In this version an ACCEPTED document has
  nothing to print (the envelope is PR 7), so the command exits 0 with empty
  stdout and empty stderr.
* a refusal goes to stderr as exactly one line that is a JSON object, and that
  line is the LAST one (a library may log a warning above it); stdout stays
  empty. The object has the five doctrine keys ``code``, ``message``,
  ``retryable``, ``details`` and ``suggestions``.
* exit statuses are coarse: ``0`` accepted, ``2`` usage, ``130`` interrupted
  with nothing written, ``1`` everything else. The failure MODE is in ``code``.

Codes. The engine modules raise typed :class:`ContractError` exceptions whose
``failure.code`` is already set, and this module passes the object through
unchanged. Three codes are this CLI's own, because they name CLI concepts a
leaf never would (the #1122 split, spec section 5.4): ``intent_malformed`` (the
document is not the published wire shape; the door raises the REASON, this
module names the code), ``config_malformed`` (the configuration file is not one
JSON object) and ``usage`` (the invocation: a bad option, or a file it names
cannot be read). :data:`FAILURE_CODES` is the whole registry and is what
``schema`` publishes; the package README carries the same table.

ADAPTER module: stdlib and ``broker_contract``; it may import ``jsonschema``
and does not need to (the door does).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from broker_contract.failure import (
    CONTRACT_FAILURE_CODES,
    ContractError,
    Failure,
    FailureCode,
    Suggestion,
)

from intent_replay import bars, classification, config, door
from intent_replay.config import RunConfig

__all__ = [
    "COMMANDS",
    "CONFIG_MALFORMED_REASONS",
    "EXIT_FAILED",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_USAGE",
    "FAILURE_CODES",
    "INTENT_MALFORMED_REASONS",
    "MANIFEST_SCHEMA",
    "OWNERS",
    "PROGRAM",
    "build_parser",
    "main",
    "manifest",
    "render_help",
]

logger = logging.getLogger(__name__)

PROGRAM: Final = "intent-replay"
MANIFEST_SCHEMA: Final = "intent_replay.cli/v1"
FORMATS: Final = ("json", "ndjson")

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2
EXIT_INTERRUPTED: Final = 130
EXIT_CODES: Final[Mapping[str, str]] = MappingProxyType(
    {
        str(EXIT_OK): "accepted; in this version nothing is printed",
        str(EXIT_FAILED): "refused; the failure object is the last line on stderr",
        str(EXIT_USAGE): "the invocation is malformed, or a file it names cannot be read",
        str(EXIT_INTERRUPTED): "interrupted; nothing was written",
    }
)

# --- the failure registry --------------------------------------------------

INTENT_MALFORMED_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "not_json": "the bytes are not a UTF-8 JSON document",
        "duplicate_key": "an object repeats a key; a JSON parser keeps the LAST silently, "
        "so the value sent first would vanish; `details.keys` lists them",
        **door.DOOR_REASONS,
    }
)
CONFIG_MALFORMED_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "not_json": "the configuration file is not a UTF-8 JSON document",
        "duplicate_key": "an object in the configuration file repeats a key; "
        "`details.keys` lists them",
    }
)


def _code(name: str, meaning: str) -> FailureCode:
    return FailureCode(name=name, retryable=False, meaning=meaning)


def _registry(*codes: FailureCode) -> Mapping[str, FailureCode]:
    return MappingProxyType({code.name: code for code in codes})


_ENGINE_CODES: Final[Mapping[str, FailureCode]] = _registry(
    _code(bars.BARS_EMPTY_CODE, "No bars were supplied."),
    _code(bars.BARS_UNORDERED_CODE, "The bars are not strictly increasing in time."),
    _code(
        bars.BARS_INVALID_CODE,
        "A bar carries a price that cannot be compared (NaN or infinite); "
        "`details.reason` and `details.field` name it.",
    ),
    _code(
        bars.WINDOW_TOO_SHORT_CODE,
        "The bars do not cover the stated `walk_start`; `details.reason` says which side.",
    ),
    _code(
        config.CONFIG_INCOMPLETE_CODE,
        "A required configuration value was not stated; `details.keys` names every missing key.",
    ),
    _code(
        config.CONFIG_INVALID_CODE,
        "A stated configuration value nothing can use; `details.keys` and "
        "`details.reason` name it.",
    ),
    _code(
        classification.PATH_UNCLASSIFIED_CODE,
        "The document carries a path the replay neither interprets, translates "
        "nor lists as out of scope; `details.paths` names them.",
    ),
)
_CLI_CODES: Final[Mapping[str, FailureCode]] = _registry(
    _code(
        "intent_malformed",
        "The document is not the published input contract: not JSON, a repeated "
        "key, a derived field, a schema violation, a missing or malformed trade "
        "date, undecodable, or carrying a key the decoder would discard. "
        "`details.reason` names which.",
    ),
    _code(
        "config_malformed",
        "The configuration file is not one JSON object: not JSON, or a repeated "
        "key. `details.reason` names which, `details.path` the file.",
    ),
    _code(
        "usage",
        "The invocation is malformed (a bad option or value), or a file it names "
        "cannot be read (`details.path`).",
    ),
)
FAILURE_CODES: Final[Mapping[str, FailureCode]] = MappingProxyType(
    {
        **_ENGINE_CODES,
        "intent_invalid": CONTRACT_FAILURE_CODES["intent_invalid"],
        **_CLI_CODES,
    }
)
OWNERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        **dict.fromkeys(_ENGINE_CODES, "engine"),
        "intent_invalid": "contract",
        **dict.fromkeys(_CLI_CODES, "CLI"),
    }
)

# --- the one metadata source ------------------------------------------------


@dataclass(frozen=True, slots=True)
class Argument:
    name: str
    dest: str
    help: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class Option:
    flags: tuple[str, ...]
    dest: str
    kind: str
    help: str
    required: bool = False
    choices: tuple[str, ...] = ()
    default: str | None = None


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    summary: str
    risk: str
    arguments: tuple[Argument, ...]
    options: tuple[Option, ...]
    output: str
    examples: tuple[str, ...]


_FORMAT_OPTION: Final = Option(
    flags=("--format",),
    dest="format",
    kind="choice",
    help="output format; there is no human renderer in v1",
    choices=FORMATS,
    default="json",
)

COMMANDS: Final[tuple[Command, ...]] = (
    Command(
        name="run",
        summary="decode a TradeIntent document and its run configuration, or refuse",
        risk="read-only",
        arguments=(
            Argument(
                name="DOCUMENT",
                dest="document",
                help="path to a TradeIntent JSON document, or - to read one from stdin (to EOF)",
            ),
        ),
        options=(
            Option(
                flags=("--config",),
                dest="config",
                kind="path",
                help="path to the run configuration block (spec section 5.2)",
                required=True,
            ),
            _FORMAT_OPTION,
        ),
        output=(
            "nothing on an accepted document in this version; a refusal is one JSON "
            "object on stderr"
        ),
        examples=(
            "intent-replay run pick.json --config run.json",
            "intent-replay run pick.json --config run.json --format ndjson",
        ),
    ),
    Command(
        name="schema",
        summary="print the machine-readable description of this command tree",
        risk="read-only",
        arguments=(
            Argument(
                name="COMMAND",
                dest="target",
                help="describe only this command",
                required=False,
            ),
        ),
        options=(
            Option(
                flags=("--format",),
                dest="format",
                kind="choice",
                help="output format",
                choices=("json",),
                default="json",
            ),
        ),
        output="exactly one JSON value on stdout",
        examples=("intent-replay schema --format json", "intent-replay schema run"),
    ),
)


def _command(name: str) -> Command:
    return next(command for command in COMMANDS if command.name == name)


# --- the parser --------------------------------------------------------------


class _UsageError(Exception):
    """argparse's `error()`, turned into an exception `main` renders."""


class _HelpRequestedError(Exception):
    def __init__(self, command: str | None) -> None:
        super().__init__(command)
        self.command = command


class _Parser(argparse.ArgumentParser):
    """No built-in help (rendered from the metadata instead), no abbreviated
    options, and no text written by argparse itself: `error()` raises."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("add_help", False)
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> Any:
        raise _UsageError(message)


class _HelpAction(argparse.Action):
    """`--help` fires while argv is consumed, before argparse checks required
    arguments, so `run --help` is help and not a usage error."""

    def __init__(self, option_strings: Sequence[str], dest: str, command: str | None) -> None:
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS)
        self.command = command

    def __call__(self, parser: Any, namespace: Any, values: Any, option_string: Any = None) -> None:
        raise _HelpRequestedError(self.command)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROGRAM)
    parser.add_argument("--help", action=_HelpAction, dest="help", command=None)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in COMMANDS:
        sub = subparsers.add_parser(command.name)
        sub.add_argument("--help", action=_HelpAction, dest="help", command=command.name)
        for argument in command.arguments:
            sub.add_argument(
                argument.dest, metavar=argument.name, nargs=None if argument.required else "?"
            )
        for option in command.options:
            sub.add_argument(
                *option.flags,
                dest=option.dest,
                required=option.required,
                choices=option.choices or None,
                default=option.default,
            )
    return parser


# --- help and manifest, rendered from the same source ------------------------


def _usage_line(command: Command) -> str:
    parts = [PROGRAM, command.name]
    parts.extend(arg.name if arg.required else f"[{arg.name}]" for arg in command.arguments)
    for option in command.options:
        value = "|".join(option.choices) if option.choices else "PATH"
        spelled = f"{option.flags[0]} {value}"
        parts.append(spelled if option.required else f"[{spelled}]")
    return " ".join(parts)


def _option_lines(command: Command) -> list[str]:
    lines = []
    for argument in command.arguments:
        lines.append(f"  {argument.name:<22}{argument.help}")
    for option in command.options:
        value = "|".join(option.choices) if option.choices else "PATH"
        suffix = f" (default: {option.default})" if option.default else ""
        lines.append(f"  {option.flags[0] + ' ' + value:<22}{option.help}{suffix}")
    lines.append(f"  {'--help':<22}print this text and exit 0")
    return lines


def render_help(command_name: str | None = None) -> str:
    """The fixed-heading help text. Never wrapped: the width of a terminal is
    not an input, so the text is the same in a pipe and on a screen."""
    selected = [_command(command_name)] if command_name else list(COMMANDS)
    lines: list[str] = ["USAGE"]
    lines.extend(f"  {_usage_line(command)}" for command in selected)
    lines.append("")
    lines.append("OPTIONS")
    if command_name:
        lines.extend(_option_lines(selected[0]))
    else:
        lines.extend(f"  {command.name:<22}{command.summary}" for command in COMMANDS)
        lines.append(f"  {'--help':<22}print this text and exit 0")
    lines.append("")
    lines.append("OUTPUT")
    lines.extend(f"  {command.name}: {command.output}" for command in selected)
    lines.append("  a refusal is one JSON object on stderr, the last line; stdout stays empty")
    lines.append("")
    lines.append("EXIT CODES")
    lines.extend(f"  {code:<6}{meaning}" for code, meaning in EXIT_CODES.items())
    lines.append("")
    lines.append("EXAMPLES")
    lines.extend(f"  {example}" for command in selected for example in command.examples)
    return "\n".join(lines) + "\n"


def _command_manifest(command: Command) -> dict[str, Any]:
    return {
        "name": command.name,
        "summary": command.summary,
        "risk": command.risk,
        "arguments": [
            {"name": a.name, "help": a.help, "required": a.required} for a in command.arguments
        ],
        "options": [
            {
                "flags": list(o.flags),
                "kind": o.kind,
                "help": o.help,
                "required": o.required,
                "choices": list(o.choices),
                "default": o.default,
            }
            for o in command.options
        ],
        "output": command.output,
        "examples": list(command.examples),
    }


def manifest(command_name: str | None = None) -> dict[str, Any]:
    """The `schema` command's value: the command tree, exit codes and failure codes."""
    selected = [_command(command_name)] if command_name else list(COMMANDS)
    return {
        "schema": MANIFEST_SCHEMA,
        "program": PROGRAM,
        "commands": [_command_manifest(command) for command in selected],
        "exit_codes": dict(EXIT_CODES),
        "failure_codes": sorted(FAILURE_CODES),
    }


# --- reading the inputs -------------------------------------------------------


class _RefusalError(Exception):
    """A failure `main` renders; the code decides the exit status."""

    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.message)
        self.failure = failure


class _MalformedError(Exception):
    """A parse failure, before the caller knows which code it maps to."""

    def __init__(self, reason: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details = details


class _DuplicateJsonKeyError(ValueError):
    def __init__(self, keys: list[str]) -> None:
        super().__init__(f"duplicate key(s): {', '.join(keys)}")
        self.keys = keys


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    counts = Counter(key for key, _ in pairs)
    duplicated = sorted(key for key, count in counts.items() if count > 1)
    if duplicated:
        raise _DuplicateJsonKeyError(duplicated)
    return dict(pairs)


_TEMPLATE_SUGGESTION: Final = Suggestion(
    argv=(PROGRAM, "run", "<FILE>", "--config", "<CONFIG>"),
    why=(
        "a document is a TradeIntent JSON file; start from a template in "
        "apps/alphalens-broker-contract/examples/manual-pick/ and state meta.trade_date"
    ),
)


def _usage(message: str, **details: Any) -> _RefusalError:
    return _RefusalError(Failure(code="usage", message=message, retryable=False, details=details))


def _read(source: str, *, suggestions: tuple[Suggestion, ...] = ()) -> bytes:
    if source == "-":
        return sys.stdin.buffer.read()
    try:
        return Path(source).read_bytes()  # NOSONAR S8707: reading the named file is the job
    except OSError as exc:
        raise _RefusalError(
            Failure(
                code="usage",
                message=f"cannot read {source}: {exc}",
                retryable=False,
                details={"path": source},
                suggestions=suggestions,
            )
        ) from exc


def _parsed(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_object_without_duplicate_keys)
    except UnicodeDecodeError as exc:
        raise _MalformedError("not_json", f"not a UTF-8 JSON document: {exc}") from exc
    # BEFORE JSONDecodeError, which is also a ValueError.
    except _DuplicateJsonKeyError as exc:
        raise _MalformedError(
            "duplicate_key",
            f"{exc} - a JSON parser keeps the last silently, so the value sent first would vanish",
            keys=exc.keys,
        ) from exc
    except json.JSONDecodeError as exc:
        raise _MalformedError("not_json", f"not a JSON document: {exc}") from exc


def _intent_malformed(reason: str, message: str, **details: Any) -> _RefusalError:
    if reason not in INTENT_MALFORMED_REASONS:
        raise ValueError(f"unregistered intent_malformed reason: {reason!r}")
    return _RefusalError(
        Failure(
            code="intent_malformed",
            message=message,
            retryable=False,
            details={"reason": reason, **details},
        )
    )


def _load_document(source: str) -> Any:
    data = _read(source, suggestions=(_TEMPLATE_SUGGESTION,))
    try:
        return _parsed(data)
    except _MalformedError as exc:
        raise _intent_malformed(exc.reason, exc.message, **exc.details) from exc


def _load_config(source: str) -> Any:
    data = _read(source)
    try:
        return _parsed(data)
    except _MalformedError as exc:
        raise _RefusalError(
            Failure(
                code="config_malformed",
                message=f"{source}: {exc.message}",
                retryable=False,
                details={"reason": exc.reason, **exc.details, "path": source},
            )
        ) from exc


# --- the commands ---------------------------------------------------------------


def _run(args: argparse.Namespace) -> int:
    """Document first, so a caller fixes the primary input before the block."""
    document = _load_document(args.document)
    try:
        door.admit(document)
    except door.DoorRefusalError as exc:
        raise _intent_malformed(exc.reason, exc.message, **exc.details) from exc
    RunConfig.from_jsonable(_load_config(args.config))
    return EXIT_OK


def _schema(args: argparse.Namespace) -> int:
    target = args.target
    if target is not None and target not in {command.name for command in COMMANDS}:
        raise _usage(f"unknown command {target!r}")
    sys.stdout.write(json.dumps(manifest(target), allow_nan=False) + "\n")
    return EXIT_OK


def _emit(failure: Failure) -> int:
    """Exactly one JSON line on stderr; the exit status follows the code."""
    if failure.code not in FAILURE_CODES:
        logger.warning("unregistered failure code %r - rendered as it is", failure.code)
    sys.stderr.write(json.dumps(failure.to_jsonable(), allow_nan=False, default=str) + "\n")
    return EXIT_USAGE if failure.code == "usage" else EXIT_FAILED


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point; returns the exit status and writes nothing
    but the result (stdout) or the failure object (stderr)."""
    try:
        args = build_parser().parse_args(argv)
        return _run(args) if args.command == "run" else _schema(args)
    except _HelpRequestedError as requested:
        sys.stdout.write(render_help(requested.command))
        return EXIT_OK
    except _UsageError as exc:
        return _emit(_usage(str(exc)).failure)
    except _RefusalError as exc:
        return _emit(exc.failure)
    except ContractError as exc:
        return _emit(exc.failure)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
