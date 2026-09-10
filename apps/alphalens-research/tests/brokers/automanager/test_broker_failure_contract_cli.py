"""The broker group's FAILURE contract (#1389).

The success half is #1379 (`test_broker_json_contract_cli.py`): one envelope,
one JSON value on stdout. This file holds the other half — what a caller gets
when the command refuses or breaks.

Four rules:

* **the format decides the rendering, and nothing else does** — ``--format
  human`` (or no ``--format``) keeps the red prose an operator reads;
  ``--format json`` writes exactly the failure object on stderr. Before this,
  ``stream-status`` wrote its JSON error unconditionally, so a human got a blob
  and every other command's machine caller got prose;
* **stdout stays EMPTY on a failure in JSON mode** — the doctrine rule, and the
  easiest one to break by rendering a partial success envelope first;
* **the code comes from the registry, never from a literal at the call site** —
  checked by walking the module's AST, with a positive control so the walk
  cannot rot into a tautology;
* **a non-retryable code whose recovery is mechanical carries a runnable
  argv** — a machine reads ``retryable: false`` as "drop it", so
  ``write_outcome_unknown`` without a reconcile command means "lose the intent".
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from broker_contract.contract import (
    BrokerAuthError,
    BrokerError,
    BrokerRateLimitError,
    BrokerTransientError,
    WriteOutcomeUnknownError,
)
from typer.testing import CliRunner

REGISTRY_SEAM = "alphalens_pipeline.brokers.registry.get_default_broker"

BROKER_CLI = (
    Path(__file__).resolve().parents[4]
    / "alphalens-pipeline"
    / "alphalens_cli"
    / "commands"
    / "broker.py"
)


def _json_lines(stderr: str) -> list[dict]:
    """Every stderr line that parses as a JSON object."""
    found = []
    for line in stderr.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


def _failure_from(result) -> dict:
    """The failure object out of a JSON-mode stderr.

    stderr is the HUMAN + log stream for this group even in JSON mode — the
    `env=… gateway=…` banner and the malformed-line warnings already print
    there alongside a JSON stdout (#1379). So the machine contract is not
    "stderr is one object"; it is "exactly one stderr line is a JSON object,
    and it is the last line". Both halves are asserted in
    :class:`TheFailureObjectIsFindable`, which is what makes this helper safe.
    """
    objects = _json_lines(result.stderr)
    assert len(objects) == 1, f"expected exactly one JSON line on stderr, got {objects}"
    return objects[0]


def _module_ast() -> ast.Module:
    return ast.parse(BROKER_CLI.read_text(encoding="utf-8"), filename=str(BROKER_CLI))


# The ONE documented indirection: this helper reads the code off the caught
# exception's class, which is the whole design (the taxonomy classifies, not the
# call site). Its codes are covered by `TestFailureCodeBinding` in
# `test_broker_contract.py`, which pins every class to a registered code.
_CODE_FROM_EXCEPTION = "_fail_from_broker_error"


def _fail_with_codes(tree: ast.Module) -> list[str | None]:
    """Every literal ``code=`` / first positional passed to ``_fail_with``.

    ``None`` marks a call whose code is not a plain string literal — the shape
    this walk cannot vouch for, and which must therefore not exist outside the
    one exempt helper.
    """
    exempt = {
        node
        for parent in ast.walk(tree)
        if isinstance(parent, ast.FunctionDef) and parent.name == _CODE_FROM_EXCEPTION
        for node in ast.walk(parent)
    }
    codes: list[str | None] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "_fail_with" or node in exempt:
            continue
        arg: ast.expr | None = node.args[0] if node.args else None
        for keyword in node.keywords:
            if keyword.arg == "code":
                arg = keyword.value
        codes.append(arg.value if isinstance(arg, ast.Constant) else None)
    return codes


class _FailureCliCase(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        home_patcher = mock.patch("pathlib.Path.home", return_value=self.home)
        home_patcher.start()
        self.addCleanup(home_patcher.stop)

        self.textfile_dir = self.home / "textfiles"
        self.textfile_dir.mkdir()
        env_patcher = mock.patch.dict(
            "os.environ", {"ALPHALENS_TEXTFILE_DIR": str(self.textfile_dir)}, clear=True
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def invoke(self, argv: list[str], *, broker_raises: Exception | None = None):
        from alphalens_cli.commands.broker import broker_app

        broker = mock.Mock()
        if broker_raises is not None:
            broker.get_account.side_effect = broker_raises
        with mock.patch(REGISTRY_SEAM, return_value=broker):
            return self.runner.invoke(broker_app, argv)


class FormatDecidesTheRendering(_FailureCliCase):
    def test_human_mode_prints_prose_and_no_json(self) -> None:
        result = self.invoke(["account"], broker_raises=BrokerError("upstream boom"))

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("upstream boom", result.stderr)
        self.assertEqual(_json_lines(result.stderr), [])

    def test_json_mode_prints_exactly_the_failure_object(self) -> None:
        result = self.invoke(
            ["account", "--format", "json"], broker_raises=BrokerError("upstream boom")
        )

        failure = _failure_from(result)
        self.assertEqual(
            sorted(failure), ["code", "details", "message", "retryable", "suggestions"]
        )
        self.assertEqual(failure["code"], "broker_failed")
        self.assertFalse(failure["retryable"])

    def test_stdout_is_empty_when_a_json_mode_command_fails(self) -> None:
        result = self.invoke(
            ["account", "--format", "json"], broker_raises=BrokerError("upstream boom")
        )

        self.assertEqual(result.stdout, "")

    def test_a_command_without_the_option_keeps_its_prose(self) -> None:
        """``--format`` is per command; a group-wide switch would change the
        rendering of commands that never opted in."""
        result = self.invoke(["auth", "--status"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(_json_lines(result.stderr), [])


class TheFailureObjectIsFindable(_FailureCliCase):
    """How a machine gets the object off a stream that also carries prose.

    This group prints operator chrome on stderr — the `env=… gateway=…` banner,
    malformed-line warnings — and did so before this ticket, alongside a JSON
    stdout. Suppressing all of it in JSON mode would be a bigger change than the
    failure contract needs, and would drop warnings a human still wants when
    they run the command by hand. So the contract states what is true and
    checkable instead: exactly ONE stderr line is a JSON object, and it is the
    last one.
    """

    def test_exactly_one_stderr_line_is_a_json_object(self) -> None:
        result = self.invoke(
            ["account", "--format", "json"], broker_raises=BrokerError("upstream boom")
        )

        self.assertEqual(len(_json_lines(result.stderr)), 1, result.stderr)

    def test_the_failure_object_is_the_last_line(self) -> None:
        """Nothing prints after a refusal — so a caller can read the last line
        rather than scan, and a future `finally` that echoed something would
        break here rather than in production."""
        result = self.invoke(
            ["account", "--format", "json"], broker_raises=BrokerError("upstream boom")
        )

        last = result.stderr.splitlines()[-1]
        self.assertEqual(json.loads(last)["code"], "broker_failed")

    def test_the_operator_banner_is_still_there_for_a_human_reading_along(self) -> None:
        """Positive control: the assertion above would also pass if we had
        silently dropped the chrome, which is a change this ticket did not make."""
        result = self.invoke(
            ["account", "--format", "json"], broker_raises=BrokerError("upstream boom")
        )

        self.assertIn("gateway=", result.stderr)


class TheExceptionDecidesTheCode(_FailureCliCase):
    """The taxonomy classifies, not the 81 call sites (#1389).

    Most refusals in this module are ``_fail(str(exc))`` pass-throughs, so
    hand-annotating call sites would have meant annotating the same fact many
    times and getting it wrong somewhere.
    """

    CASES = (
        (BrokerTransientError("dns"), "broker_transient", True, 7),
        (BrokerRateLimitError("429"), "broker_rate_limited", True, 7),
        (WriteOutcomeUnknownError("500 on POST"), "write_outcome_unknown", False, 1),
        (BrokerAuthError("401"), "broker_auth", False, 1),
        (BrokerError("boom"), "broker_failed", False, 1),
    )

    def test_each_broker_error_reports_its_own_code_and_exit_status(self) -> None:
        for exc, code, retryable, exit_code in self.CASES:
            with self.subTest(exc=type(exc).__name__):
                result = self.invoke(["account", "--format", "json"], broker_raises=exc)
                failure = _failure_from(result)
                self.assertEqual(failure["code"], code)
                self.assertEqual(failure["retryable"], retryable)
                self.assertEqual(result.exit_code, exit_code)

    def test_the_transient_case_is_the_one_that_could_have_refuted_the_split(self) -> None:
        """Before #1389 this exact failure — the 2026-09-08 DNS outage shape —
        arrived as a bare ``BrokerError`` and would have been published as
        ``retryable: false``."""
        result = self.invoke(
            ["account", "--format", "json"], broker_raises=BrokerTransientError("dns fail")
        )

        self.assertTrue(_failure_from(result)["retryable"])

    def test_an_ambiguous_write_is_not_retryable_and_says_how_to_recover(self) -> None:
        """Transient cause, forbidden retry. The client must be handed the
        reconcile command, because ``retryable: false`` alone reads as "drop
        it"."""
        result = self.invoke(
            ["account", "--format", "json"],
            broker_raises=WriteOutcomeUnknownError("500 after POST"),
        )

        failure = _failure_from(result)
        self.assertFalse(failure["retryable"])
        self.assertTrue(failure["suggestions"], "a mechanical recovery must carry an argv")
        self.assertTrue(all(s["argv"] for s in failure["suggestions"]))


class UsageRefusalsAreTheirOwnClass(_FailureCliCase):
    def test_an_unknown_format_is_a_usage_error(self) -> None:
        result = self.invoke(["account", "--format", "xml"])

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.stdout, "")

    def test_a_usage_refusal_renders_as_prose_because_the_format_never_resolved(self) -> None:
        """The one place prose is correct in a would-be JSON call: the caller's
        ``--format`` was not a value we understand, so there is no format to
        honour."""
        result = self.invoke(["account", "--format", "xml"])

        self.assertIn("--format", result.stderr)
        self.assertEqual(_json_lines(result.stderr), [])


class MissingTextfileKeepsItsCodeAndStatus(_FailureCliCase):
    """``stream-status`` shipped the shape this ticket generalises; absorbing it
    into the emitter must not change its code or its exit status."""

    def test_json_mode_still_answers_with_the_not_found_object(self) -> None:
        result = self.invoke(["stream-status", "--format", "json"])

        failure = _failure_from(result)
        self.assertEqual(failure["code"], "stream_metrics_missing")
        self.assertEqual(result.exit_code, 4)
        self.assertTrue(failure["suggestions"][0]["argv"])

    def test_human_mode_now_gets_prose_instead_of_a_blob(self) -> None:
        """The behaviour change this PR makes deliberately: before #1389 the
        object was written on stderr unconditionally, including for a human."""
        result = self.invoke(["stream-status"])

        self.assertEqual(result.exit_code, 4)
        self.assertEqual(_json_lines(result.stderr), [])


class TheFormatHandleDoesNotLeakBetweenCommands(_FailureCliCase):
    """A module-level handle is how ``_fail`` learns the format without
    threading it through 81 call sites — and is exactly what can leak in-process.

    The test sets the handle DIRECTLY rather than running a json command first:
    two ``invoke`` calls in a row would pass even if the reset did nothing,
    because ``CliRunner`` may build a fresh context each time. This shape can
    actually produce the refuting observation.
    """

    def test_a_command_without_the_option_renders_prose_even_after_a_json_call(self) -> None:
        from alphalens_cli.commands import broker as broker_cli

        token = broker_cli._OUTPUT_FORMAT.set("json")
        try:
            result = self.invoke(["auth", "--status"])
        finally:
            broker_cli._OUTPUT_FORMAT.reset(token)

        self.assertEqual(_json_lines(result.stderr), [])


class AMutatorRendersBeforeItWrites(unittest.TestCase):
    """A command that appends must not report a failure for work it already did.

    `arm` used to write the pick and THEN serialise the envelope. An
    unrenderable payload would have exited non-zero with the pick queued —
    exactly the `write_outcome_unknown` shape this ticket exists to keep out of
    a client's hands, manufactured by our own renderer. Pinned by source order
    rather than by simulating a JSON failure: the payload is plain scalars, so
    the failure is not reachable from outside, which is why the invariant needs
    a structural check instead of a behavioural one.
    """

    ARMING_COMMANDS = ("arm_command", "arm_manual_command")

    def test_the_json_branch_renders_before_it_arms(self) -> None:
        tree = _module_ast()
        for func in ast.walk(tree):
            if not (isinstance(func, ast.FunctionDef) and func.name in self.ARMING_COMMANDS):
                continue
            with self.subTest(command=func.name):
                # By LINE, not by walk order: `ast.walk` is breadth-first and
                # says nothing about which statement runs first.
                calls: dict[str, list[int]] = {"_render_json": [], "arm_pick": []}
                for node in ast.walk(func):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id in calls
                    ):
                        calls[node.func.id].append(node.lineno)
                self.assertTrue(calls["_render_json"], "the JSON branch must pre-render")
                self.assertTrue(calls["arm_pick"], "the command must actually arm")
                self.assertLess(
                    min(calls["_render_json"]),
                    min(calls["arm_pick"]),
                    "render must come before the append",
                )

    def test_both_arming_commands_were_actually_found(self) -> None:
        """Guard against the walk passing because it matched nothing."""
        tree = _module_ast()
        found = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name in self.ARMING_COMMANDS
        }

        self.assertEqual(found, set(self.ARMING_COMMANDS))


