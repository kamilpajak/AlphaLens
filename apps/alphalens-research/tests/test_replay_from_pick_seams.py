"""The source gates over ``scripts/replay_from_pick``: its seams, its refusals.

Both gates are here because both answer a question no behavioural test can:
whether every SITE of the module obeys a rule, rather than whether the paths a
test happens to drive do.

**The seams.** Every behavioural test of the fetch path injects a fake, and the
no-live-network guard makes the production default untestable by running it. So
a ``main()`` whose ``bar_fetch`` default was left ``None``, or pointed at a
private local copy of the fetcher, would pass every one of those tests and
still make no vendor call -- or the wrong one -- in the operator's hands.

**The refusals.** The command refuses with a ``code`` and a closed ``reason``
vocabulary, the shape ``broker arm`` publishes. Those were bare literals at
every call site and were written out again in two documents, so a reason added
at a site turned nothing red. ``_REFUSAL_REASONS`` is now the one registry, the
help text is the one published surface, and the walk below checks both
directions against the module's own refusal sites.

This repository's remedy for that shape is a source gate, the one
``tests/brokers/test_broker_cli_places_nothing.py`` established, in its own
words: a rule a caller declares is a rule a caller can opt out of, while "an
AST walk over every CLI module cannot be opted out of". The seam gate and the
refusal walk read the module's source and run nothing; the published-surface
half renders the command's own ``--help``, because the text argparse prints is
what a caller reads.
"""

from __future__ import annotations

import ast
import contextlib
import io
import pathlib
import re
import unittest
from collections.abc import Mapping

import scripts.replay_from_pick
from scripts.replay_from_pick import _REFUSAL_REASONS

# The three keyword-only seams a test injects and production defaults.
_SEAMS = ("actions_lookup", "bar_fetch", "now")

_BAR_WINDOW_MODULE = "alphalens_pipeline.feedback.bar_window"
_BAR_FETCH_DEFAULT = "default_bar_fetch"


def _module_ast() -> ast.Module:
    source = pathlib.Path(scripts.replay_from_pick.__file__).read_text(encoding="utf-8")
    return ast.parse(source)


def _main_defaults(tree: ast.Module) -> dict[str, ast.expr]:
    """``main``'s keyword-only arguments that carry a default, by name.

    An argument declared without a default is absent from the mapping, so a
    seam that became required reads as missing rather than as wired.
    """
    main = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    pairs = zip(main.args.kwonlyargs, main.args.kw_defaults, strict=True)
    return {arg.arg: default for arg, default in pairs if default is not None}


def _imported_names(tree: ast.Module, module: str) -> set[str]:
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == module
        for alias in node.names
    }


