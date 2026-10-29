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
  ``write_outcome_unknown`` without a reconcile command means "lose the intent";
* **a ``broker_auth`` emitter that KNOWS its rail says so** — the table default
  can only promise that something runnable comes out, never that it is the most
  specific thing, so "remember the per-rail constant" is checked here rather
  than left to review (#1811).
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

_COMMANDS_DIR = (
    Path(__file__).resolve().parents[4] / "alphalens-pipeline" / "alphalens_cli" / "commands"
)

BROKER_CLI = _COMMANDS_DIR / "broker.py"

#: The envelope and the failure emitters, moved out of `broker.py` in #1689.
#: `_fail_with` is DEFINED here, so this is where its unregistered-code warning
#: is logged and where a second code-resolving helper would hide.
ENVELOPE_CLI = _COMMANDS_DIR / "_cli_envelope.py"

#: The second group built on the same envelope (#1689). It emits codes of its
#: own, so the registry walk must read it too.
CASHBOOK_CLI = _COMMANDS_DIR / "cashbook.py"

#: Every module that reaches the emitters. One entry per module, so a new group
#: built on this envelope is added here rather than silently uncovered.
EMITTING_CLI_MODULES = (BROKER_CLI, ENVELOPE_CLI, CASHBOOK_CLI)


def _modules_importing_the_envelope() -> set[Path]:
    """Every command module that reaches the emitters, found by its IMPORTS.

    The floor under :data:`EMITTING_CLI_MODULES`: that tuple is hand-written,
    so without this a new group built on the shared envelope — or an existing
    entry deleted — leaves the registry walks above reading fewer modules than
    emit codes, which is green and blind.
    """
    found: set[Path] = set()
    for path in sorted(_COMMANDS_DIR.glob("*.py")):
        if path == ENVELOPE_CLI:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and node.module.split(".")[-1] == ENVELOPE_CLI.stem
            ):
                found.add(path)
    return found


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
    """``broker.py`` ALONE.

    Kept for the two walks whose subject is a broker command: the arming
    commands stayed here, so widening them to the whole family would assert a
    property of `broker.py` against modules that do not arm.
    """
    return ast.parse(BROKER_CLI.read_text(encoding="utf-8"), filename=str(BROKER_CLI))


def _module_asts() -> tuple[ast.Module, ...]:
    """Every module that emits a failure code through this one contract.

    The registry walks must read every one of them: after #1689 the emitters
    live in ``_cli_envelope.py``, so a walk over ``broker.py`` alone would stop
    seeing the helper definitions and would see no sibling group's codes at
    all — green, and blind.
    """
    return tuple(
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in EMITTING_CLI_MODULES
    )


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


#: The code whose per-cause recovery is a convention the table cannot enforce.
_MULTI_CAUSE_CODE = "broker_auth"

#: The per-rail recovery constants an emitter of :data:`_MULTI_CAUSE_CODE` may
#: pass, matched by PREFIX rather than by exact name so a third rail's constant
#: is accepted once it follows the same spelling. What each one contains is
#: asserted behaviourally in ``test_broker_auth_suggestion_cli.py``; this walk
#: only checks that a site which knows its rail named one.
_AUTH_RECOVERY_PREFIXES = ("_SIM_AUTH_", "_LIVE_AUTH_")

#: The five known ``broker_auth`` sites that know their own cause: two on the
#: SIM store read (``broker.py`` ~495 and ~514) and three on the LIVE
#: market-data read (~772, ~795 and the legacy-store branch at ~806). A floor,
#: not an equality: a sixth site that passes its constant is fine, a deleted one
#: must not leave this walk asserting a property of nothing.
_KNOWN_AUTH_SITES = 5


def _code_of(call: ast.Call) -> str | None:
    """The literal failure code of a ``_fail_with`` call, or ``None``."""
    arg: ast.expr | None = call.args[0] if call.args else None
    for keyword in call.keywords:
        if keyword.arg == "code":
            arg = keyword.value
    return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None