class EveryEmittedCodeIsRegistered(unittest.TestCase):
    """Anti-rot gate over the module's AST.

    Without it, a call site can invent a code that no published table lists and
    nothing goes red — which is the whole failure mode a "stable code" promise
    has.
    """

    def test_every_fail_with_call_uses_a_registered_code(self) -> None:
        from alphalens_cli.commands.broker import _FAILURE_CODES

        for code in _fail_with_codes(_module_ast()):
            self.assertIsNotNone(code, "_fail_with must be called with a literal code")
            self.assertIn(code, _FAILURE_CODES)

    def test_the_walk_can_actually_fail(self) -> None:
        """Positive control: the same walk over a snippet with an invented code
        must find it, so a green run above means something."""
        tree = ast.parse("_fail_with('invented_code', 'boom')")

        self.assertEqual(_fail_with_codes(tree), ["invented_code"])

    def test_the_walk_finds_the_keyword_form_too(self) -> None:
        tree = ast.parse("_fail_with(code='usage', message='boom')")

        self.assertEqual(_fail_with_codes(tree), ["usage"])

    def test_the_walk_flags_a_non_literal_code(self) -> None:
        tree = ast.parse("_fail_with(some_variable, 'boom')")

        self.assertEqual(_fail_with_codes(tree), [None])

    def test_the_exemption_covers_exactly_one_helper(self) -> None:
        """The exemption is a hole in the gate, so it must stay one hole. If a
        second helper starts resolving codes at runtime, this fails and forces
        the decision to be made again rather than inherited."""
        tree = _module_ast()
        resolving = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_fail_with"
                and not (call.args and isinstance(call.args[0], ast.Constant))
                and not any(
                    kw.arg == "code" and isinstance(kw.value, ast.Constant) for kw in call.keywords
                )
                for call in ast.walk(node)
            )
        }

        self.assertEqual(resolving, {_CODE_FROM_EXCEPTION})

    def test_the_module_actually_emits_codes(self) -> None:
        """Guard against the gate passing because it found nothing at all."""
        self.assertGreater(len(_fail_with_codes(_module_ast())), 5)


