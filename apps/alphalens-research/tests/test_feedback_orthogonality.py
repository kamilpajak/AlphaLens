"""Enforcement: the human-CLICK columns of the feedback ledger never feed the
scoring / weighting / sizing / execution-telemetry path.

The invariant
-------------
The AlphaLens feedback loop rests on ONE load-bearing rule: the columns a human
authors on a brief candidate (``action``, ``dismiss_category``, ``dismiss_reason``,
``dismiss_note``, ``confidence_subjective``) are an ORTHOGONAL signal. They answer
"does the human add edge over the scorer?" and must be evaluated SEPARATELY — they
must never be confirmed by the very weights / sizing / execution mode they would
influence. Letting a click column flow back into the scorer is a degenerate
feedback loop (model autophagy): the model learns to like what it already
surfaced, and the orthogonal-edge question becomes unanswerable.

Today the code respects this. The chokepoint feed into all scoring / telemetry,
:meth:`alphalens_feedback.store.FeedbackStore.iter_matured_decisions`, projects
ONLY job-set columns (regime, fill_status, shadow_return, realized_return) — no
click column. The thematic scorer never touches the ledger at all. This test
makes that a REGRESSION TRIPWIRE so a future re-weighting PR cannot silently wire
a click column into a scoring path.

Detection model
---------------
Two classes of click column with different exposure to false positives:

* The four UNAMBIGUOUS columns (``dismiss_category`` / ``dismiss_reason`` /
  ``dismiss_note`` / ``confidence_subjective``) have no legitimate non-ledger use
  anywhere in the codebase. A READ of any of them in ANY scanned, non-allowlisted
  file is a violation — no consumer gate needed. This closes the split-file leak:
  a pure scorer that takes ``Decision`` objects from another module (so it never
  imports the store / names ``FeedbackStore``) and reads ``d.dismiss_reason`` is
  caught even though it is not itself a "feedback-consumer".
* The AMBIGUOUS ``action`` token collides with argparse / typer namespaces, Alpaca
  order ``side``/``action``, the word "actionable", etc. A read of ``action`` only
  counts as a violation inside a file that actually touches the ledger (the
  :func:`is_feedback_consumer` gate), so ``order.action`` / ``args.action`` in an
  unrelated module do not false-positive.

Accepted static-analysis blind spots (documented so the coverage claim stays
honest — do NOT silently expand the detector to chase these without a fixture):

  (a) Dynamic SQL where the column name is interpolated, e.g.
      ``f"SELECT {col} FROM decisions"`` with ``col`` bound at runtime — the
      literal never appears as a Constant, so the SQL-context branch cannot see it.
  (b) The AMBIGUOUS ``action`` token read via attribute / subscript in a
      NON-consumer file (a split-file leak for ``action`` ONLY). The four
      unambiguous columns are now covered everywhere; ``action`` still relies on
      the consumer gate to keep false positives out of unrelated modules.
  (c) The research tier (``apps/alphalens-research/alphalens_research``) is
      intentionally OUT of scope — it is the lab, not the live scoring path per
      ADR 0011. Only the live pipeline / feedback / CLI / Django roots are scanned.

Escape hatch: a NEW legitimate operator / API reader (schema CRUD, a CLI report,
a Django POST handler) is added to :data:`CLICK_READER_ALLOWLIST` — never by
weakening the detector.

Design memo: ``docs/research/feedback_ledger_counterfactual_design_2026_06_02.md``
(orthogonality / §2.4).

House style mirrors ``test_module_dependencies.py`` (AST walk) and
``test_no_raw_sec_http.py`` (forbidden-pattern scan + path allowlist). The
POSITIVE / NEGATIVE controls below are MANDATORY (per CLAUDE.md): they prove the
detector is non-vacuous and does not false-positive on the protected path, so the
test cannot rot into always-passing.
"""

from __future__ import annotations

import ast
import inspect
import os
import re
import tempfile
import textwrap
import unittest
from pathlib import Path

from alphalens_feedback.store import FeedbackStore

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]

# The human-authored columns. Forbidden anywhere on the scoring / telemetry path.
# Mirrors the ``decisions`` table + ``Decision`` dataclass in
# ``alphalens_feedback.store``; the ``action`` CHECK enum is
# interested/watching/dismissed/paper_traded/live_traded.
CLICK_COLUMNS = (
    "action",
    "dismiss_category",
    "dismiss_reason",
    "dismiss_note",
    "confidence_subjective",
)

