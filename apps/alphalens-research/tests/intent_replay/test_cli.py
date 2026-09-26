"""`intent-replay`, the one-shot command (spec sections 5.3 and 5.4).

The command is a versioned API with a machine renderer: one metadata source
builds the parser, the help and the `schema` manifest, so the three cannot
drift. Its failure contract is the broker CLI's: stdout stays EMPTY, stderr
carries exactly one line that is a JSON object and that line is the last one,
the object has the five doctrine keys, and the exit status is coarse (0 ok,
2 usage, 1 everything else, 130 interrupted with nothing written).

The success path is deliberately empty: an accepted document exits 0 with
nothing on either stream, because nothing exists yet to print (the envelope is
PR 7). That is asserted here as the contract of this version, and it holds
although `run` now also INTERPRETS the document — the interpreter's two
refusals are reachable while its success is silent.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import io
import json
import logging
import os
import pkgutil
import runpy
import shlex
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

import intent_replay
from broker_contract.failure import CONTRACT_FAILURE_CODES, ContractError, Failure
from intent_replay import classification, cli
from intent_replay.cli import (
    CONFIG_MALFORMED_REASONS,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    FAILURE_CODES,
    INTENT_MALFORMED_REASONS,
    MANIFEST_SCHEMA,
    build_parser,
    main,
    manifest,
    render_help,
)
from intent_replay.door import DOOR_REASONS

from tests.intent_replay.test_config import CANONICAL

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
EXAMPLES = WORKSPACE_ROOT / "apps" / "alphalens-broker-contract" / "examples" / "manual-pick"
HELP_HEADINGS = ("USAGE", "OPTIONS", "OUTPUT", "EXIT CODES", "EXAMPLES")
ADAPTER_MODULES = frozenset({"door", "cli", "__main__"})


def _document(name: str = "pullback-two-tiers", *, dated: bool = True) -> dict[str, Any]:
    document = json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    if dated:
        document["meta"] = {**document["meta"], "trade_date": "2026-09-23"}
    return document


class _Run:
    """One invocation of `main`, with both streams captured."""

    def __init__(self, argv: Sequence[str], *, stdin: bytes | None = None) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            if stdin is not None:
                stack.enter_context(
                    mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8"))
                )
            self.code = main(list(argv))
        self.stdout = out.getvalue()
        self.stderr = err.getvalue()

    def failure(self, test: unittest.TestCase) -> dict[str, Any]:
        """The one JSON object on stderr, asserting the whole stderr contract."""
        test.assertEqual(self.stdout, "", "stdout must stay empty on a refusal")
        lines = self.stderr.splitlines()
        objects = [index for index, line in enumerate(lines) if _is_json_object(line)]
        test.assertEqual(len(objects), 1, f"expected exactly one JSON line, got {self.stderr!r}")
        test.assertEqual(objects[0], len(lines) - 1, "the JSON object must be the LAST line")
        value = json.loads(lines[objects[0]])
        test.assertEqual(
            set(value), {"code", "message", "retryable", "details", "suggestions"}, value
        )
        test.assertFalse(value["retryable"], "a replay refusal is never retryable")
        return value


def _is_json_object(line: str) -> bool:
    try:
        return isinstance(json.loads(line), dict)
    except ValueError:
        return False


class _Files(unittest.TestCase):
    """A temp directory with a dated template and the canonical config in it."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: _rmtree(self.directory))
        self.document = self.write("pick.json", _document())
        self.config = self.write("run.json", CANONICAL)

    def write(self, name: str, value: Any) -> str:
        return self.write_text(name, json.dumps(value))

    def write_text(self, name: str, text: str) -> str:
        return self.write_bytes(name, text.encode("utf-8"))

    def write_bytes(self, name: str, data: bytes) -> str:
        path = self.directory / name
        path.write_bytes(data)
        return str(path)

    def run_cli(self, *argv: str, stdin: bytes | None = None) -> _Run:
        return _Run(argv, stdin=stdin)