class TheRegistryHonoursItsOwnRules(unittest.TestCase):
    def test_the_cli_registry_adds_only_cli_concepts(self) -> None:
        """The contract package must not learn the CLI's vocabulary (#1122)."""
        from alphalens_cli.commands.broker import _CLI_FAILURE_CODES
        from broker_contract.failure import CONTRACT_FAILURE_CODES

        self.assertEqual(set(_CLI_FAILURE_CODES) & set(CONTRACT_FAILURE_CODES), set())

    def test_the_merged_registry_is_what_the_emitter_validates_against(self) -> None:
        from alphalens_cli.commands.broker import _CLI_FAILURE_CODES, _FAILURE_CODES
        from broker_contract.failure import CONTRACT_FAILURE_CODES

        self.assertEqual(set(_FAILURE_CODES), set(CONTRACT_FAILURE_CODES) | set(_CLI_FAILURE_CODES))

    def test_unclassified_is_present_and_not_retryable(self) -> None:
        """The code that makes the work finite: a refusal nobody has classified
        yet still answers in JSON rather than leaking prose to a machine."""
        from alphalens_cli.commands.broker import _FAILURE_CODES

        self.assertIn("unclassified", _FAILURE_CODES)
        self.assertFalse(_FAILURE_CODES["unclassified"].retryable)

    def test_a_plain_fail_reports_unclassified_and_keeps_exit_one(self) -> None:
        """``_fail`` stays, so the long tail of refusals is classified over time
        instead of being a precondition — and a machine still gets an object
        rather than prose. Its exit status is unchanged, so nothing that runs
        this CLI today sees a different result for an unclassified refusal."""
        from alphalens_cli.commands.broker import _OUTPUT_FORMAT, _fail

        buffer = io.StringIO()
        token = _OUTPUT_FORMAT.set("json")
        try:
            with contextlib.redirect_stderr(buffer):
                exit_exc = _fail("something not yet classified")
        finally:
            _OUTPUT_FORMAT.reset(token)

        self.assertEqual(exit_exc.exit_code, 1)
        failure = json.loads(buffer.getvalue())
        self.assertEqual(failure["code"], "unclassified")
        self.assertEqual(failure["message"], "something not yet classified")


if __name__ == "__main__":
    unittest.main()