# The four columns with NO legitimate non-ledger use anywhere. A read of any of
# them in a non-allowlisted scanned file is a violation regardless of whether the
# file is a "feedback-consumer" — this is what closes the split-file leak.
UNAMBIGUOUS_CLICK_COLUMNS = frozenset(
    {
        "dismiss_category",
        "dismiss_reason",
        "dismiss_note",
        "confidence_subjective",
    }
)

# ``action`` collides with argparse / typer / Alpaca order side / "actionable", so
# a read of it only counts inside a file that touches the ledger (consumer gate).
AMBIGUOUS_CLICK_COLUMNS = frozenset({"action"})

assert set(CLICK_COLUMNS) == UNAMBIGUOUS_CLICK_COLUMNS | AMBIGUOUS_CLICK_COLUMNS

# Job-set (NOT click) columns the chokepoint projection legitimately exposes to
# scoring / telemetry. ``regime`` is the COALESCE alias of
# ``market_regime_at_entry`` used inside ``iter_matured_decisions``.
SAFE_PROJECTION_COLUMNS = frozenset(
    {
        "brief_date",
        "ticker",
        "market_regime_at_entry",
        "regime",
        "fill_status",
        "shadow_return",
        "realized_return",
    }
)

# The ONLY files allowed to read a click column. Every one is a legitimate
# operator / API surface (schema CRUD, the ``report`` CLI histogram, the Django
# POST handler + DRF serializer), NOT a scoring path.
CLICK_READER_ALLOWLIST = frozenset(
    {
        "apps/alphalens-feedback/alphalens_feedback/store.py",
        "apps/alphalens-pipeline/alphalens_cli/commands/feedback.py",
        "apps/alphalens-django/feedback/views.py",
        "apps/alphalens-django/feedback/serializers.py",
    }
)

# Production / operator roots to scan. ``tests`` + ``migrations`` segments and the
# test file itself are excluded (fixtures + ORM column DDL legitimately name the
# columns and are not a scoring path).
SCAN_ROOTS = (
    WORKSPACE_ROOT / "apps" / "alphalens-feedback" / "alphalens_feedback",
    WORKSPACE_ROOT / "apps" / "alphalens-pipeline" / "alphalens_pipeline",
    WORKSPACE_ROOT / "apps" / "alphalens-pipeline" / "alphalens_cli",
    WORKSPACE_ROOT / "apps" / "alphalens-django",
)

_EXCLUDED_SEGMENTS = frozenset({"tests", "migrations"})

# Resolved paths of the feedback store (positive control) + the two protected
# scoring-side modules (negative control). Computed once at module load.
_STORE_PY = WORKSPACE_ROOT / "apps/alphalens-feedback/alphalens_feedback/store.py"
_EXECUTION_TELEMETRY_PY = (
    WORKSPACE_ROOT / "apps/alphalens-pipeline/alphalens_pipeline/feedback/execution_telemetry.py"
)
_EXECUTION_MODES_PY = (
    WORKSPACE_ROOT / "apps/alphalens-pipeline/alphalens_pipeline/feedback/execution_modes.py"
)


def _iter_scan_files() -> list[Path]:
    """Every ``*.py`` under SCAN_ROOTS, excluding tests/migrations + this file."""
    self_path = Path(__file__).resolve()
    out: list[Path] = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            if py.resolve() == self_path:
                continue
            if _EXCLUDED_SEGMENTS & set(py.parts):
                continue
            out.append(py)
    return out


def _rel(path: Path) -> str:
    return path.resolve().relative_to(WORKSPACE_ROOT).as_posix()


def _parse_source(src: str) -> ast.Module:
    return ast.parse(src)