def _rmtree(directory: Path) -> None:
    for path in sorted(directory.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    directory.rmdir()


class AcceptedDocumentTest(_Files):
    def test_an_accepted_document_exits_zero_with_nothing_on_either_stream(self) -> None:
        run = self.run_cli("run", self.document, "--config", self.config)
        self.assertEqual((run.code, run.stdout, run.stderr), (EXIT_OK, "", ""))

    def test_the_document_may_come_from_stdin(self) -> None:
        data = json.dumps(_document()).encode("utf-8")
        run = self.run_cli("run", "-", "--config", self.config, stdin=data)
        self.assertEqual((run.code, run.stdout, run.stderr), (EXIT_OK, "", ""))

    def test_ndjson_is_accepted_as_a_format_name(self) -> None:
        run = self.run_cli("run", self.document, "--config", self.config, "--format", "ndjson")
        self.assertEqual((run.code, run.stdout, run.stderr), (EXIT_OK, "", ""))


class DocumentRefusalTest(_Files):
    def test_bytes_that_are_not_json(self) -> None:
        path = self.write_text("bad.json", "{not json")
        failure = self.run_cli("run", path, "--config", self.config).failure(self)
        self.assertEqual(
            (failure["code"], failure["details"]["reason"]), ("intent_malformed", "not_json")
        )

    def test_bytes_that_are_not_utf8(self) -> None:
        path = self.write_bytes("bad.json", b'{"a": "\xff\xfe"}')
        failure = self.run_cli("run", path, "--config", self.config).failure(self)
        self.assertEqual(
            (failure["code"], failure["details"]["reason"]), ("intent_malformed", "not_json")
        )

    def test_a_repeated_key_names_every_repeated_key(self) -> None:
        path = self.write_text(
            "dup.json", '{"instrument": 1, "instrument": 2, "spec": 1, "spec": 2}'
        )
        failure = self.run_cli("run", path, "--config", self.config).failure(self)
        self.assertEqual(failure["code"], "intent_malformed")
        self.assertEqual(
            failure["details"], {"reason": "duplicate_key", "keys": ["instrument", "spec"]}
        )

    def test_a_repeated_key_is_seen_through_stdin_too(self) -> None:
        failure = self.run_cli(
            "run",
            "-",
            "--config",
            self.config,
            stdin=b'{"meta": {"source": "manual", "source": "brief"}}',
        ).failure(self)
        self.assertEqual(failure["details"], {"reason": "duplicate_key", "keys": ["source"]})

    def test_a_document_that_is_not_an_object(self) -> None:
        path = self.write_text("list.json", "[1, 2, 3]")
        failure = self.run_cli("run", path, "--config", self.config).failure(self)
        self.assertEqual(
            (failure["code"], failure["details"]["reason"]),
            ("intent_malformed", "schema_violation"),
        )
        self.assertEqual(failure["details"]["path"], "$")

    def test_a_missing_document_file_is_usage_with_a_runnable_suggestion(self) -> None:
        run = self.run_cli("run", str(self.directory / "absent.json"), "--config", self.config)
        failure = run.failure(self)
        self.assertEqual((run.code, failure["code"]), (EXIT_USAGE, "usage"))
        self.assertEqual(failure["details"]["path"], str(self.directory / "absent.json"))
        self.assertIsInstance(failure["suggestions"][0]["argv"], list)
        self.assertEqual(failure["suggestions"][0]["argv"][:2], ["intent-replay", "run"])

    def test_a_door_refusal_is_intent_malformed_with_the_doors_reason(self) -> None:
        path = self.write("template.json", _document(dated=False))
        run = self.run_cli("run", path, "--config", self.config)
        failure = run.failure(self)
        self.assertEqual(run.code, EXIT_FAILED)
        self.assertEqual(
            (failure["code"], failure["details"]["reason"]),
            ("intent_malformed", "trade_date_required"),
        )

    def test_a_discarded_key_carries_its_paths(self) -> None:
        document = _document()
        document["spec"]["entry_tiers"][0]["limit_pirce"] = 1.0
        path = self.write("typo.json", document)
        failure = self.run_cli("run", path, "--config", self.config).failure(self)
        self.assertEqual(
            failure["details"],
            {"reason": "key_discarded", "paths": ["spec.entry_tiers[0].limit_pirce"]},
        )

    def test_an_incoherent_document_passes_through_as_intent_invalid(self) -> None:
        document = _document()
        document["spec"]["disaster_stop"] = 1000.0
        path = self.write("incoherent.json", document)
        run = self.run_cli("run", path, "--config", self.config)
        failure = run.failure(self)
        self.assertEqual(run.code, EXIT_FAILED)
        self.assertEqual(
            (failure["code"], failure["details"]["reason"]), ("intent_invalid", "stop_above_entry")
        )


class ConfigRefusalTest(_Files):
    def test_an_empty_block_is_config_incomplete_naming_the_seven_keys(self) -> None:
        path = self.write("empty.json", {})
        run = self.run_cli("run", self.document, "--config", path)
        failure = run.failure(self)
        self.assertEqual((run.code, failure["code"]), (EXIT_FAILED, "config_incomplete"))
        self.assertEqual(len(failure["details"]["keys"]), 7)

    def test_an_unusable_value_is_config_invalid(self) -> None:
        config = copy.deepcopy(CANONICAL)
        config["oco"] = True
        path = self.write("oco.json", config)
        failure = self.run_cli("run", self.document, "--config", path).failure(self)
        self.assertEqual(
            (failure["code"], failure["details"]["reason"]), ("config_invalid", "oco_unsupported")
        )

    def test_a_config_that_is_not_json_is_config_malformed(self) -> None:
        path = self.write_text("bad.json", "nope")
        run = self.run_cli("run", self.document, "--config", path)
        failure = run.failure(self)
        self.assertEqual((run.code, failure["code"]), (EXIT_FAILED, "config_malformed"))
        self.assertEqual(failure["details"], {"reason": "not_json", "path": path})

    def test_a_config_with_a_repeated_key_is_config_malformed(self) -> None:
        path = self.write_text("dup.json", '{"oco": false, "oco": true}')
        failure = self.run_cli("run", self.document, "--config", path).failure(self)
        self.assertEqual(
            failure["details"], {"reason": "duplicate_key", "keys": ["oco"], "path": path}
        )

    def test_a_missing_config_file_is_usage(self) -> None:
        absent = str(self.directory / "absent.json")
        run = self.run_cli("run", self.document, "--config", absent)
        failure = run.failure(self)
        self.assertEqual(
            (run.code, failure["code"], failure["details"]["path"]), (EXIT_USAGE, "usage", absent)
        )

    def test_the_document_is_judged_before_the_config(self) -> None:
        template = self.write("template.json", _document(dated=False))
        empty = self.write("empty.json", {})
        failure = self.run_cli("run", template, "--config", empty).failure(self)
        self.assertEqual(failure["code"], "intent_malformed")


class UsageTest(_Files):
    def _usage(self, *argv: str) -> dict[str, Any]:
        run = self.run_cli(*argv)
        failure = run.failure(self)
        self.assertEqual((run.code, failure["code"]), (EXIT_USAGE, "usage"))
        return failure

    def test_no_command(self) -> None:
        self._usage()

    def test_a_missing_required_option(self) -> None:
        self._usage("run", self.document)

    def test_an_unknown_format(self) -> None:
        self._usage("run", self.document, "--config", self.config, "--format", "human")

    def test_an_abbreviated_option_is_not_understood(self) -> None:
        self._usage("run", self.document, "--conf", self.config)

    def test_an_unknown_option(self) -> None:
        self._usage("run", self.document, "--config", self.config, "--bogus")

    def test_an_unknown_command(self) -> None:
        self._usage("walk")

    def test_schema_of_an_unknown_command(self) -> None:
        self._usage("schema", "walk")


class InterpreterRefusalTest(_Files):
    """The interpreter's own refusals, reached through `run` (PR 5)."""

    def test_an_immediate_tranche_is_refused_naming_its_tier(self) -> None:
        path = self.write("immediate.json", _document("immediate-plus-pullback"))
        run = self.run_cli("run", path, "--config", self.config)
        failure = run.failure(self)
        self.assertEqual((run.code, failure["code"]), (EXIT_FAILED, "entry_mode_unsupported"))
        self.assertEqual(failure["details"]["tiers"], [0])

    def test_an_unclassified_path_is_mapped_with_the_paths_it_names(self) -> None:
        # No admissible document can provoke the gate: once the interpreter has
        # read the interpreted paths, every path of the published input schema is
        # classified, the one that is not (`spec.tp_tranches[].r_multiple`) is
        # refused by door gate 0, and any other key an author adds is refused by
        # the fixed point. So what this proves is the MAPPING, with one class row
        # removed — the shape `test_classification.py` uses on the gate itself.
        thinner = {
            path: why
            for path, why in classification.OUT_OF_SCOPE.items()
            if path != "instrument.ticker"
        }
        with mock.patch.object(classification, "OUT_OF_SCOPE", thinner):
            run = self.run_cli("run", self.document, "--config", self.config)
        failure = run.failure(self)
        self.assertEqual((run.code, failure["code"]), (EXIT_FAILED, "path_unclassified"))
        self.assertEqual(failure["details"]["paths"], ["instrument.ticker"])


class UnexpectedPathsTest(_Files):
    def test_an_unregistered_code_is_rendered_as_it_is_and_logged(self) -> None:
        error = ContractError(Failure(code="made_up", message="?", retryable=False))
        with (
            mock.patch("intent_replay.door.admit", side_effect=error),
            self.assertLogs("intent_replay.cli", level="WARNING") as logs,
        ):
            run = self.run_cli("run", self.document, "--config", self.config)
        failure = run.failure(self)
        self.assertEqual((run.code, failure["code"]), (EXIT_FAILED, "made_up"))
        self.assertTrue(any("made_up" in line for line in logs.output))

    def test_an_interrupt_exits_130_with_nothing_written(self) -> None:
        with mock.patch("intent_replay.door.admit", side_effect=KeyboardInterrupt):
            run = self.run_cli("run", self.document, "--config", self.config)
        self.assertEqual((run.code, run.stdout, run.stderr), (EXIT_INTERRUPTED, "", ""))


class HelpTest(unittest.TestCase):
    def _help(self, *argv: str) -> str:
        run = _Run(argv)
        self.assertEqual((run.code, run.stderr), (EXIT_OK, ""))
        return run.stdout

    def test_help_at_every_level_carries_the_fixed_headings_in_order(self) -> None:
        for argv in (("--help",), ("run", "--help"), ("schema", "--help")):
            with self.subTest(argv=argv):
                text = self._help(*argv)
                positions = [text.index(heading) for heading in HELP_HEADINGS]
                self.assertEqual(positions, sorted(positions))

    def test_help_wins_over_a_missing_required_argument(self) -> None:
        self.assertIn("USAGE", self._help("run", "pick.json", "--help"))

    def test_help_does_not_wrap_to_the_terminal_width(self) -> None:
        with mock.patch.dict(os.environ, {"COLUMNS": "20"}):
            narrow = render_help("run")
        with mock.patch.dict(os.environ, {"COLUMNS": "200"}):
            wide = render_help("run")
        self.assertEqual(narrow, wide)
        self.assertEqual(narrow, self._help("run", "--help"))

    def test_every_example_parses(self) -> None:
        parser = build_parser()
        for command in manifest()["commands"]:
            for example in command["examples"]:
                with self.subTest(example=example):
                    tokens = shlex.split(example)
                    self.assertEqual(tokens[0], "intent-replay")
                    parser.parse_args(tokens[1:])


class ManifestTest(unittest.TestCase):
    def test_schema_prints_exactly_one_json_value(self) -> None:
        run = _Run(("schema", "--format", "json"))
        self.assertEqual((run.code, run.stderr), (EXIT_OK, ""))
        value = json.loads(run.stdout)
        self.assertEqual(value["schema"], MANIFEST_SCHEMA)
        self.assertEqual([c["name"] for c in value["commands"]], ["run", "schema"])

    def test_schema_of_one_command(self) -> None:
        value = json.loads(_Run(("schema", "run")).stdout)
        self.assertEqual([c["name"] for c in value["commands"]], ["run"])

    def test_the_manifest_and_the_parser_know_the_same_options(self) -> None:
        parser = build_parser()
        subparsers = next(action for action in parser._actions if action.dest == "command")
        for command in manifest()["commands"]:
            with self.subTest(command=command["name"]):
                sub = subparsers.choices[command["name"]]
                parsed = {flag for action in sub._actions for flag in action.option_strings}
                declared = {flag for option in command["options"] for flag in option["flags"]}
                self.assertEqual(declared, parsed - {"--help"})

    def test_the_format_choices_and_the_risk_are_published(self) -> None:
        run_command = next(c for c in manifest()["commands"] if c["name"] == "run")
        fmt = next(o for o in run_command["options"] if "--format" in o["flags"])
        self.assertEqual(fmt["choices"], ["json", "ndjson"])
        self.assertEqual(fmt["default"], "json")
        self.assertEqual({c["risk"] for c in manifest()["commands"]}, {"read-only"})

    def test_the_exit_codes_and_the_failure_codes_are_published(self) -> None:
        value = manifest()
        self.assertEqual(set(value["exit_codes"]), {"0", "1", "2", "130"})
        self.assertEqual(value["failure_codes"], sorted(FAILURE_CODES))


class RegistryTest(unittest.TestCase):
    def test_every_engine_code_and_intent_invalid_are_registered(self) -> None:
        engine_codes: set[str] = set()
        for info in pkgutil.iter_modules(intent_replay.__path__):
            if info.name in ADAPTER_MODULES:
                continue
            module = __import__(f"intent_replay.{info.name}", fromlist=["_"])
            engine_codes.update(
                getattr(module, name) for name in dir(module) if name.endswith("_CODE")
            )
        self.assertGreaterEqual(len(engine_codes), 8, "positive control: the engine has codes")
        self.assertTrue(engine_codes <= set(FAILURE_CODES), engine_codes - set(FAILURE_CODES))
        self.assertIn("intent_invalid", FAILURE_CODES)
        self.assertEqual(FAILURE_CODES["intent_invalid"], CONTRACT_FAILURE_CODES["intent_invalid"])

    def test_the_cli_owns_three_codes(self) -> None:
        for code in ("intent_malformed", "config_malformed", "usage"):
            self.assertIn(code, FAILURE_CODES)
            self.assertFalse(FAILURE_CODES[code].retryable)

    def test_a_door_reason_outside_the_vocabulary_is_a_programming_error(self) -> None:
        """The door's vocabulary is a subset of the CLI's by construction; a
        reason the CLI does not publish must never reach a caller as a code."""
        with self.assertRaises(ValueError):
            cli._intent_malformed("made_up", "nothing")

    def test_the_reason_vocabularies(self) -> None:
        self.assertEqual(
            set(INTENT_MALFORMED_REASONS), set(DOOR_REASONS) | {"not_json", "duplicate_key"}
        )
        self.assertEqual(set(CONFIG_MALFORMED_REASONS), {"not_json", "duplicate_key"})


class EntryPointTest(unittest.TestCase):
    def test_python_m_intent_replay_runs_main(self) -> None:
        out = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["intent-replay", "schema", "--format", "json"]),
            contextlib.redirect_stdout(out),
            self.assertRaises(SystemExit) as caught,
        ):
            runpy.run_module("intent_replay", run_name="__main__", alter_sys=True)
        self.assertEqual(caught.exception.code, EXIT_OK)
        self.assertEqual(json.loads(out.getvalue())["schema"], MANIFEST_SCHEMA)

    def test_importing_the_cli_configures_no_logging(self) -> None:
        tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
        calls = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in {"basicConfig", "addHandler"}
        }
        self.assertEqual(calls, set())
        self.assertIsInstance(logging.getLogger("intent_replay.cli"), logging.Logger)


if __name__ == "__main__":
    unittest.main()