def _called_name(call: ast.Call) -> str | None:
    """The bare name of what ``call`` calls, attribute access included."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


class TheFetchSeamIsWiredToProductionTest(unittest.TestCase):
    """A seam defaulting to ``None`` is a production path that silently does
    nothing while every injected-fake test stays green."""

    def test_the_bar_fetch_default_is_the_canonical_polygon_seam(self) -> None:
        tree = _module_ast()

        default = _main_defaults(tree)["bar_fetch"]

        self.assertIsInstance(default, ast.Name)
        self.assertEqual(default.id, _BAR_FETCH_DEFAULT)
        self.assertIn(_BAR_FETCH_DEFAULT, _imported_names(tree, _BAR_WINDOW_MODULE))

    def test_the_actions_lookup_default_builds_the_cached_polygon_lookup(self) -> None:
        default = _main_defaults(_module_ast())["actions_lookup"]

        # The cache path is a run value, so the default is a factory; what the
        # gate is about is the composition inside it.
        built = default.body if isinstance(default, ast.Lambda) else default

        self.assertIsInstance(built, ast.Call)
        self.assertEqual(_called_name(built), "CachedCorporateActionsLookup")
        self.assertTrue(built.args, "the disk cache wraps nothing")
        self.assertIsInstance(built.args[0], ast.Call)
        self.assertEqual(_called_name(built.args[0]), "PolygonCorporateActionsLookup")

    def test_no_injected_seam_defaults_to_none(self) -> None:
        """The shape this gate exists to catch: a seam resolved inside the body
        from a ``None``, which every injected-fake test passes straight over."""
        defaults = _main_defaults(_module_ast())

        self.assertEqual(sorted(defaults), sorted(_SEAMS))
        for name, default in sorted(defaults.items()):
            with self.subTest(seam=name):
                self.assertFalse(
                    isinstance(default, ast.Constant) and default.value is None,
                    f"{name} defaults to None, so production would run without it",
                )


# ---------------------------------------------------------------------------
# The refusal vocabulary. See ``_REFUSAL_REASONS`` in the module under test.
# ---------------------------------------------------------------------------

# What builds a refusal: the renderer every refusal passes through, and the
# object the bar stage returns instead of printing.
_REFUSAL_FACTORIES = ("_fail", "BarRefusal")

# Each indirection that produces a ``reason`` the walk cannot read at the
# refusal site, and the code it produces one for. Three typed exceptions carry
# their reason to ``main``, the off-grid reasons are a family of module
# constants, and ``_action_reason`` picks one of two words out of the lookup's
# own detail. Without this table those reasons reach no gate at all; with it, a
# new one is a red test rather than a silent addition.
#
# ``TheWalkCanRefuteTheModuleTest`` pins that every entry still produces
# something, so a renamed source cannot quietly empty its half of the gate.
#
# EVERY NEW INDIRECTION FEEDING A CODE NEEDS A ROW HERE. That is the gate's one
# hole and it is declared rather than hidden: a new helper returning a reason
# for a code this table ALREADY names has its literals read by nothing, and the
# author gets the runtime warning instead of a red test. Registering the new
# reason is what turns ``test_every_registered_reason_is_one_a_site_can_publish``
# red until the row exists.
_INDIRECT_REASON_SOURCES = {
    "EntryTrailUnknownError": "entry_trail_unknown",
    "ExitTimeUnknownError": "exit_time_unknown",
    "OFF_GRID_": "bars_off_grid",
    "ReportUnreadableError": "report_unreadable",
    "_action_reason": "bars_corporate_action",
}

# Where the published code list starts inside the help text's EXIT CODES block.
_CODE_LIST_MARKER = "The codes:"


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"``, so a reason named by a constant reads
    the same as one spelled at the site."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value
    return constants


def _text(node: ast.expr | None, constants: Mapping[str, str]) -> str | None:
    """``node`` as the string it certainly is, or ``None`` when it is not one.

    A bare ``Name`` resolves through the module's own constants: a reason the
    code spells once and references by name is as readable as a literal, and
    refusing to read it would push two real reasons into the unvouched pile.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _branches(node: ast.expr | None) -> tuple[ast.expr | None, ...]:
    """The expressions ``node`` can evaluate to, flattening a conditional.

    Two refusals read their reason off a typed exception OR fall back to a
    constant for an arrival that is not that exception. Taking the whole
    ``IfExp`` as one unreadable expression would lose the half that is a stated
    literal.
    """
    if isinstance(node, ast.IfExp):
        return _branches(node.body) + _branches(node.orelse)
    return (node,)


def _owners(tree: ast.Module) -> dict[ast.AST, ast.FunctionDef]:
    """Each node's innermost enclosing function.

    ``ast.walk`` is breadth-first from the module, so a nested function is
    reached after the one holding it and its own assignment wins.
    """
    owners: dict[ast.AST, ast.FunctionDef] = {}
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            owners[node] = function
    return owners


def _argument(call: ast.Call, name: str, index: int) -> ast.expr | None:
    """One argument of ``call``, by keyword if it is passed that way."""
    found = call.args[index] if len(call.args) > index else None
    for keyword in call.keywords:
        if keyword.arg == name:
            found = keyword.value
    return found


def _refusal_calls(tree: ast.Module) -> list[tuple[ast.Call, ast.expr | None, ast.expr | None]]:
    """Every refusal site, with the expressions it states its code and reason as."""
    return [
        (node, _argument(node, "code", 0), _argument(node, "reason", 1))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node) in _REFUSAL_FACTORIES
    ]


def _forwarded_reasons(
    tree: ast.Module, function: ast.FunctionDef, parameter: str, constants: Mapping[str, str]
) -> set[str]:
    """The literals every caller of ``function`` passes as ``parameter``.

    A refusal FACTORY takes its reason as an argument -- ``_unreadable`` builds
    five of them -- so the call site that states the reason is the factory's
    caller and not the ``BarRefusal`` construction itself.
    """
    names = [argument.arg for argument in function.args.args]
    if parameter not in names:
        return set()
    index = names.index(parameter)
    reasons = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _called_name(node) == function.name):
            continue
        reason = _text(_argument(node, parameter, index), constants)
        if reason is not None:
            reasons.add(reason)
    return reasons