def is_feedback_consumer(path: Path) -> bool:
    """True iff the file plausibly consumes the feedback ledger.

    Conjunction gate: either it imports ``alphalens_feedback`` (any submodule)
    OR its text names ``FeedbackStore`` / ``iter_matured_decisions`` /
    ``alphalens_feedback``. This is what suppresses false positives on the
    ambiguous ``action`` token (argparse / typer / Alpaca order side / the word
    "actionable"): a stray ``action`` only counts inside a file that actually
    touches the ledger.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    if "alphalens_feedback" in text or "FeedbackStore" in text or "iter_matured_decisions" in text:
        return True
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name.split(".")[0] == "alphalens_feedback" for alias in node.names
        ):
            return True
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".")[0] == "alphalens_feedback"
        ):
            return True
    return False


_SQL_KEYWORD_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|WHERE|VALUES|SET|FROM)\b", re.IGNORECASE
)


def _docstring_constant_ids(tree: ast.Module) -> set[int]:
    """Object-ids of every module / class / function docstring Constant node.

    Docstrings legitimately NAME the click columns in prose (e.g.
    "NEVER reads the ``action`` column"). They are not a READ of the column, so
    they must not trip the detector — otherwise the orthogonality docstrings in
    ``execution_telemetry`` would false-positive. We exclude the docstring node
    of every module / class / def whose first statement is a bare string Expr.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def click_columns_read_in_source(src: str) -> set[str]:
    """AST detector over SOURCE TEXT: which CLICK columns does it READ?

    Returns the SET of click columns the module READS (vs merely names in prose).
    Operating on source text (rather than a path) lets the fixtures assert on
    inline snippets without temp files; :func:`click_columns_read` is the
    path-reading wrapper.

    Fires for a column when any of these forms references it:

    * ``ast.Attribute`` whose ``.attr`` is the column — the dataclass-field read
      ``d.action`` / ``decision.dismiss_category``.
    * ``ast.Subscript`` with a string ``ast.Constant`` slice equal to the column —
      the dict / Row key read ``row["action"]`` / ``data["dismiss_reason"]``.
    * ``x.get("action")`` — an ``ast.Call`` whose ``func`` is an ``ast.Attribute``
      with attr ``"get"`` and whose FIRST positional arg is a string Constant equal
      to the column. (This is the ``.get(...)`` form the old docstring CLAIMED was
      handled but had no branch for.)
    * ``getattr(d, "action")`` — an ``ast.Call`` whose ``func`` is ``ast.Name`` with
      id ``"getattr"`` and whose SECOND positional arg is a string Constant equal to
      the column.
    * a string Constant that names the column (whole-word) AND sits in a SQL clause
      (contains SELECT / WHERE / INSERT / ...) — catches
      ``"SELECT action, dismiss_reason FROM ..."``. Docstrings / plain prose are
      excluded so the orthogonality docstrings ("NEVER reads the ``action``
      column") are not mistaken for an actual read.

    What it deliberately does NOT fire on: a click column named only in PROSE (a
    docstring or a plain free-text string), an assignment TARGET Name (e.g. the DRF
    ``dismiss_category = serializers.CharField()`` declaration — a Name store, not a
    read), or a column whose name is interpolated into dynamic SQL (blind spot (a)
    in the module docstring).
    """
    tree = _parse_source(src)
    docstring_ids = _docstring_constant_ids(tree)
    found: set[str] = set()

    word_res = {col: re.compile(rf"\b{re.escape(col)}\b") for col in CLICK_COLUMNS}

    def _sql_names(value: str) -> set[str]:
        return {col for col, rx in word_res.items() if rx.search(value)}

    for node in ast.walk(tree):
        # Dataclass-field read: d.action / decision.dismiss_category.
        if isinstance(node, ast.Attribute) and node.attr in CLICK_COLUMNS:
            found.add(node.attr)
            continue
        # Subscript string key: row["action"], data["dismiss_reason"].
        if isinstance(node, ast.Subscript):
            key = node.slice
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value in CLICK_COLUMNS
            ):
                found.add(key.value)
            continue
        if isinstance(node, ast.Call):
            func = node.func
            # x.get("action") — string-literal field access via a .get() call.
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value in CLICK_COLUMNS
            ):
                found.add(node.args[0].value)
                continue
            # getattr(d, "action") — string-literal attribute access.
            if (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value in CLICK_COLUMNS
            ):
                found.add(node.args[1].value)
                continue
        # String constant in SQL context — but never a docstring / prose string.
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_ids
            and _SQL_KEYWORD_RE.search(node.value)
        ):
            found |= _sql_names(node.value)
    return found


def click_columns_read(path: Path) -> set[str]:
    """Path wrapper around :func:`click_columns_read_in_source`."""
    return click_columns_read_in_source(path.read_text(encoding="utf-8", errors="replace"))


def reads_click_data(path: Path) -> bool:
    """Thin bool wrapper: does the file read ANY click column? (Legacy call-site.)"""
    return bool(click_columns_read(path))


