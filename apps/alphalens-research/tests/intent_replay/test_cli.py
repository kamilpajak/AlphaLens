"""`intent-replay`, the one-shot command (spec sections 5.3 and 5.4).

The command is a versioned API with a machine renderer: one metadata source
builds the parser, the help and the `schema` manifest, so the three cannot
drift. Its failure contract is the broker CLI's: stdout stays EMPTY, stderr
carries exactly one line that is a JSON object and that line is the last one,
the object has the five doctrine keys, and the exit status is coarse (0 ok,
2 usage, 1 everything else, 130 interrupted with nothing written).

The success path PRINTS from this version on: `--format json` writes exactly
one JSON value on stdout and `--format ndjson` writes the section 5.3 transport
lines, both with stderr empty. The assertion with the most refuting power is
that the `data` of the stream's `result` line EQUALS the whole `--format json`
object: section 5.3 gives that byte-identity as the reason the wrapper exists
at all.
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
    BARS_MALFORMED_REASONS,
    CONFIG_MALFORMED_REASONS,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    FAILURE_CODES,
    INTENT_MALFORMED_REASONS,
    MANIFEST_SCHEMA,
    OWNERS,
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

# Two bars straddling the canonical `walk_start`, taken FROM the config block so
# the pair cannot drift apart: the window check refuses bars that miss it.
WALK_START = CANONICAL["walk_start"]["value"]
BARS = [
    {"t": WALK_START, "open": 68.0, "high": 68.2, "low": 67.9, "close": 68.0},
    {"t": WALK_START + 60_000, "open": 68.0, "high": 68.3, "low": 67.8, "close": 68.1},
]
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
        self.bars = self.write("bars.json", BARS)

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
    def _json(self, *extra: str, stdin: bytes | None = None) -> tuple[_Run, Any]:
        run = self.run_cli(
            "run", self.document, "--config", self.config, "--bars", self.bars, *extra, stdin=stdin
        )
        self.assertEqual((run.code, run.stderr), (EXIT_OK, ""))
        return run, json.loads(run.stdout)

    def test_an_accepted_document_prints_exactly_one_json_value(self) -> None:
        run, value = self._json()
        self.assertEqual(run.stdout.count("\n"), 1)
        self.assertTrue(run.stdout.endswith("\n"))
        self.assertEqual(value["schema"], "intent_replay.result/v1")
        self.assertEqual(value["intent_id"], "REPLAY")

    def test_the_command_hands_the_envelope_the_documents_own_floor(self) -> None:
        # The wiring between this command and the envelope was untested for
        # VALUES: every assertion looked at ``schema`` and ``intent_id``, so
        # passing 0.0 as the R denominator floor changed every published R
        # number and left the whole suite green. The document's disaster stop
        # is 63.00 and the single fill is at 68.00, so the denominator is 5.00.
        _, value = self._json()
        summary = value["summary"]
        self.assertEqual(summary["avg_entry_price"]["value"], 68.0)
        self.assertEqual(summary["r_multiple"]["denominator"]["value"], 5.0)
        self.assertEqual(summary["notional_spent"], {"value": 900.0, "unit": "EUR"})
        self.assertEqual(value["outcome"], "open")

    def test_a_stated_trail_distance_reaches_the_published_command(self) -> None:
        """The trailing model is the one both deployments run, and no case here
        reached it: every CLI test uses the canonical block, which states null.

        The rung is 68.00 and the distance 50 bps. Bar 1 touches it and arms a
        trigger of 68.34, which that bar's high of 68.20 does not reach, and its
        low of 67.90 ratchets the trough. Bar 2 therefore tests 68.24, its high
        of 68.30 reaches it, and the fill is 68.24 instead of the rung - which
        is where every number below comes from, including the R denominator the
        document's 63.00 stop is measured against.

        Compared with a tolerance, not for equality: the trigger is a float SUM
        of the trough and the distance, and 67.90 + 0.34 is 68.24000000000001.
        The published number carries that representation, so an exact-equality
        assertion here would be asserting the arithmetic and not the model.
        """
        config = self.write("trail.json", {**CANONICAL, "entry_trail_bps": 50})
        run = self.run_cli("run", self.document, "--config", config, "--bars", self.bars)
        self.assertEqual((run.code, run.stderr), (EXIT_OK, ""))
        value = json.loads(run.stdout)
        summary = value["summary"]
        self.assertAlmostEqual(summary["avg_entry_price"]["value"], 68.24, places=9)
        self.assertAlmostEqual(summary["r_multiple"]["denominator"]["value"], 5.24, places=9)
        self.assertAlmostEqual(summary["notional_spent"]["value"], 903.176471, places=6)
        self.assertEqual(summary["snu_bars"], 1)
        self.assertEqual(value["outcome"], "open")
        self.assertIn("native_entry_trail_is_a_broker_model", value["divergences"])

    def test_the_command_hands_the_envelope_the_series_it_read(self) -> None:
        _, value = self._json()
        self.assertEqual(
            value["window"],
            {"from_t": BARS[0]["t"], "to_t": BARS[-1]["t"], "bars": len(BARS)},
        )

    def test_no_non_finite_number_can_reach_stdout(self) -> None:
        # ``allow_nan=False`` on the writer; the reader refuses the tokens a
        # non-strict writer would have emitted.
        run, _ = self._json()

        def refuse(token: str) -> float:
            raise AssertionError(f"non-finite token on stdout: {token}")

        json.loads(run.stdout, parse_constant=refuse)

    def test_the_document_may_come_from_stdin(self) -> None:
        _, value = self._json(stdin=json.dumps(_document()).encode("utf-8"))
        self.assertEqual(value["schema"], "intent_replay.result/v1")

    def test_stdin_and_a_file_print_the_same_object(self) -> None:
        _, from_file = self._json()
        run = self.run_cli(
            "run",
            "-",
            "--config",
            self.config,
            "--bars",
            self.bars,
            stdin=json.dumps(_document()).encode("utf-8"),
        )
        self.assertEqual(json.loads(run.stdout), from_file)

    def test_ndjson_prints_a_result_line_and_a_summary_line(self) -> None:
        run = self.run_cli(
            "run",
            self.document,
            "--config",
            self.config,
            "--bars",
            self.bars,
            "--format",
            "ndjson",
        )
        self.assertEqual((run.code, run.stderr), (EXIT_OK, ""))
        lines = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual([line["type"] for line in lines], ["result", "summary"])
        self.assertEqual([line["sequence"] for line in lines], [1, 2])

    def test_every_stream_line_carries_the_transport_schema(self) -> None:
        # Section 5.3: EVERY line, the summary included, so the summary's own
        # shape can be versioned later.
        run = self.run_cli(
            "run",
            self.document,
            "--config",
            self.config,
            "--bars",
            self.bars,
            "--format",
            "ndjson",
        )
        for line in run.stdout.splitlines():
            self.assertEqual(json.loads(line)["schema"], "intent_replay.stream/v1")

    def test_the_summary_line_has_exactly_the_four_published_keys(self) -> None:
        # Section 5.3 prints it as a COMPLETE literal with no ellipsis, unlike
        # the result line, whose payload it abbreviates.
        run = self.run_cli(
            "run",
            self.document,
            "--config",
            self.config,
            "--bars",
            self.bars,
            "--format",
            "ndjson",
        )
        summary = json.loads(run.stdout.splitlines()[1])
        self.assertEqual(set(summary), {"schema", "type", "sequence", "documents"})
        self.assertEqual(summary["documents"], 1)

    def test_the_stream_payload_is_byte_identical_to_the_single_value_form(self) -> None:
        # Section 5.3 says BYTE-identical, and that byte-identity is the stated
        # reason the transport wrapper exists at all. Comparing the PARSED
        # objects would accept a different key order, which is exactly the
        # difference a consumer reading raw bytes would see.
        run_single = self.run_cli(
            "run", self.document, "--config", self.config, "--bars", self.bars
        )
        run_stream = self.run_cli(
            "run",
            self.document,
            "--config",
            self.config,
            "--bars",
            self.bars,
            "--format",
            "ndjson",
        )
        line = run_stream.stdout.splitlines()[0]
        marker = '"data": '
        payload_bytes = line[line.index(marker) + len(marker) : -1]
        self.assertEqual(payload_bytes, run_single.stdout.rstrip("\n"))

    def test_the_stream_payload_equals_the_single_value_form(self) -> None:
        # Section 5.3: ``data`` is byte-identical to what ``--format json``
        # prints. This is why the trace is NOT routed by format.
        _, single = self._json()
        run = self.run_cli(
            "run",
            self.document,
            "--config",
            self.config,
            "--bars",
            self.bars,
            "--format",
            "ndjson",
        )
        self.assertEqual(json.loads(run.stdout.splitlines()[0])["data"], single)

    def test_the_trace_is_present_in_both_formats(self) -> None:
        _, single = self._json()
        run = self.run_cli(
            "run",
            self.document,
            "--config",
            self.config,
            "--bars",
            self.bars,
            "--format",
            "ndjson",
        )
        streamed = json.loads(run.stdout.splitlines()[0])["data"]
        self.assertIn("trace", single)
        self.assertEqual(streamed["trace"], single["trace"])


class DocumentRefusalTest(_Files):
    def test_bytes_that_are_not_json(self) -> None:
        path = self.write_text("bad.json", "{not json")
        failure = self.run_cli("run", path, "--config", self.config, "--bars", self.bars).failure(
            self
        )
        self.assertEqual(
            (failure["code"], failure["details"]["reason"]), ("intent_malformed", "not_json")
        )

    def test_bytes_that_are_not_utf8(self) -> None:
        path = self.write_bytes("bad.json", b'{"a": "\xff\xfe"}')
        failure = self.run_cli("run", path, "--config", self.config, "--bars", self.bars).failure(
            self
        )
        self.assertEqual(
            (failure["code"], failure["details"]["reason"]), ("intent_malformed", "not_json")
        )

    def test_a_repeated_key_names_every_repeated_key(self) -> None:
        path = self.write_text(
            "dup.json", '{"instrument": 1, "instrument": 2, "spec": 1, "spec": 2}'
        )
        failure = self.run_cli("run", path, "--config", self.config, "--bars", self.bars).failure(
            self
        )
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
            "--bars",
            self.bars,
            stdin=b'{"meta": {"source": "manual", "source": "brief"}}',
        ).failure(self)
        self.assertEqual(failure["details"], {"reason": "duplicate_key", "keys": ["source"]})

    def test_a_document_that_is_not_an_object(self) -> None:
        path = self.write_text("list.json", "[1, 2, 3]")
        failure = self.run_cli("run", path, "--config", self.config, "--bars", self.bars).failure(
            self
        )
        self.assertEqual(
            (failure["code"], failure["details"]["reason"]),
            ("intent_malformed", "schema_violation"),
        )
        self.assertEqual(failure["details"]["path"], "$")

    def test_a_missing_document_file_is_usage_with_a_runnable_suggestion(self) -> None:
        run = self.run_cli(
            "run", str(self.directory / "absent.json"), "--config", self.config, "--bars", self.bars
        )
        failure = run.failure(self)
        self.assertEqual((run.code, failure["code"]), (EXIT_USAGE, "usage"))
        self.assertEqual(failure["details"]["path"], str(self.directory / "absent.json"))
        self.assertIsInstance(failure["suggestions"][0]["argv"], list)
        self.assertEqual(failure["suggestions"][0]["argv"][:2], ["intent-replay", "run"])

    def test_a_door_refusal_is_intent_malformed_with_the_doors_reason(self) -> None:
        path = self.write("template.json", _document(dated=False))
        run = self.run_cli("run", path, "--config", self.config, "--bars", self.bars)
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
        failure = self.run_cli("run", path, "--config", self.config, "--bars", self.bars).failure(
            self
        )
        self.assertEqual(
            failure["details"],
            {"reason": "key_discarded", "paths": ["spec.entry_tiers[0].limit_pirce"]},
        )

    def test_an_incoherent_document_passes_through_as_intent_invalid(self) -> None:
        document = _document()
        document["spec"]["disaster_stop"] = 1000.0
        path = self.write("incoherent.json", document)
        run = self.run_cli("run", path, "--config", self.config, "--bars", self.bars)
        failure = run.failure(self)
        self.assertEqual(run.code, EXIT_FAILED)
        self.assertEqual(
            (failure["code"], failure["details"]["reason"]), ("intent_invalid", "stop_above_entry")
        )


class ConfigRefusalTest(_Files):
    def test_an_empty_block_is_config_incomplete_naming_the_seven_keys(self) -> None:
        path = self.write("empty.json", {})
        run = self.run_cli("run", self.document, "--config", path, "--bars", self.bars)
        failure = run.failure(self)
        self.assertEqual((run.code, failure["code"]), (EXIT_FAILED, "config_incomplete"))
        self.assertEqual(len(failure["details"]["keys"]), 7)

    def test_an_unusable_value_is_config_invalid(self) -> None:
        config = copy.deepcopy(CANONICAL)
        config["oco"] = True
        path = self.write("oco.json", config)
        failure = self.run_cli("run", self.document, "--config", path, "--bars", self.bars).failure(
            self
        )
        self.assertEqual(
            (failure["code"], failure["details"]["reason"]), ("config_invalid", "oco_unsupported")
        )

    def test_a_config_that_is_not_json_is_config_malformed(self) -> None:
        path = self.write_text("bad.json", "nope")
        run = self.run_cli("run", self.document, "--config", path, "--bars", self.bars)
        failure = run.failure(self)
        self.assertEqual((run.code, failure["code"]), (EXIT_FAILED, "config_malformed"))
        self.assertEqual(failure["details"], {"reason": "not_json", "path": path})

    def test_a_config_with_a_repeated_key_is_config_malformed(self) -> None:
        path = self.write_text("dup.json", '{"oco": false, "oco": true}')
        failure = self.run_cli("run", self.document, "--config", path, "--bars", self.bars).failure(
            self
        )
        self.assertEqual(
            failure["details"], {"reason": "duplicate_key", "keys": ["oco"], "path": path}
        )

    def test_a_missing_config_file_is_usage(self) -> None:
        absent = str(self.directory / "absent.json")
        run = self.run_cli("run", self.document, "--config", absent, "--bars", self.bars)
        failure = run.failure(self)
        self.assertEqual(
            (run.code, failure["code"], failure["details"]["path"]), (EXIT_USAGE, "usage", absent)
        )

    def test_the_document_is_judged_before_the_config(self) -> None:
        template = self.write("template.json", _document(dated=False))
        empty = self.write("empty.json", {})
        failure = self.run_cli("run", template, "--config", empty, "--bars", self.bars).failure(
            self
        )
        self.assertEqual(failure["code"], "intent_malformed")


class BarRefusalTest(_Files):
    """The four bar codes shipped in PR 1 and were unreachable from the command
    until it took bars; `bars_malformed` is the CLI's own half, the file that is
    not one JSON document at all."""

    def _refuse(self, bars_path: str) -> dict[str, Any]:
        return self.run_cli(
            "run", self.document, "--config", self.config, "--bars", bars_path
        ).failure(self)

    def test_a_bars_file_that_is_not_json_is_bars_malformed(self) -> None:
        path = self.write_text("bad.json", "nope")
        run = self.run_cli("run", self.document, "--config", self.config, "--bars", path)
        failure = run.failure(self)
        self.assertEqual((run.code, failure["code"]), (EXIT_FAILED, "bars_malformed"))
        self.assertEqual(failure["details"], {"reason": "not_json", "path": path})

    def test_a_bars_file_with_a_repeated_key_is_bars_malformed(self) -> None:
        path = self.write_text("dup.json", '[{"t": 1, "t": 2}]')
        failure = self._refuse(path)
        self.assertEqual(
            failure["details"], {"reason": "duplicate_key", "keys": ["t"], "path": path}
        )

    def test_a_missing_bars_file_is_usage(self) -> None:
        absent = str(self.directory / "absent.json")
        run = self.run_cli("run", self.document, "--config", self.config, "--bars", absent)
        failure = run.failure(self)
        self.assertEqual(
            (run.code, failure["code"], failure["details"]["path"]), (EXIT_USAGE, "usage", absent)
        )

    def test_bar_input_that_is_not_the_published_shape_is_bars_invalid(self) -> None:
        path = self.write("typo.json", [{**BARS[0], "hgih": 1.0}])
        failure = self._refuse(path)
        self.assertEqual(
            (failure["code"], failure["details"]["reason"]), ("bars_invalid", "unknown_key")
        )

    def test_an_empty_bar_array_is_bars_empty(self) -> None:
        self.assertEqual(self._refuse(self.write("empty.json", []))["code"], "bars_empty")

    def test_unordered_bars_are_bars_unordered(self) -> None:
        failure = self._refuse(self.write("unordered.json", [BARS[1], BARS[0]]))
        self.assertEqual(
            (failure["code"], failure["details"]["reason"]), ("bars_unordered", "decreasing")
        )

    def test_bars_that_miss_the_walk_start_are_window_too_short(self) -> None:
        early = [{**BARS[0], "t": WALK_START - 120_000}, {**BARS[1], "t": WALK_START - 60_000}]
        failure = self._refuse(self.write("early.json", early))
        self.assertEqual(
            (failure["code"], failure["details"]["reason"]),
            ("window_too_short", "ends_before_walk_start"),
        )

    def test_the_config_is_judged_before_the_bars(self) -> None:
        # The bars are the LAST gate: a caller fixes the document, then the
        # block, then the price input.
        empty = self.write("empty-config.json", {})
        bad_bars = self.write_text("bad-bars.json", "nope")
        failure = self.run_cli("run", self.document, "--config", empty, "--bars", bad_bars).failure(
            self
        )
        self.assertEqual(failure["code"], "config_incomplete")


class UsageTest(_Files):
    def _usage(self, *argv: str) -> dict[str, Any]:
        run = self.run_cli(*argv)
        failure = run.failure(self)
        self.assertEqual((run.code, failure["code"]), (EXIT_USAGE, "usage"))
        return failure

    def test_no_command(self) -> None:
        self._usage()

    def test_a_missing_config_option(self) -> None:
        self._usage("run", self.document, "--bars", self.bars)

    def test_a_missing_bars_option(self) -> None:
        # Required, not optional-with-a-default: there is no bar series to
        # invent, and a run without bars would answer about nothing.
        self._usage("run", self.document, "--config", self.config)

    def test_an_unknown_format(self) -> None:
        self._usage(
            "run", self.document, "--config", self.config, "--bars", self.bars, "--format", "human"
        )

    def test_an_abbreviated_option_is_not_understood(self) -> None:
        self._usage("run", self.document, "--conf", self.config, "--bars", self.bars)

    def test_an_unknown_option(self) -> None:
        self._usage("run", self.document, "--config", self.config, "--bars", self.bars, "--bogus")

    def test_an_unknown_command(self) -> None:
        self._usage("walk")

    def test_schema_of_an_unknown_command(self) -> None:
        self._usage("schema", "walk")


class InterpreterRefusalTest(_Files):
    """The interpreter's own refusals, reached through `run` (PR 5)."""

    def test_an_immediate_tranche_is_refused_naming_its_tier(self) -> None:
        path = self.write("immediate.json", _document("immediate-plus-pullback"))
        run = self.run_cli("run", path, "--config", self.config, "--bars", self.bars)
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
            run = self.run_cli("run", self.document, "--config", self.config, "--bars", self.bars)
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
            run = self.run_cli("run", self.document, "--config", self.config, "--bars", self.bars)
        failure = run.failure(self)
        self.assertEqual((run.code, failure["code"]), (EXIT_FAILED, "made_up"))
        self.assertTrue(any("made_up" in line for line in logs.output))

    def test_an_interrupt_exits_130_with_nothing_written(self) -> None:
        with mock.patch("intent_replay.door.admit", side_effect=KeyboardInterrupt):
            run = self.run_cli("run", self.document, "--config", self.config, "--bars", self.bars)
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

    def test_the_help_body_does_not_still_promise_an_empty_success(self) -> None:
        # Until PR 7 six published sentences said an accepted document prints
        # nothing, and NONE of them had a gate: the manifest test asserts only
        # the SET of exit-code keys and the headings test only their order, so
        # both bodies went unread. One assertion covers the OUTPUT section and
        # the EXIT CODES table at once.
        text = render_help("run")
        body = text[text.index("OUTPUT") :]
        self.assertNotIn("nothing is printed", body)
        self.assertNotIn("nothing on an accepted document", body)
        self.assertIn("stdout", body)
        self.assertIn("ndjson", body)

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

    def test_the_cli_owns_exactly_these_codes(self) -> None:
        # The SET, not a membership loop: the old shape stayed green when a
        # fourth CLI code arrived, so it could not report the thing it was for.
        owned = {code for code, owner in OWNERS.items() if owner == "CLI"}
        self.assertEqual(owned, {"intent_malformed", "config_malformed", "bars_malformed", "usage"})
        for code in owned:
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
        self.assertEqual(set(BARS_MALFORMED_REASONS), {"not_json", "duplicate_key"})


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