def _indirect_reasons(tree: ast.Module, key: str, constants: Mapping[str, str]) -> set[str]:
    """Every reason the indirection ``key`` can produce.

    A trailing underscore names a family of module constants; anything else is
    read both as a callable whose first argument is the reason (a typed
    exception) and as a function that returns one.
    """
    if key.endswith("_"):
        return {value for name, value in constants.items() if name.startswith(key)}
    reasons = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _called_name(node) == key and node.args:
            reason = _text(node.args[0], constants)
            if reason is not None:
                reasons.add(reason)
        if isinstance(node, ast.FunctionDef) and node.name == key:
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Return):
                    continue
                reasons.update(
                    reason
                    for reason in (_text(leaf, constants) for leaf in _branches(inner.value))
                    if reason is not None
                )
    return reasons


def _pass_through(code: ast.expr | None, reason: ast.expr | None) -> bool:
    """Whether a site forwards one refusal object's own code and reason.

    ``main`` re-renders a ``BarRefusal`` the bar stage returned. That site can
    state no literal and needs none: the pair was already vouched for where the
    object was built. What it must not be is two attributes of two different
    objects, which is how a code comes to be printed beside another refusal's
    reason.
    """
    if not (isinstance(code, ast.Attribute) and isinstance(reason, ast.Attribute)):
        return False
    if (code.attr, reason.attr) != ("code", "reason"):
        return False
    return (
        isinstance(code.value, ast.Name)
        and isinstance(reason.value, ast.Name)
        and code.value.id == reason.value.id
    )


def refusal_vocabulary(tree: ast.Module) -> tuple[set[tuple[str, str]], list[tuple[str, str]]]:
    """Every ``(code, reason)`` the module can publish, and the sites it cannot read.

    A hole is ``(code, kind)``: ``indirect`` when the code is stated and the
    reason arrives from somewhere this walk cannot follow, ``pass_through``
    when a whole refusal object is re-rendered, and ``unvouched`` when neither
    account holds -- the shape no gate covers.
    """
    constants = _module_constants(tree)
    owners = _owners(tree)
    pairs: set[tuple[str, str]] = set()
    holes: list[tuple[str, str]] = []
    for call, code_node, reason_node in _refusal_calls(tree):
        code = _text(code_node, constants)
        if code is None:
            holes.append(
                ("", "pass_through" if _pass_through(code_node, reason_node) else "unvouched")
            )
            continue
        for leaf in _branches(reason_node):
            reason = _text(leaf, constants)
            if reason is not None:
                pairs.add((code, reason))
                continue
            owner = owners.get(call)
            forwarded = (
                _forwarded_reasons(tree, owner, leaf.id, constants)
                if isinstance(leaf, ast.Name) and owner is not None
                else set()
            )
            if forwarded:
                pairs.update((code, one) for one in forwarded)
            else:
                holes.append((code, "indirect"))
    for key, code in _INDIRECT_REASON_SOURCES.items():
        pairs.update((code, reason) for reason in _indirect_reasons(tree, key, constants))
    return pairs, holes


class TheRefusalCodesComeFromOneRegistryTest(unittest.TestCase):
    """The codes and reasons were bare literals at every call site, so a reason
    added at one of them was published nowhere and contradicted nothing."""

    def test_every_refusal_site_names_a_registered_code(self) -> None:
        pairs, _ = refusal_vocabulary(_module_ast())

        for code, reason in sorted(pairs):
            with self.subTest(code=code, reason=reason):
                self.assertIn(code, _REFUSAL_REASONS)

    def test_every_reason_a_site_states_is_in_that_codes_closed_set(self) -> None:
        """A reason is only closed if something closes it. Added at a call site
        under an already registered code, a new one changed no document and
        broke no test, so a client's branch on `reason` silently lost a case."""
        pairs, _ = refusal_vocabulary(_module_ast())

        for code, reason in sorted(pairs):
            with self.subTest(code=code, reason=reason):
                self.assertIn(reason, _REFUSAL_REASONS.get(code, frozenset()))