def _auth_recovery_names(tree: ast.Module) -> list[tuple[str | None, int]]:
    """The ``suggestions=`` name each ``broker_auth`` site passes, one per site.

    Each entry is ``(name, lineno)``. ``None`` marks a site that passed
    nothing, or passed something this walk cannot read as a named constant —
    the shape that silently degrades to the table's two store reads.
    """
    names: list[tuple[str | None, int]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "_fail_with" or _code_of(node) != _MULTI_CAUSE_CODE:
            continue
        passed = next((kw.value for kw in node.keywords if kw.arg == "suggestions"), None)
        # The line number rides along so a refusal can say WHICH site is bare.
        # Its sibling gate already subTests on the module name; a failure that
        # names neither sends the reader to grep for five call sites.
        names.append((passed.id if isinstance(passed, ast.Name) else None, node.lineno))
    return names


def _string_tuple(node: ast.expr | None) -> tuple[str, ...] | None:
    """A literal tuple or list of string constants, or ``None``.

    An EMPTY literal comes back as ``()``, not ``None``: "declared with nothing
    to run" and "built at runtime, unreadable" are different observations and
    both must fail, so they must not collapse into one value.
    """
    if not isinstance(node, ast.Tuple | ast.List):
        return None
    elements: list[str] = []
    for elt in node.elts:
        if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
            return None
        elements.append(elt.value)
    return tuple(elements)


def _module_level_string_tuples(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    """Module-level names bound to a tuple of string literals.

    The argvs are extracted into constants beside the table they serve, so a
    walk that only read literal tuples would call four of the nine resolvable
    ``Suggestion`` argvs "unreadable" and go red on correct code.
    """
    bound: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        # BOTH binding forms. A review mutation showed the cost of reading only
        # ``ast.Assign``: annotating one of these constants -- which is the style
        # of its own neighbour, ``_SIM_AUTH_BOOTSTRAP: tuple[Suggestion, ...]``
        # -- turned this gate red on correct code, blaming the ``Suggestion`` for
        # an argv the walk simply could not read.
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        else:
            continue
        literal = _string_tuple(value)
        if isinstance(target, ast.Name) and literal is not None:
            bound[target.id] = literal
    return bound


def _suggestion_argvs(tree: ast.Module) -> list[tuple[str, ...] | None]:
    """Every ``Suggestion(...)`` construction's argv, resolved where it can be.

    One entry per construction, in walk order. ``None`` marks an argv this walk
    cannot vouch for, which must not exist in these modules either.
    """
    bound = _module_level_string_tuples(tree)
    argvs: list[tuple[str, ...] | None] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "Suggestion":
            continue
        value = next((kw.value for kw in node.keywords if kw.arg == "argv"), None)
        if value is None and node.args:
            value = node.args[0]
        argvs.append(bound.get(value.id) if isinstance(value, ast.Name) else _string_tuple(value))
    return argvs


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

    `broker arm` (deleted in #1469) used to write the pick and THEN serialise the envelope. An
    unrenderable payload would have exited non-zero with the pick queued —
    exactly the `write_outcome_unknown` shape this ticket exists to keep out of
    a client's hands, manufactured by our own renderer. Pinned by source order
    rather than by simulating a JSON failure: the payload is plain scalars, so
    the failure is not reachable from outside, which is why the invariant needs
    a structural check instead of a behavioural one.
    """

    ARMING_COMMANDS = ("arm_command",)

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

        for tree in _module_asts():
            for code in _fail_with_codes(tree):
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
        resolving = {
            node.name
            for tree in _module_asts()
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
        emitted = [code for tree in _module_asts() for code in _fail_with_codes(tree)]

        self.assertGreater(len(emitted), 5)


class EveryAuthSiteNamesItsOwnRail(unittest.TestCase):
    """The one gap the table default cannot close (#1811).

    ``_SUGGESTIONS_BY_CODE["broker_auth"]`` guarantees that SOMETHING runnable
    comes out of this code — it is the two offline store reads, true for all
    five causes. It cannot guarantee the most SPECIFIC thing: a site that knows
    it is on the LIVE market-data rail has to pass
    ``_LIVE_AUTH_BOOTSTRAP`` itself, and a site added later that forgets
    degrades to "read both stores first" with nothing going red.

    So the convention is checked structurally instead, by the same walk that
    already requires a literal code. The exception-classified path in
    ``_fail_from_broker_error`` is not a site here and must not be: it holds a
    ``BrokerAuthError`` whose rail it does not know, which is exactly the case
    the table default exists for.
    """

    def test_every_broker_auth_site_passes_a_per_rail_recovery(self) -> None:
        for path, tree in zip(EMITTING_CLI_MODULES, _module_asts(), strict=True):
            for name, lineno in _auth_recovery_names(tree):
                where = f"{path.name}:{lineno}"
                with self.subTest(site=where):
                    if name is None:
                        self.fail(
                            f"{where}: this {_MULTI_CAUSE_CODE} site passed no named "
                            f"recovery; pass one of {_AUTH_RECOVERY_PREFIXES}*"
                        )
                    self.assertTrue(
                        name.startswith(_AUTH_RECOVERY_PREFIXES),
                        f"{where}: {name} is not a per-rail recovery constant",
                    )

    def test_the_walk_found_every_known_site(self) -> None:
        """Non-vacuity: the assertions above hold trivially over an empty walk,
        which is what deleting a site or renaming ``_fail_with`` would produce."""
        found = [entry for tree in _module_asts() for entry in _auth_recovery_names(tree)]

        self.assertGreaterEqual(len(found), _KNOWN_AUTH_SITES, found)

    def test_the_walk_catches_a_site_that_forgot(self) -> None:
        """Positive control: a site with no ``suggestions=`` must read as
        ``None``, or a green run above means only that the walk matched nothing
        it could object to."""
        tree = ast.parse("_fail_with('broker_auth', 'refresh chain is dead')")

        self.assertEqual([name for name, _line in _auth_recovery_names(tree)], [None])

    def test_the_walk_catches_a_recovery_that_is_not_a_per_rail_constant(self) -> None:
        """A name the walk CAN read but must still reject — otherwise the gate
        would accept any constant at all and only check that one was passed."""
        passed = "_SOMETHING_ELSE"
        tree = ast.parse(f"_fail_with('broker_auth', 'boom', suggestions={passed})")

        self.assertEqual([name for name, _line in _auth_recovery_names(tree)], [passed])
        self.assertFalse(passed.startswith(_AUTH_RECOVERY_PREFIXES))

    def test_the_walk_reads_the_keyword_code_form_too(self) -> None:
        tree = ast.parse("_fail_with(code='broker_auth', message='boom')")

        self.assertEqual([name for name, _line in _auth_recovery_names(tree)], [None])

    def test_another_code_is_not_this_walks_business(self) -> None:
        """Only a multi-cause code needs this; a single-cause code's default IS
        its most specific answer, so requiring a constant there would be noise."""
        tree = ast.parse("_fail_with('usage', 'boom')")

        self.assertEqual(_auth_recovery_names(tree), [])


class EverySuggestionCarriesARunnableArgv(unittest.TestCase):
    """The hole next to the table: a NON-EMPTY tuple of EMPTY argvs.

    ``test_every_code_that_demands_a_suggestion_has_one_in_the_table`` checks
    that the entry exists. ``Suggestion(argv=(), why="...")`` would satisfy it
    and publish a sentence with nothing to run, which is the shape the doctrine
    forbids ("a runnable next command, not a sentence to parse"). Validating in
    ``Suggestion.__post_init__`` would be cleaner and is deliberately not done:
    that class is in the shared contract package.
    """

    def test_every_suggestion_in_the_cli_has_a_non_empty_argv(self) -> None:
        for path, tree in zip(EMITTING_CLI_MODULES, _module_asts(), strict=True):
            for argv in _suggestion_argvs(tree):
                with self.subTest(module=path.name, argv=argv):
                    self.assertTrue(argv, "a Suggestion must carry a runnable argv")

    def test_the_walk_found_every_suggestion_the_cli_builds(self) -> None:
        """Non-vacuity, with the measured count as the floor: eight entries in
        ``_cli_envelope.py`` (two per-rail recoveries plus six table rows) and
        ``_TEMPLATE_SUGGESTION`` in ``broker.py``."""
        found = [argv for tree in _module_asts() for argv in _suggestion_argvs(tree)]

        self.assertGreaterEqual(len(found), 9, found)

    def test_the_walk_catches_a_declared_empty_argv(self) -> None:
        tree = ast.parse("Suggestion(argv=(), why='read the docs')")

        self.assertEqual(_suggestion_argvs(tree), [()])

    def test_the_walk_catches_an_argv_it_cannot_read(self) -> None:
        """An argv built at runtime is not something this walk can vouch for, so
        it fails rather than passing on trust."""
        tree = ast.parse("Suggestion(argv=tuple(parts), why='boom')")

        self.assertEqual(_suggestion_argvs(tree), [None])

    def test_the_walk_resolves_a_module_level_argv_constant(self) -> None:
        """Positive control for the indirection the real modules use: without
        this resolution the gate would call a correct argv unreadable."""
        tree = ast.parse(
            "_ARGV = ('alphalens', 'broker', 'auth', '--status')\n"
            "S = Suggestion(argv=_ARGV, why='read the store')\n"
        )

        self.assertEqual(_suggestion_argvs(tree), [("alphalens", "broker", "auth", "--status")])


class TheWalkedModuleListIsComplete(unittest.TestCase):
    """The list of walked modules is itself checked, in both directions.

    Before this, dropping ``CASHBOOK_CLI`` from the tuple left every test in
    this file green: the one thing keeping a second group's codes inside the
    registry walks was an unguarded tuple entry.
    """

    def test_every_module_that_imports_the_envelope_is_walked(self) -> None:
        walked = set(EMITTING_CLI_MODULES)

        self.assertEqual(_modules_importing_the_envelope() | {ENVELOPE_CLI}, walked)

    def test_the_import_scan_found_something(self) -> None:
        """Positive control: an empty scan would satisfy the equality above as
        soon as the tuple held only the defining module."""
        self.assertIn(CASHBOOK_CLI, _modules_importing_the_envelope())


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

    def test_an_unregistered_code_is_reported_and_logged_rather_than_raising(self) -> None:
        """The error path must never crash — but a typo must not vanish either.

        The AST gate below catches an unregistered code in CI. This is what
        happens on the operator's machine if one ever slips past: the refusal
        still renders, and the log says the code was not registered.

        The logger is the one of the module where `_fail_with` is DEFINED, not
        the one where it is called or re-exported from — since #1689 that is
        `_cli_envelope`.
        """
        from alphalens_cli.commands.broker import _fail_with

        with self.assertLogs("alphalens_cli.commands._cli_envelope", level="WARNING") as logs:
            exit_exc = _fail_with("not_a_registered_code", "boom")

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertIn("not_a_registered_code", logs.output[0])

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