def _strip_function_docstring(source: str) -> str:
    """Return ``source`` with the function's own docstring removed.

    ``inspect.getsource`` of a method includes its docstring. If a future author
    documents the orthogonality invariant in the method docstring (e.g. "never
    selects the action column"), a naive click-column regex over the full source
    would false-FAIL. We parse the single function and drop its leading docstring
    Expr so only executable lines are scanned.
    """
    try:
        # A method source is indented; dedent so the def parses standalone.
        normalized = textwrap.dedent(source)
        tree = ast.parse(normalized)
    except (SyntaxError, IndentationError):
        return source
    func = next(
        (n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
        None,
    )
    doc = ast.get_docstring(func, clean=False) if func is not None else None
    if not doc:
        return source
    # Remove the exact docstring text from the original source.
    return source.replace(doc, "", 1)


class TestChokepointIsClickFree(unittest.TestCase):
    """TEST 1 — ``iter_matured_decisions`` is the single click-free feed."""

    def test_iter_matured_decisions_selects_no_click_column(self):
        raw = inspect.getsource(FeedbackStore.iter_matured_decisions)
        # Strip the method's own docstring so a future author documenting the
        # invariant in prose ("never selects the action column") cannot false-FAIL
        # this regex. Scan only executable lines.
        source = _strip_function_docstring(raw)
        for col in CLICK_COLUMNS:
            self.assertNotRegex(
                source,
                rf"\b{re.escape(col)}\b",
                f"iter_matured_decisions references the click column {col!r}; the "
                "scoring / telemetry chokepoint must project only job-set columns "
                f"({sorted(SAFE_PROJECTION_COLUMNS)}).",
            )

    def test_iter_matured_decisions_select_subset_of_safe_columns(self):
        source = inspect.getsource(FeedbackStore.iter_matured_decisions)
        # The SQL is built from adjacent string literals, so the raw source
        # carries embedded quote characters + newlines between fragments. Drop
        # the quote chars and collapse whitespace so the SELECT ... FROM slice
        # is a single clean clause regardless of how the literals are split.
        sql = re.sub(r"\s+", " ", source.replace('"', " ").replace("'", " "))
        match = re.search(r"\bSELECT\b(.*?)\bFROM\b", sql, re.IGNORECASE)
        # Guard the match: if the projection cannot be located, fail with a clear
        # message rather than raising AttributeError on ``None.group(...)``.
        self.assertIsNotNone(
            match,
            "could not locate the SELECT ... FROM projection in "
            "iter_matured_decisions source — the parser must find it.",
        )
        assert match is not None  # narrows type for the line below
        select_body = match.group(1)
        # Collect every bare identifier token in the projection, then keep only
        # those that name a decisions column we know about (drops SQL keywords
        # like COALESCE / AS and the 'unknown' literal).
        tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", select_body))
        known_columns = SAFE_PROJECTION_COLUMNS | set(CLICK_COLUMNS)
        selected_columns = tokens & known_columns
        self.assertTrue(selected_columns, "no recognised decisions column found in projection")
        self.assertTrue(
            selected_columns <= SAFE_PROJECTION_COLUMNS,
            f"chokepoint projects non-safe columns {sorted(selected_columns - SAFE_PROJECTION_COLUMNS)}; "
            f"allowed set is {sorted(SAFE_PROJECTION_COLUMNS)}.",
        )

    def test_positive_control_click_columns_are_real_in_store(self):
        # Non-vacuous guard: each click column must still exist in the store
        # source (schema DDL + INSERT). If someone renames ``action``, this
        # fails and forces the constant set to be updated rather than the test
        # silently passing on a stale name.
        store_source = _STORE_PY.read_text(encoding="utf-8")
        for col in CLICK_COLUMNS:
            self.assertRegex(
                store_source,
                rf"\b{re.escape(col)}\b",
                f"click column {col!r} no longer present in store.py — update "
                "CLICK_COLUMNS to match the current schema.",
            )


def _is_violation(path: Path) -> bool:
    """The tree-scan violation rule for a non-allowlisted scanned file.

    A file violates orthogonality when it reads:
      * ANY of the four UNAMBIGUOUS columns (no consumer gate — these names have
        no legitimate non-ledger use), OR
      * the AMBIGUOUS ``action`` token AND it is a feedback-consumer (the gate
        keeps ``order.action`` / ``args.action`` in unrelated modules from
        false-positiving).
    """
    cols = click_columns_read(path)
    if cols & UNAMBIGUOUS_CLICK_COLUMNS:
        return True
    # The ambiguous ``action`` token only counts inside a feedback-consumer.
    return bool(cols & AMBIGUOUS_CLICK_COLUMNS) and is_feedback_consumer(path)


class TestNoScoringSideClickReader(unittest.TestCase):
    """TEST 2 — no scoring-side module reads a click column (forward tripwire)."""

    def test_no_unallowlisted_click_reader(self):
        offenders: list[str] = []
        for py in _iter_scan_files():
            rel = _rel(py)
            if rel in CLICK_READER_ALLOWLIST:
                continue
            if _is_violation(py):
                offenders.append(rel)
        offenders.sort()
        if offenders:
            self.fail(
                "Scoring-side module(s) read a human-CLICK feedback column "
                f"({sorted(CLICK_COLUMNS)}). Click data must stay orthogonal to "
                "the scorer / weighting / sizing / execution path — it cannot be "
                "confirmed by the weights it would influence (degenerate feedback "
                "loop). Offenders:\n  "
                + "\n  ".join(offenders)
                + "\nAdd to CLICK_READER_ALLOWLIST only if the file is a legitimate "
                "operator / API surface, NOT a scoring path."
            )

    def test_positive_control_detector_fires_on_known_readers(self):
        # The detector must flag the real click readers — otherwise the
        # tripwire above is vacuous (an always-empty offender set). Every
        # allowlisted file that would violate the rule must be detected, and at
        # minimum the store + the feedback CLI must be among them. Removing the
        # allowlist would therefore surface a NON-EMPTY set.
        flagged_without_allowlist = {_rel(py) for py in _iter_scan_files() if _is_violation(py)}

        self.assertTrue(
            flagged_without_allowlist,
            "detector flagged nothing even without the allowlist — it is vacuous; "
            "the orthogonality tripwire would never catch a regression.",
        )
        # No surprises: every flagged file must be on the allowlist. A flagged
        # file NOT on the allowlist would be a real orthogonality break caught by
        # ``test_no_unallowlisted_click_reader``.
        self.assertTrue(
            flagged_without_allowlist <= set(CLICK_READER_ALLOWLIST),
            "a file reads a click column under the violation rule but is not on "
            f"the allowlist: {sorted(flagged_without_allowlist - set(CLICK_READER_ALLOWLIST))}.",
        )
        # Non-vacuous: the canonical readers (store schema CRUD, the feedback
        # `report` CLI, the Django POST handler) MUST be detected. If any drops
        # out, the detector regressed and the tripwire is hollow.
        expected_canonical = {
            "apps/alphalens-feedback/alphalens_feedback/store.py",
            "apps/alphalens-pipeline/alphalens_cli/commands/feedback.py",
            "apps/alphalens-django/feedback/views.py",
        }
        self.assertTrue(
            expected_canonical <= flagged_without_allowlist,
            f"detector failed to flag a canonical click reader: "
            f"{sorted(expected_canonical - flagged_without_allowlist)}.",
        )

    def test_positive_control_store_is_consumer_and_reader(self):
        self.assertTrue(is_feedback_consumer(_STORE_PY))
        self.assertTrue(reads_click_data(_STORE_PY))

    def test_negative_control_execution_telemetry_not_flagged(self):
        # execution_telemetry IS a feedback-consumer (it imports FeedbackStore via
        # execution_gauges_for_ledger) yet reads NO click column — consuming the
        # store is fine, reading a CLICK column is the violation. This pins the
        # exact desired behaviour.
        self.assertTrue(
            is_feedback_consumer(_EXECUTION_TELEMETRY_PY),
            "execution_telemetry should register as a feedback-consumer (it opens "
            "the store) — the negative control is only meaningful if it does.",
        )
        self.assertFalse(
            reads_click_data(_EXECUTION_TELEMETRY_PY),
            "execution_telemetry must NOT be flagged as a click reader — it reads "
            "only (regime, fill_status, shadow_return, realized_return).",
        )

    def test_negative_control_execution_modes_not_flagged(self):
        self.assertFalse(
            reads_click_data(_EXECUTION_MODES_PY),
            "execution_modes must NOT be flagged as a click reader — it consumes "
            "only the click-free projection rows.",
        )


class TestDetectorBranchFixtures(unittest.TestCase):
    """Inline-source fixtures so the new detector branches cannot silently rot."""

    # --- positive: each new read form is detected ---------------------------

    def test_detects_dict_get_call_action(self):
        cols = click_columns_read_in_source('x = row.get("action")')
        self.assertIn("action", cols)

    def test_detects_getattr_call_dismiss_reason(self):
        cols = click_columns_read_in_source('y = getattr(d, "dismiss_reason")')
        self.assertIn("dismiss_reason", cols)

    def test_detects_attribute_confidence_subjective(self):
        cols = click_columns_read_in_source("score = d.confidence_subjective")
        self.assertIn("confidence_subjective", cols)

    def test_detects_subscript_dismiss_note(self):
        cols = click_columns_read_in_source('note = row["dismiss_note"]')
        self.assertIn("dismiss_note", cols)

    def test_detects_sql_constant_multiple_columns(self):
        cols = click_columns_read_in_source('q = "SELECT action, dismiss_category FROM decisions"')
        self.assertEqual({"action", "dismiss_category"}, cols)

    # --- split-file leak: unambiguous-anywhere rule -------------------------

    def test_split_file_unambiguous_read_is_violation_without_consumer(self):
        # A pure scorer that takes Decision objects from another module and reads
        # an UNAMBIGUOUS column. It is NOT a feedback-consumer (no import / no
        # FeedbackStore reference), yet the read must still be a violation.
        snippet = "def reweight(decisions):\n    return [d.dismiss_reason for d in decisions]"
        cols = click_columns_read_in_source(snippet)
        self.assertIn("dismiss_reason", cols)

        path = _write_tmp_module(self, snippet)
        self.assertFalse(
            is_feedback_consumer(path),
            "the split-file snippet must NOT register as a feedback-consumer — "
            "otherwise it would not exercise the unambiguous-anywhere branch.",
        )
        self.assertTrue(
            _is_violation(path),
            "an unambiguous click column read in a non-consumer file must still be "
            "a violation (split-file leak closed).",
        )

    # --- negative: no false positives on the action+consumer gate -----------

    def test_action_attribute_in_non_consumer_is_not_violation(self):
        # order.action / args.action ALONE, in a file that does NOT touch the
        # ledger, must NOT be a violation (the ambiguous token needs the consumer
        # gate). The detector still SEES the read, but _is_violation gates it.
        snippet = "def submit(order):\n    return order.action\n\nargs_action = args.action\n"
        cols = click_columns_read_in_source(snippet)
        self.assertEqual({"action"}, cols, "the read is seen but should be the only one")

        path = _write_tmp_module(self, snippet)
        self.assertFalse(is_feedback_consumer(path))
        self.assertFalse(
            _is_violation(path),
            "a bare action attribute read in a non-consumer file must NOT be a "
            "violation — the consumer gate suppresses the ambiguous token.",
        )

    def test_action_attribute_in_consumer_is_violation(self):
        # The same action read, but now the file touches the ledger → violation.
        snippet = (
            "from alphalens_feedback.store import FeedbackStore\n\n"
            "def submit(order):\n    return order.action\n"
        )
        path = _write_tmp_module(self, snippet)
        self.assertTrue(is_feedback_consumer(path))
        self.assertTrue(
            _is_violation(path),
            "an action read inside a feedback-consumer file must be a violation.",
        )

    def test_prose_docstring_naming_columns_is_not_a_read(self):
        # A module docstring naming action / dismiss_reason in prose is NOT a read.
        snippet = '"""This module never reads the action or dismiss_reason column."""\nVALUE = 1\n'
        cols = click_columns_read_in_source(snippet)
        self.assertEqual(set(), cols, "prose docstring naming columns must not be a read.")


def _write_tmp_module(test: unittest.TestCase, source: str) -> Path:
    """Write ``source`` to a temp .py file that is cleaned up after the test.

    Used by the consumer-gate fixtures, which need ``is_feedback_consumer`` /
    ``_is_violation`` to read from a real path. The source-only branches use
    :func:`click_columns_read_in_source` directly and need no temp file.
    """
    fd, name = tempfile.mkstemp(suffix=".py")
    path = Path(name)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(source)
    test.addCleanup(path.unlink)
    return path


if __name__ == "__main__":
    unittest.main()