class TheWalkCanRefuteTheModuleTest(unittest.TestCase):
    """A gate that cannot produce the disproving observation has tested
    nothing. These are the controls on the walk itself: that it reads a pair it
    is shown, that it refuses to vouch for one it cannot read, and that every
    site it cannot read is accounted for by a declared source rather than
    passing silently.
    """

    def test_the_walk_reads_an_invented_pair_out_of_a_snippet(self) -> None:
        pairs, holes = refusal_vocabulary(ast.parse("_fail('invented', 'bogus', 'boom', 1)"))

        self.assertEqual(pairs, {("invented", "bogus")})
        self.assertEqual(holes, [])

    def test_the_walk_refuses_to_vouch_for_two_different_objects(self) -> None:
        """A pass-through is only safe while both halves come off ONE refusal;
        a code read from one object beside another's reason publishes a pair
        neither of them ever built."""
        pairs, holes = refusal_vocabulary(ast.parse("_fail(one.code, other.reason, 'boom', 1)"))

        self.assertEqual(pairs, set())
        self.assertEqual(holes, [("", "unvouched")])

    def test_no_refusal_site_in_the_module_is_unvouched_for(self) -> None:
        _, holes = refusal_vocabulary(_module_ast())

        self.assertEqual([kind for _, kind in holes if kind == "unvouched"], [])

    def test_every_site_with_an_unreadable_reason_names_a_declared_source(self) -> None:
        """The one way a reason escapes this gate: a code whose reason arrives
        from an indirection no table mentions."""
        _, holes = refusal_vocabulary(_module_ast())

        for code, kind in holes:
            if kind == "indirect":
                with self.subTest(code=code):
                    self.assertIn(code, set(_INDIRECT_REASON_SOURCES.values()))

    def test_every_declared_source_still_produces_a_reason(self) -> None:
        """Anti-rot: a renamed exception or constant family yields nothing, and
        an empty source silently drops its half of the gate while every
        assertion above stays green."""
        tree = _module_ast()
        constants = _module_constants(tree)

        for key in sorted(_INDIRECT_REASON_SOURCES):
            with self.subTest(source=key):
                self.assertTrue(_indirect_reasons(tree, key, constants))

    def test_every_registered_reason_is_one_a_site_can_publish(self) -> None:
        """The stale-row direction. A reason left in the registry after its
        site changed is published in the help text's own code and documented
        nowhere as gone, so a client keeps a branch for a case that cannot
        arrive."""
        pairs, _ = refusal_vocabulary(_module_ast())

        self.assertEqual(
            pairs,
            {(code, reason) for code, reasons in _REFUSAL_REASONS.items() for reason in reasons},
        )


def _help_text() -> str:
    """The command's own ``--help``, which is the surface a caller reads.

    Rendered rather than read off ``_USAGE``, because what is published is what
    argparse prints: a block the formatter dropped or rewrapped would still
    read as published if the constant were inspected directly.
    """
    out, err = io.StringIO(), io.StringIO()
    with (
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
        contextlib.suppress(SystemExit),
    ):
        scripts.replay_from_pick.main(["--help"])
    return out.getvalue()


def _published_codes(help_text: str) -> set[str]:
    """Every code the help text's EXIT CODES block lists.

    Only the region after ``_CODE_LIST_MARKER`` is read, so the prose above it
    -- which names no code -- cannot pass a code off as published, and an
    absent marker reads as "nothing is published" rather than as a crash.
    """
    block = help_text.split("EXIT CODES", 1)[1].split("EXAMPLES", 1)[0]
    listed = block.split(_CODE_LIST_MARKER, 1)[1] if _CODE_LIST_MARKER in block else ""
    return set(re.findall(r"[a-z][a-z_]+", listed))


class TheRegistryIsPublishedInTheHelpTextTest(unittest.TestCase):
    """The codes were published in two places that nothing kept in step: this
    command's `--help`, and section 11 of the bars design memo.

    The help text is the surface pinned here. The memo is a DATED design record
    marked LOCKED, so pinning live code to it would make a historical document
    a contract and force an edit to it on the next change to the code; `--help`
    is what a caller actually reads, and the CLI conventions already make it a
    published surface with fixed headings. What it carries is the CODES, under
    EXIT CODES: the reasons are twenty-nine words across fifteen modes and
    would make the block unreadable, so they stay in the registry, and this
    gate checks exactly what the text claims and no more.
    """

    def test_the_help_text_lists_every_registered_code(self) -> None:
        published = _published_codes(_help_text())

        for code in sorted(_REFUSAL_REASONS):
            with self.subTest(code=code):
                self.assertIn(code, published)

    def test_the_help_text_lists_no_code_the_registry_does_not_define(self) -> None:
        """The other direction: a code removed from the module stays in the
        help text as a branch a client can still be written against."""
        for code in sorted(_published_codes(_help_text())):
            with self.subTest(code=code):
                self.assertIn(code, _REFUSAL_REASONS)


if __name__ == "__main__":
    unittest.main()
