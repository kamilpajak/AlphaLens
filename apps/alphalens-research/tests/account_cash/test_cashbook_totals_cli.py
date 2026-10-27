"""``alphalens cashbook totals``: the CLI half of the cash book's answer (#1689).

The numbers themselves are tested in ``tests/account_cash/test_report.py``,
against literals re-derived from the capture. What is tested HERE is only what
a command adds: the one JSON contract (#1379) on this group's second command,
the failure contract (#1389) on one new CLI code, the prose/JSON parity the
repo CLI doctrine requires, and the five properties memo section 6 calls
load-bearing.

**The command computes no money.** Every number it publishes comes off the
record :func:`~alphalens_pipeline.account_cash.report.build_totals` returns,
and two tests below are what make that refutable: one compares the whole
published body against that record's own ``to_jsonable()``, and one hands the
command a record whose lines were replaced with values the store cannot
produce and requires those to be what it prints.

One of the doctored lines is DERIVED, and that is the half that was missing.
``net_closed_acct`` and ``cash_book_sum_acct`` are the lines a recomputation
would take as INPUTS, so neither can see a CLI that re-sums the subtotal from
the five lines above it -- and the real store cannot either, because on
CPython 3.12+ ``sum()`` is Neumaier-compensated and reproduces ``report.py``'s
Decimal answer to the cent on this capture. The guard therefore rested on a
float rounding accident until ``subtotal_acct`` was doctored too, to a value
the five lines above it do not add up to.

Rows are the LIVE account's own, from
``tests/brokers/automanager/fixtures/trades/live_2026_10_03/venue.json``: 104
bookings rows and 41 trades rows, swept under one sweep covering
2026-08-01..2026-10-03.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from alphalens_cli.commands import cashbook
from alphalens_pipeline.account_cash import buckets, money, paths, record, report, sweep
from alphalens_pipeline.brokers.account_cash import AccountCashRead
from alphalens_pipeline.brokers.fill_history import ReadWindow
from typer.testing import CliRunner

from tests.account_cash.cashbook_fixture import BALANCE, BOOKINGS, TRADES
from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase

SHOW_SEAM = "alphalens_pipeline.brokers.automanager.unit_env._systemctl_show"
READ_SEAM = "alphalens_pipeline.brokers.automanager.unit_env._read_text"

#: The command module itself, for the gates that read its SOURCE rather than
#: its imported object. One definition, because two spellings of one path is
#: how one of them comes to point at nothing and pass.
_CASHBOOK_SOURCE = (
    Path(__file__).resolve().parents[2]
    / ".."
    / "alphalens-pipeline"
    / "alphalens_cli"
    / "commands"
    / "cashbook.py"
).resolve()

#: The published failure contract, whose `cashbook_totals_refused` reason table
#: tells an operator how to recover from each refusal. One definition beside the
#: source above, for the same reason.
_CONTRACT_README = (
    Path(__file__).resolve().parents[2] / ".." / "alphalens-broker-contract" / "README.md"
).resolve()

#: The heading of that table's own section. The reasons for the OTHER cash-book
#: code sit under a heading of their own, so a slice from here to the next
#: ``####`` can only ever hold these.
_TOTALS_REASON_SECTION = "#### `cashbook_totals_refused` publishes a `reason` too"

#: The two `details` keys EVERY refusal of this code carries, named in that
#: section's preamble rather than in any row. The per-row check below is about
#: the keys a row has to name for itself.
_REFUSAL_KEYS_EVERY_ROW_CARRIES = frozenset({"reason", "env"})

_EXIT_FAILED = 1
_EXIT_USAGE = 2
#: What the shared emitter answers for a RETRYABLE code. Named here because one
#: cash-book failure is retryable -- a journal this process cannot read now is
#: readable once the cause clears, and this command writes nothing either way.
_EXIT_TRANSIENT = 7

_LIVE = "live"
#: The window the one seeded sweep ASKED for -- the whole capture.
_SWEPT_SINCE = "2026-08-01"
_SWEPT_UNTIL = "2026-10-03"
_SWEPT_AT = dt.datetime(2026, 10, 4, 0, 31, tzinfo=dt.UTC)

#: The rails a LIVE composition would carry. ``totals`` composes BEST-EFFORT
#: (it holds no broker and places nothing), so these only keep the composed
#: branch exercised rather than the warning one.
_LIVE_RAILS = {
    "ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC": "1.0",
    "ALPHALENS_BROKER_DAILY_LOSS_LIMIT_R": "1.0",
    "ALPHALENS_BROKER_MAX_PICK_NOTIONAL": "15000",
    "ALPHALENS_BROKER_SIZING_EQUITY_MODE": "declared",
    "ALPHALENS_BROKER_EXIT_POLICY": "breakeven_trail",
    "ALPHALENS_BROKER_MAX_FEE_BPS": "1000",
    "ALPHALENS_BROKER_ENTRY_TRAIL_BPS": "50",
}
_UNIT_PROPERTIES = {
    "LoadState": "loaded",
    "Environment": " ".join(f"{key}={value}" for key, value in _LIVE_RAILS.items()),
    "EnvironmentFiles": "/etc/alphalens/env (ignore_errors=no)",
    "DropInPaths": "",
    "NeedDaemonReload": "no",
}

#: Every top-level key ``alphalens.cashbook.totals/v1`` publishes, stated here
#: rather than derived: an added field inside a major version is a line
#: somebody has to write down. An ALLOWLIST, because a blocklist cannot see
#: the next field's name.
_PUBLISHED_TOP_LEVEL_KEYS = (
    "schema",
    "env",
    "basis",
    "coverage",
    "cost_basis_open_asof",
    "cost_basis_open_excluded",
    "residual_terms",
    "buckets",
    "balance_checks",
    "net_closed_acct",
    "cost_basis_open_acct",
    "income_acct",
    "unclassified_acct",
    "residual_acct",
    "subtotal_acct",
    "deposits_acct",
    "cash_book_sum_acct",
)

#: The key set of one published money value. Stated, not read off
#: :class:`~alphalens_pipeline.account_cash.money.AcctAmount`: reading it back
#: off the dataclass would make the assertion production against production.
_MONEY_LEAF_KEYS = {"value", "currency", "rows", "null_reason", "value_rendered"}

#: How many money values the body carries at least. Eight published lines plus
#: five bucket totals plus three per balance check; a walk that found nothing
#: would satisfy every per-leaf assertion below without it.
_MONEY_LEAF_FLOOR = 10

#: How many leaves the whole body carries at least. Measured at 100 on the
#: seeded capture; a floor rather than the number, because a field added to the
#: body must not make a parity test red for the wrong reason.
_LEAF_FLOOR = 90

#: The one published key the prose does not carry. ``schema`` is the machine
#: contract's own identity, not a fact about this window, and an operator
#: cannot act on it. Named rather than silently skipped, so a second exemption
#: is a line somebody writes down.
_HUMAN_EXEMPT_KEYS = ("schema",)

#: The word for a published null and for an empty list. ``null`` and ``[]``
#: are what the JSON body says; a human renderer printing JSON literals is a
#: serialiser wearing a renderer's name.
_NONE = "none"

#: A value the seeded store cannot produce and no identity over it allows, for
#: the tests that ask which side of the seam a published number came from.
_ABSURD_PLN = -123456.78

#: The published money lines, in render order, keyed by the body field each
#: one renders -- READ OFF THE RENDERER rather than copied. A second copy here
#: would be a second source of truth: a line reordered or moved between the
#: two blocks would leave the copies disagreeing while every test still passed,
#: which is how the open cost basis came to sit among terms it is not one of.
_ADDED_LINES: tuple[tuple[str, str], ...] = cashbook._TOTALS_MONEY_LINES
_STATED_LINES: tuple[tuple[str, str], ...] = cashbook._TOTALS_STATED_LINES
_MONEY_LINES: tuple[tuple[str, str], ...] = _ADDED_LINES + _STATED_LINES
_MONEY_LABELS = tuple(label for _key, label in _MONEY_LINES)
_MONEY_LABEL_BY_FIELD = dict(_MONEY_LINES)
_LABEL_OF = _MONEY_LABEL_BY_FIELD.__getitem__
_NET_LABEL = _LABEL_OF("net_closed_acct")
_COST_BASIS_LABEL = _LABEL_OF("cost_basis_open_acct")
_UNCLASSIFIED_LABEL = _LABEL_OF("unclassified_acct")
_RESIDUAL_LABEL = _LABEL_OF("residual_acct")
#: The two DERIVED money lines: the block above each one adds up to it, which
#: is what makes them the lines a CLI could recompute instead of rendering.
_SUBTOTAL_LABEL = _LABEL_OF("subtotal_acct")
_SUBTOTAL_LINE = ("subtotal_acct", _SUBTOTAL_LABEL)
_DEPOSITS_LABEL = _LABEL_OF("deposits_acct")
_CASH_BOOK_SUM_LABEL = _LABEL_OF("cash_book_sum_acct")


#: The labels of the fact rows -- the ones that carry ``name value`` pairs
#: rather than one money cell. They share one column width.
_FACT_LABELS = (
    "basis",
    "exclusions",
    "fills_unassigned",
    "restated",
    "malformed",
    "coverage",
    "bucket",
    "balance",
)


def _reject_json_constant(token: str) -> None:
    """``json.loads`` accepts NaN / Infinity; a strict reader does not."""
    raise ValueError(f"non-strict JSON constant {token!r} on stdout")


def _strict_json(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_json_constant)


def _json_lines(stderr: str) -> list[dict]:
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
    """The one failure object on a JSON-mode stderr, and it is the LAST line.

    stderr carries operator chrome too (the composition banner, a best-effort
    warning), so the machine rule has two halves: exactly one stderr line is a
    JSON object, AND it is the last line.
    """
    objects = _json_lines(result.stderr)
    assert len(objects) == 1, f"expected exactly one JSON line on stderr, got {objects}"
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert _json_lines(lines[-1]) == objects, (
        f"the failure object must be the LAST stderr line, got {lines[-1]!r}"
    )
    return objects[0]


def _totals_reason_rows() -> dict[str, str]:
    """Each published ``cashbook_totals_refused`` reason, with its own row text.

    Sliced from this code's own ``####`` heading to the next one, the way
    ``tests/brokers/test_broker_failure_reasons_published.py`` slices it: the
    other cash-book code's reasons sit under a heading of their own, so one
    slice can never hold two vocabularies.
    """
    text = _CONTRACT_README.read_text(encoding="utf-8")
    section = text[text.index(_TOTALS_REASON_SECTION) :]
    section = section[: section.index("\n####", len(_TOTALS_REASON_SECTION))]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 4:
            continue
        # The header's own second cell is `details.reason` and the separator's
        # is `---`; neither is an identifier, and a reason is.
        reason = cells[2].strip("`")
        if reason.isidentifier():
            rows[reason] = cells[3]
    return rows


def _named_details_keys(row: str) -> set[str]:
    """The ``details.<key>`` names one published row promises, as bare keys."""
    found = set()
    for token in row.replace("`", " ").replace(",", " ").split():
        if token.startswith("details."):
            found.add(token.removeprefix("details.").strip("."))
    return found


def _leaves(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Every LEAF of the published body, as ``(dotted path, value)``.

    Three rules the sync command's version does not need:

    * **lists are recursed into**, with the index as a path segment.
      ``buckets``, ``balance_checks`` and ``coverage.gaps`` are lists, and a
      dict-only walk hands each one to the prose check as a single leaf whose
      token is a Python list repr -- a token no renderer prints, so the whole
      list half of the body would read as covered and be unchecked;
    * **an EMPTY list is a leaf of its own.** ``gaps: []`` is the common case
      and it is a fact ("no hole in the coverage"), so the renderer owes a
      word for it;
    * **one money value is ONE fact plus its row count.** ``value_rendered``
      carries the number WITH its currency, or the null reason when there is
      no number, so ``value``, ``currency`` and ``null_reason`` are that same
      fact in parts; requiring each separately would demand the prose print
      the bare float, which is exactly what memo section 5(d) forbids.
    """
    if isinstance(value, dict):
        if set(value) == _MONEY_LEAF_KEYS:
            return [
                (f"{prefix}value_rendered", value["value_rendered"]),
                (f"{prefix}rows", value["rows"]),
            ]
        found: list[tuple[str, Any]] = []
        for key, child in value.items():
            found.extend(_leaves(child, f"{prefix}{key}."))
        return found
    if isinstance(value, list):
        if not value:
            return [(prefix.rstrip("."), value)]
        nested: list[tuple[str, Any]] = []
        for index, child in enumerate(value):
            nested.extend(_leaves(child, f"{prefix}{index}."))
        return nested
    return [(prefix.rstrip("."), value)]


def _human_token(value: Any) -> str:
    """How one published leaf has to read in the prose.

    ``yes`` / ``no`` rather than ``true`` / ``false``: the JSON body is what a
    machine branches on, and a human renderer printing JSON literals is the
    tell of a renderer that is really a serialiser. A null and an empty list
    share one word, because they say the same thing to an operator -- there is
    nothing here -- and two words for one fact is how a reader learns to skim.
    """
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None or value == []:
        return _NONE
    return str(value)


def _collapsed_lines(human: str) -> list[str]:
    """The prose, one whitespace-collapsed line per rendered line.

    A TRAILING COMMA is dropped from every token. The cost-basis note reads
    ``as of 2026-10-03, 0 more held``, and a date with a comma behind it is the
    same published fact as the date; without this the whole note would read as
    carrying no date at all.
    """
    collapsed = []
    for line in human.splitlines():
        if line.strip():
            collapsed.append(" ".join(token.rstrip(",") for token in line.split()))
    return collapsed


def _label_block(human: str, label: str) -> str:
    """Every prose line whose first field is ``label``, whitespace collapsed.

    Per label rather than over the whole output, because the counts are mostly
    zeros: ``cash_events 0`` is in two mappings at once, so a check against
    the full text cannot tell which label printed it.
    """
    return " | ".join(line for line in _collapsed_lines(human) if line.split()[0] == label)


#: The block key for the one line that is not selected by a label: the header,
#: which is where the WINDOW the whole report answers for is stated.
_HEADER_BLOCK = "header"

#: The four small mappings the basis publishes, each under its own prose label.
_MAPPING_LABELS = ("exclusions", "fills_unassigned", "restated", "malformed")

#: The three amounts every balance check publishes, each on its own prose line.
_BALANCE_AMOUNTS = ("expected", "observed", "difference")

#: The two basis fields the HEADER carries rather than a ``basis`` line: the
#: window the report answers for.
_HEADER_BASIS_FIELDS = ("window_since", "window_until")

#: Balance-check fields the renderer prints WITHOUT their own name beside them.
_BALANCE_POSITIONAL = ("kind", "verdict", "since", "until")

#: The two loose top-level scalars that belong to the cost-basis money LINE:
#: that line's as-of and its held-but-excluded count. On any other line they
#: would read as facts about a different number.
_COST_BASIS_SCALARS = ("cost_basis_open_asof", "cost_basis_open_excluded")

#: How many prose lines one leaf's block may span. Measured: 1 for a money
#: line, a bucket line, one balance amount, the header and each mapping; 4 for
#: the basis; 3 for the coverage; 5 for one balance check. A bound rather than
#: the number, and far under the ~30 lines of the whole report, because a block
#: that widened to the whole output is exactly the hole this replaced.
_BLOCK_LINE_CEILING = 5


def _prose_block(human: str, block: str) -> str:
    """The prose lines one published leaf must appear on, joined by ``|``.

    NARROW by construction. The whole-output search this replaced could not see
    a dropped fact whose rendered token duplicated any other leaf's, and on
    this body duplicates are the normal case: ``0`` is printed about twenty
    times, ``none`` by three different renderers, ``104`` by three basis
    counters, and both window dates have a twin on the coverage line. Measured
    on the committed renderer: eight published facts could be deleted from the
    prose with every test green.
    """
    lines = _collapsed_lines(human)
    kind, _, rest = block.partition(":")
    if kind == _HEADER_BLOCK:
        return lines[0]
    if kind == "money":
        return lines[_line_index(lines, rest)]
    if kind in ("bucket", "balance"):
        want = [kind, *rest.split(":")]
        return " | ".join(line for line in lines if line.split()[: len(want)] == want)
    return _label_block(human, block)


def _requirement(path: str, payload: dict) -> tuple[str, str | None]:
    """Which block must carry one leaf, and the NAME the renderer prints by it.

    A ``name`` turns the check into a PAIR (``malformed_windows 0``) rather
    than a token search, which is what gives it power over a body whose counts
    are nearly all zeros. ``None`` means the renderer prints the value as a
    bare cell -- a money amount, a bucket's own fields, a balance check's kind
    and dates, and the two window dates in the header.

    An unknown path RAISES rather than being skipped: a field added to the body
    is then red here until somebody says which line is supposed to carry it,
    which is the property the old whole-output search gave up.
    """
    parts = path.split(".")
    head = parts[0]
    if head == "env":
        return _HEADER_BLOCK, None
    if head == "basis":
        if parts[1] in _HEADER_BASIS_FIELDS:
            return _HEADER_BLOCK, None
        if parts[1] in _MAPPING_LABELS:
            return parts[1], parts[2]
        return "basis", parts[1]
    if head == "coverage":
        # A gap's own dates and a `cannot_detect` entry are printed as bare
        # cells; the EMPTY list is printed under its own name, which is the
        # fact a shorter list would otherwise read as fuller coverage.
        if parts[1] in ("gaps", "cannot_detect"):
            return "coverage", parts[1] if len(parts) == 2 else None
        return "coverage", parts[1]
    if head in _COST_BASIS_SCALARS:
        return f"money:{_COST_BASIS_LABEL}", None
    # Every term the residual subtracted, on the residual's own line: that note
    # is the only place the report says WHAT was subtracted, and one of the
    # terms is rendered below the line.
    if head == "residual_terms":
        return f"money:{_RESIDUAL_LABEL}", None
    if head == "buckets":
        return f"bucket:{payload['buckets'][int(parts[1])]['name']}", None
    if head == "balance_checks":
        kind = payload["balance_checks"][int(parts[1])]["kind"]
        if parts[2] in _BALANCE_AMOUNTS:
            return f"balance:{kind}:{parts[2]}", None
        if parts[2] in _BALANCE_POSITIONAL:
            return f"balance:{kind}", None
        return f"balance:{kind}", parts[2]
    if head in _MONEY_LABEL_BY_FIELD:
        return f"money:{_MONEY_LABEL_BY_FIELD[head]}", None
    raise AssertionError(
        f"no prose rule for the published leaf {path!r}: say which line of the report "
        f"is supposed to carry it, in `_requirement`"
    )


def _assert_prose_carries_every_leaf(case: unittest.TestCase, payload: dict, human: str) -> None:
    """Every leaf's token, whole and space-bounded, on the LINE that owes it.

    Space-bounded rather than a plain substring: ``0`` is inside ``104`` and
    ``no`` is inside ``none``, so a substring check passes whatever the
    renderer drops. Per BLOCK rather than over the whole output, and as a
    ``name value`` pair wherever the renderer prints the name, because a token
    that duplicates another leaf's is not covered by a search over everything.
    """
    for path, value in _leaves(payload):
        if path in _HUMAN_EXEMPT_KEYS:
            continue
        block, name = _requirement(path, payload)
        rendered = _prose_block(human, block)
        token = _human_token(value)
        wanted = token if name is None else f"{name} {token}"
        with case.subTest(field=path, block=block):
            case.assertIn(f" {wanted} ", f" {rendered} ", f"{path} is not on the {block} line(s)")


def _money_cell_ends(run, lines: list[str]) -> set[int]:
    """Where each money cell ENDS, which is what a right-aligned column means.

    The cell is located by the token the BODY publishes (``value_rendered``),
    searched past the label, so a note repeating the same amount cannot be
    mistaken for the cell. ``run`` is the bound ``totals`` helper, used to read
    the same window's JSON body.
    """
    payload = _strict_json(run("--format", "json").stdout)
    ends = set()
    for key, label in _MONEY_LINES:
        rendered = payload[key]["value_rendered"]
        line = lines[_line_index(lines, label)]
        ends.add(line.index(rendered, len(label)) + len(rendered))
    return ends


def _is_money_line(line: str) -> bool:
    """Whether a rendered line is one of the money lines, by its label alone.

    Membership of a ruled block is decided from the RENDER, so a line moved
    into or out of a block changes what the arithmetic check sums.
    """
    return any(line.startswith(label) for label in _MONEY_LABELS)


def _money_in(line: str) -> float:
    """The number a money line renders, read back off the prose.

    Read from the render and not from the body on purpose: this is what the
    reader adds up, so a body that totals correctly while the render puts a
    line in the wrong block must still fail.
    """
    label = next(one for one in _MONEY_LABELS if line.startswith(one))
    amount = line[len(label) :].lstrip().split(" ", 1)[0]
    try:
        return float(amount)
    except ValueError as exc:  # a null renders as its reason, which cannot be added
        raise AssertionError(f"{label!r} renders {amount!r}, which is not a number") from exc


def _line_index(lines: list[str], label: str) -> int:
    """Where the line carrying ``label`` sits, so ORDER can be asserted."""
    for index, line in enumerate(lines):
        if line.startswith(label):
            return index
    raise AssertionError(f"no line starts with {label!r}:\n" + "\n".join(lines))


def _money_leaves(value: Any, prefix: str = "") -> list[tuple[str, dict]]:
    """Every published money value, as ``(dotted path, the object)``."""
    found: list[tuple[str, dict]] = []
    if isinstance(value, dict):
        if set(value) == _MONEY_LEAF_KEYS:
            return [(prefix.rstrip("."), value)]
        for key, child in value.items():
            found.extend(_money_leaves(child, f"{prefix}{key}."))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_money_leaves(child, f"{prefix}{index}."))
    return found


class _CashbookTotalsCase(IsolatedHomeTestCase):
    """Isolated home, a cleared environment, and one seeded sweep."""

    def setUp(self) -> None:
        super().setUp()
        self.runner = CliRunner()
        env_patcher = mock.patch.dict("os.environ", {}, clear=True)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def seed(
        self,
        *,
        since: str = _SWEPT_SINCE,
        until: str = _SWEPT_UNTIL,
        now: dt.datetime = _SWEPT_AT,
        events: tuple[Any, ...] = tuple(BOOKINGS),
    ) -> None:
        """Store the whole capture under one sweep, through the real sweep path."""
        read = AccountCashRead(
            events=tuple(events),
            trades=tuple(TRADES),
            balance=BALANCE,
            events_window=ReadWindow(since, until, len(events)),
            trades_window=ReadWindow(since, until, len(TRADES)),
            skipped_reason=None,
        )
        plan = sweep.plan_sweep(
            read,
            env=_LIVE,
            since=dt.date.fromisoformat(since),
            until=dt.date.fromisoformat(until),
        )
        sweep.apply_sweep(plan, now=now)

    def invoke(self, argv: list[str]):
        """Run one cashbook command with the unit-composition seams faked."""
        from alphalens_cli.commands.cashbook import cashbook_app

        with (
            mock.patch(SHOW_SEAM, lambda unit, prop: _UNIT_PROPERTIES[prop]),
            mock.patch(READ_SEAM, lambda _path: ""),
        ):
            return self.runner.invoke(cashbook_app, argv)

    def totals(
        self,
        *extra: str,
        env: str = _LIVE,
        since: str = _SWEPT_SINCE,
        until: str = _SWEPT_UNTIL,
    ):
        return self.invoke(["totals", "--since", since, "--until", until, "--env", env, *extra])

    def record(self, *, since: str = _SWEPT_SINCE, until: str = _SWEPT_UNTIL):
        """The record the command must render, built by the same function."""
        return report.build_totals(
            env=_LIVE,
            since=dt.date.fromisoformat(since),
            until=dt.date.fromisoformat(until),
        )


class TheEnvelopeIsTheSameContract(_CashbookTotalsCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed()

    def test_the_envelope_leads_with_schema_and_env(self) -> None:
        payload = _strict_json(self.totals("--format", "json").stdout)

        self.assertEqual(list(payload)[:2], ["schema", "env"])
        self.assertEqual(payload["schema"], "alphalens.cashbook.totals/v1")
        self.assertEqual(payload["env"], _LIVE)

    def test_stdout_is_exactly_one_strict_json_value(self) -> None:
        result = self.totals("--format", "json")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        self.assertIsInstance(_strict_json(result.stdout), dict)

    def test_the_published_body_keys_are_exactly_this_set(self) -> None:
        payload = _strict_json(self.totals("--format", "json").stdout)

        self.assertEqual(sorted(payload), sorted(_PUBLISHED_TOP_LEVEL_KEYS))

    def test_the_body_is_the_records_own_answer_and_not_a_second_computation(self) -> None:
        """**Mutation it catches:** any number recomputed in the CLI.

        The body is compared against the record's own wire form, so a CLI that
        re-derived one line -- a subtotal, a residual, a bucket -- from the
        others would have to reproduce it exactly or be red here. The envelope
        owns ``schema`` and ``env``, so those two are the only difference.
        """
        payload = _strict_json(self.totals("--format", "json").stdout)

        expected = self.record().to_jsonable()
        expected.pop("env")

        self.assertEqual({key: payload[key] for key in expected}, expected)


class TheCommandRendersTheRecordAndComputesNothing(_CashbookTotalsCase):
    """A record the store cannot produce, to show which side the numbers come from.

    **Mutation it catches:** a CLI that computes a line of its own. Every
    number here disagrees with the seeded store and with every identity over
    it, so a command that recomputed anything would print the store's figure
    and go red.

    One of the three doctored lines is DERIVED, and that one is the point.
    ``net_closed_acct`` and ``cash_book_sum_acct`` are leaves of the
    dependency graph as the CLI sees it -- the lines a recomputation would
    take as INPUTS. A CLI that re-summed ``subtotal_acct`` from the five lines
    above it would be invisible against those two, and invisible against the
    real store as well: on CPython 3.12+ ``sum()`` is Neumaier-compensated and
    reproduces ``report.py``'s Decimal answer to the cent on this capture, so
    the guard would have rested on a float rounding accident. ``ABSURD_SUBTOTAL``
    is a value the five doctored lines above it do NOT add up to, which is
    what makes the re-summing refutable rather than lucky.
    """

    ABSURD = -123456.78
    #: Not the sum of the five lines above the subtotal once they are doctored
    #: (that sum is -127047.59), so a re-summed subtotal cannot land here.
    ABSURD_SUBTOTAL = -99999.99

    def setUp(self) -> None:
        super().setUp()
        self.seed()
        real = self.record()
        self.doctored = replace(
            real,
            net_closed_acct=replace(real.net_closed_acct, value=self.ABSURD),
            subtotal_acct=replace(real.subtotal_acct, value=self.ABSURD_SUBTOTAL),
            cash_book_sum_acct=replace(real.cash_book_sum_acct, value=self.ABSURD),
        )

    def totals_of_the_doctored_record(self, *extra: str):
        with mock.patch.object(report, "build_totals", return_value=self.doctored):
            return self.totals(*extra)

    def test_the_premise_is_a_subtotal_the_lines_above_it_do_not_add_up_to(self) -> None:
        """Positive control on the arithmetic the class rests on: if the five
        doctored lines happened to sum to ``ABSURD_SUBTOTAL``, the two
        assertions below would pass under a CLI that re-sums them."""
        above = [field for field, _label in _MONEY_LINES[: _MONEY_LINES.index(_SUBTOTAL_LINE)]]
        body = self.doctored.to_jsonable()

        self.assertNotAlmostEqual(
            sum(body[field]["value"] for field in above), self.ABSURD_SUBTOTAL, places=2
        )

    def test_the_json_body_carries_the_records_number(self) -> None:
        payload = _strict_json(self.totals_of_the_doctored_record("--format", "json").stdout)

        self.assertEqual(payload["net_closed_acct"]["value"], self.ABSURD)
        self.assertEqual(payload["subtotal_acct"]["value"], self.ABSURD_SUBTOTAL)
        self.assertEqual(payload["cash_book_sum_acct"]["value"], self.ABSURD)

    def test_the_prose_carries_the_derived_lines_number_too(self) -> None:
        """The other half of the same seam: a recomputation inside the human
        renderer rather than inside the body."""
        human = self.totals_of_the_doctored_record().stdout
        lines = [line for line in human.splitlines() if line.strip()]

        subtotal = lines[_line_index(lines, _SUBTOTAL_LABEL)]

        self.assertIn(f"{self.ABSURD_SUBTOTAL:.2f} PLN", subtotal)


class EveryPublishedMoneyValueCarriesItsBasis(_CashbookTotalsCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.payload = _strict_json(self.totals("--format", "json").stdout)
        self.leaves = _money_leaves(self.payload)

    def test_the_walk_found_the_bodys_money_values(self) -> None:
        """Positive control: a walk that found nothing would make both
        assertions below vacuous, which is how every such gate rots."""
        self.assertGreaterEqual(len(self.leaves), _MONEY_LEAF_FLOOR)

    def test_every_money_value_carries_its_currency_and_its_rows(self) -> None:
        for path, leaf in self.leaves:
            with self.subTest(field=path):
                self.assertEqual(set(leaf), _MONEY_LEAF_KEYS)

    def test_a_null_money_value_carries_a_reason_and_a_number_does_not(self) -> None:
        """The ``oneOf`` discipline, on the wire rather than in the dataclass.

        **Mutation it catches:** a body hand-built from the record's parts,
        which can publish a ``null`` with no reason (prints as an empty cell)
        or a number beside one (prints as a figure that is also a refusal).
        """
        for path, leaf in self.leaves:
            with self.subTest(field=path):
                self.assertEqual(leaf["value"] is None, leaf["null_reason"] is not None)
                if leaf["null_reason"] is not None:
                    self.assertIn(leaf["null_reason"], money.NULL_REASONS)


class AUsageRefusalRendersTheWayTheCallerAsked(_CashbookTotalsCase):
    def test_an_unknown_format_exits_two_with_stdout_empty(self) -> None:
        result = self.totals("--format", "xml")

        self.assertEqual(result.exit_code, _EXIT_USAGE)
        self.assertEqual(result.stdout, "")

    def test_a_bad_date_exits_two_in_the_format_the_caller_asked_for(self) -> None:
        result = self.invoke(
            [
                "totals",
                "--since",
                "nope",
                "--until",
                _SWEPT_UNTIL,
                "--env",
                _LIVE,
                "--format",
                "json",
            ]
        )

        self.assertEqual(result.exit_code, _EXIT_USAGE)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "usage")
        self.assertEqual(failure["details"]["option"], "--since")

    def test_an_inverted_window_is_a_usage_error_before_the_store_is_read(self) -> None:
        """**Mutation it catches:** the window check lost, which sends an
        inverted window into ``build_totals`` as a ``ValueError`` traceback."""
        result = self.invoke(
            [
                "totals",
                "--since",
                _SWEPT_UNTIL,
                "--until",
                _SWEPT_SINCE,
                "--env",
                _LIVE,
                "--format",
                "json",
            ]
        )

        self.assertEqual(result.exit_code, _EXIT_USAGE)
        self.assertEqual(result.stdout, "")
        self.assertEqual(_failure_from(result)["code"], "usage")


class TheProseCarriesEveryFactTheJsonBodyPublishes(_CashbookTotalsCase):
    """The repo CLI doctrine: one internal result rendered two ways.

    Driven by the PAYLOAD, over every leaf of it, so the next field added to
    the body is covered without anybody listing it here. The walk recurses
    into LISTS as well as dicts (``buckets``, ``balance_checks`` and
    ``coverage.gaps`` are lists, and a dict-only walk hands each of them to
    the prose check as one Python list repr, which no renderer would ever
    print).

    Each leaf is checked against the LINE that owes it, and as a ``name value``
    pair wherever the renderer prints the name. A search over the whole output
    cannot see a dropped fact whose token duplicates another leaf's, and on
    this body that is the normal case: eight published facts -- the window the
    report answers for, ``coverage.history_begins``,
    ``coverage.malformed_windows``, the ``gaps none`` line,
    ``cost_basis_open_excluded``, ``rows_in_store``, ``rows_classified`` and
    the word for every null -- could each be deleted from the prose with every
    test green.
    """

    def setUp(self) -> None:
        super().setUp()
        self.seed()

    def test_the_walk_found_the_whole_body(self) -> None:
        """Positive control with a FLOOR: a walk that stopped at the first
        container would make every parity assertion below cover a handful of
        scalars while reading as full coverage."""
        payload = _strict_json(self.totals("--format", "json").stdout)

        leaves = _leaves(payload)

        self.assertGreaterEqual(len(leaves), _LEAF_FLOOR)
        # The null half of the body really is here: one snapshot cannot answer
        # the incremental check, so its three amounts render as their reason.
        # Stated rather than assumed, because the class below says this run is
        # what covers that half.
        self.assertIn(
            ("balance_checks.1.difference.value_rendered", "account_amount_not_reported"),
            leaves,
        )
        self.assertIn(("coverage.gaps", []), leaves)

    def test_every_leaf_of_the_body_appears_in_the_prose(self) -> None:
        payload = _strict_json(self.totals("--format", "json").stdout)

        human = self.totals().stdout

        _assert_prose_carries_every_leaf(self, payload, human)

    def test_every_leaf_is_checked_against_a_narrow_block_of_its_own(self) -> None:
        """Positive control on the BLOCKS, not on the walk.

        The check above is only as strong as the lines it looks at, and the
        failure it replaced was a search over the whole output: a block that
        widened back to everything would keep every assertion green while
        covering nothing. So each leaf's block must be non-empty and at most
        ``_BLOCK_LINE_CEILING`` lines of the ~30 the report prints.
        """
        payload = _strict_json(self.totals("--format", "json").stdout)

        human = self.totals().stdout
        printed = len(_collapsed_lines(human))

        self.assertGreater(printed, 2 * _BLOCK_LINE_CEILING, human)
        for path, _value in _leaves(payload):
            if path in _HUMAN_EXEMPT_KEYS:
                continue
            block, _name = _requirement(path, payload)
            rendered = _prose_block(human, block)
            with self.subTest(field=path, block=block):
                self.assertNotEqual(rendered, "", f"{block} selects no line")
                self.assertLessEqual(rendered.count(" | ") + 1, _BLOCK_LINE_CEILING, rendered)

    def test_the_prose_carries_the_number_the_record_holds(self) -> None:
        """**Mutation it catches:** a renderer that computes a line of its own.

        The record handed in here disagrees with the seeded store and with
        every identity over it, so a prose line derived from the others would
        print the store's figure instead.
        """
        real = self.record()
        doctored = replace(real, net_closed_acct=replace(real.net_closed_acct, value=_ABSURD_PLN))

        with mock.patch.object(report, "build_totals", return_value=doctored):
            human = self.totals().stdout

        self.assertIn(f"{_ABSURD_PLN:.2f} PLN", human)

    def test_every_published_count_appears_under_its_own_label(self) -> None:
        """The other half, which full coverage alone does not give: the counts
        are mostly zeros, so a renderer printing one mapping's numbers under
        another's label would satisfy every leaf check above.

        ``fills_unassigned`` is the body's key and ``unassigned`` is not, so
        the label is read off the payload rather than written here.
        """
        payload = _strict_json(self.totals("--format", "json").stdout)

        human = self.totals().stdout

        for label in ("exclusions", "fills_unassigned", "restated", "malformed"):
            block = _label_block(human, label)
            self.assertNotEqual(block, "", f"{label} has no line of its own")
            for name, value in payload["basis"][label].items():
                with self.subTest(label=label, entry=name):
                    self.assertIn(f"{name} {value}", block)

    def test_the_fact_rows_line_up_in_one_column(self) -> None:
        """Asserted as "every fact row agrees", not against a number, so a new
        label either fits or turns this red. ``fills_unassigned`` is 16
        characters; a width copied from the sync body's 15 starts that one row
        a character late while every other row lines up."""
        human = self.totals().stdout

        starts = {
            len(line) - len(line.split(maxsplit=1)[1])
            for line in human.splitlines()
            if line.strip() and line.split()[0] in _FACT_LABELS
        }

        self.assertEqual(len(starts), 1, human)


class TheProseCarriesAGapAndASecondBalanceCheckToo(_CashbookTotalsCase):
    """The same parity over the body shape where the lists are NOT empty.

    Two sweeps with a 21-day hole between them make ``coverage.gaps`` a real
    pair, and they give the incremental balance check the second snapshot it
    needs, so all three of its money values are numbers rather than named
    nulls. Neither shape appears in the full-span run above, where the gaps
    list is empty and the incremental check is ``not_checkable`` -- that run
    covers the NULL half of the body, this one the non-empty-list half.

    Each sweep takes its OWN id: one id folds the two journals onto one
    record, which moves ``history_begins`` instead of opening a gap.
    """

    def setUp(self) -> None:
        super().setUp()
        self.seed(since=_SWEPT_SINCE, until="2026-08-10")
        self.seed(since="2026-09-01", until=_SWEPT_UNTIL, now=_SWEPT_AT + dt.timedelta(hours=1))

    def test_the_window_really_has_a_gap_and_a_second_snapshot(self) -> None:
        """Positive control: with neither shape present the parity assertion
        below would be the full-span one again under a second name."""
        payload = _strict_json(self.totals("--format", "json").stdout)

        self.assertEqual(payload["coverage"]["gaps"], [["2026-08-11", "2026-08-31"]])
        self.assertIsNotNone(payload["balance_checks"][1]["difference"]["value"])

    def test_every_leaf_of_the_body_appears_in_the_prose(self) -> None:
        payload = _strict_json(self.totals("--format", "json").stdout)

        human = self.totals().stdout

        _assert_prose_carries_every_leaf(self, payload, human)


class TheFiveLoadBearingPropertiesOfTheRender(_CashbookTotalsCase):
    """Memo section 6's own list, each one as its own assertion.

    These are the properties a correct-looking renderer gets wrong: they are
    about ORDER, about which line a number sits on, and about a unit -- none
    of which the parity check above can see, because it only asks whether
    each fact appears somewhere.
    """

    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.human = self.totals().stdout
        self.lines = [line for line in self.human.splitlines() if line.strip()]

    def test_the_deposits_sit_below_the_subtotal_and_say_not_p_and_l(self) -> None:
        """24 000 PLN of funding is the easiest number in this repo to misread
        as profit. **Mutation it catches:** a renderer ordering by the body's
        own key order, which puts the deposits above the subtotal."""
        subtotal = _line_index(self.lines, _SUBTOTAL_LABEL)
        deposits = _line_index(self.lines, _DEPOSITS_LABEL)

        self.assertGreater(deposits, subtotal)
        self.assertIn("not P&L", self.lines[deposits])
        self.assertIn("24000.00 PLN", self.lines[deposits])
        self.assertNotIn("24000.00", self.lines[subtotal])

    def test_the_open_episodes_are_a_cost_basis_and_never_in_the_realized_line(self) -> None:
        """**Mutation it catches:** the cost basis folded into the net, which
        publishes money that is not realized as money that is."""
        net = self.lines[_line_index(self.lines, _NET_LABEL)]
        basis = self.lines[_line_index(self.lines, _COST_BASIS_LABEL)]

        self.assertIn("329.49 PLN", net)
        self.assertIn("after commission and exchange fee", net)
        self.assertNotIn("3594.86", net)
        self.assertIn("-3594.86 PLN", basis)
        self.assertIn("not realized", basis)
        # Its own as-of, which is NOT the window's end: the cost of a position
        # that is still open is as-of the last day the store can speak for.
        self.assertIn("as of 2026-10-03", basis)

    def test_the_unclassified_line_prints_at_zero(self) -> None:
        """Memo section 5(a): printed even at zero, so a type nobody has seen
        reads as a CHANGED number rather than as a row appearing in a list the
        reader has stopped scanning."""
        line = self.lines[_line_index(self.lines, _UNCLASSIFIED_LABEL)]

        self.assertIn("0.00 PLN", line)
        self.assertIn("0 rows", line)

    def test_the_residual_is_its_own_named_line(self) -> None:
        """Measured at the 2026-09-12 cut, where it is -7928.96 PLN.

        The cut rather than the full span, for a reason that survived the open
        cost basis leaving the residual's term list. The full span now reads
        -3594.86, which is the same number the open-basis line carries -- over
        the whole store the legs of the positions still held ARE the only
        window cash the four summed lines leave, so the two coincide there
        while meaning two different things. A renderer that printed the basis
        where the residual belongs would therefore pass at the full span. At
        the cut they are -7928.96 and -3594.86, and only that window can tell
        the two lines apart."""
        human = self.totals(until="2026-09-12").stdout
        lines = [line for line in human.splitlines() if line.strip()]

        line = lines[_line_index(lines, _RESIDUAL_LABEL)]

        self.assertIn("-7928.96 PLN", line)
        self.assertNotIn("-3594.86", line)

    def test_the_block_above_each_rule_adds_up_to_the_number_the_rule_carries(self) -> None:
        """The property a reader actually performs, and the one that was absent.

        A rule invites addition: everything above it is claimed to total to the
        line below it. While the open cost basis sat among the four terms, the
        five lines above the first rule came to -11422.02 at the 2026-09-12 cut
        while the rule carried -7827.16 -- wrong by exactly the basis, which is
        as-of the store's watermark and not a term of a window-scoped sum.
        Every other render test passed through that, because each one asks
        about one line.

        **Which lines the block holds is read off the RENDER, never from a list
        here.** A first version of this test summed a tuple of field names and
        so could not see a fifth line appear above the rule: it kept adding the
        four it already believed in and stayed green under exactly the defect
        it was written for.

        Run at the cut rather than the full span: the open basis is -3594.86 at
        both, but at the full span the residual carries that same number, so a
        renderer that put the basis back among the terms would be off by a
        value the block also legitimately contains."""
        lines = [
            line for line in self.totals(until="2026-09-12").stdout.splitlines() if line.strip()
        ]
        rules = [index for index, line in enumerate(lines) if set(line) in ({"-"}, {"="})]
        self.assertEqual(
            len(rules), 2, f"expected two rules, found {len(rules)}:\n" + "\n".join(lines)
        )

        start = 0
        for rule in rules:
            carried = lines[rule + 1]
            label = next(one for one in _MONEY_LABELS if carried.startswith(one))
            with self.subTest(rule=label):
                above = [line for line in lines[start:rule] if _is_money_line(line)]
                self.assertTrue(above, "a rule with nothing above it totals nothing")
                total = sum(_money_in(line) for line in above)
                self.assertAlmostEqual(
                    total,
                    _money_in(carried),
                    places=2,
                    msg=f"the {len(above)} line(s) above the rule do not add up to {label!r}",
                )
            start = rule + 1

    def test_no_line_the_report_states_but_does_not_add_sits_above_a_rule(self) -> None:
        """The structural half of the test above, so the two cannot drift.

        The arithmetic check would also pass if a stated line were moved above
        a rule and silently added into the total. This one says where the
        stated lines may appear: below the last rule, which is the sum.

        The floor is not decoration: without it, emptying
        ``_TOTALS_STATED_LINES`` makes the loop body run zero times and the
        test pass on a render that moved the line back among the terms -- the
        true-and-empty shape."""
        self.assertTrue(_STATED_LINES, "the report states at least one line it does not add")
        sum_at = _line_index(self.lines, _CASH_BOOK_SUM_LABEL)

        for field, label in _STATED_LINES:
            with self.subTest(stated=field):
                self.assertGreater(_line_index(self.lines, label), sum_at)

    def test_the_balance_line_says_it_agrees_and_that_it_cannot_see_a_restatement(self) -> None:
        """**Mutation it catches:** shipping the cumulative check without its
        ``detects_restatement`` flag, which makes "reconciles" read as proof
        the history is intact. Memo section 5(c) makes that sentence
        load-bearing.

        Both checks, in one assertion each, because copying the flag from one
        to the other is the mutation that makes the two indistinguishable: the
        cumulative check is store-wide against the latest balance and cannot
        see a correction to an old row, and the incremental one exists to.
        """
        block = _label_block(self.human, "balance")

        self.assertIn("cumulative reconciles detects_restatement no", block)
        self.assertIn("incremental not_checkable detects_restatement yes", block)
        self.assertIn("null_reason one_snapshot_only", block)

    def test_the_currency_is_on_every_number_and_not_only_in_a_header(self) -> None:
        """Memo section 6's mock puts PLN in the header and leaves the numbers
        bare; that is the half of it which disagrees with the memo's own
        section 5(d) and with the house renderer. A number beside a threshold
        in a different unit reads as a comfortable pass.
        """
        for label in _MONEY_LABELS:
            with self.subTest(line=label):
                cell = self.lines[_line_index(self.lines, label)][len(label) :].lstrip()
                self.assertRegex(cell, r"^-?\d+\.\d{2} PLN\b")

    def test_the_money_column_is_one_right_aligned_column(self) -> None:
        """Every money cell ENDS at one offset, which is what a right-aligned
        column means. **Mutation it catches:** a width literal drifting from
        the widest published label. The width of the AMOUNT column cannot be
        refuted here, where every amount is 12 characters or fewer -- that is
        ``ALineWithNoNumberPrintsItsReasonAndKeepsTheColumn``'s job, and it is
        why the two cases both exist.
        """
        self.assertEqual(len(_money_cell_ends(self.totals, self.lines)), 1, self.human)


class TheResidualsProseNamesTheTermsItSubtracted(_CashbookTotalsCase):
    """The one money line whose own label used to describe a different number.

    The record subtracts FOUR window-scoped lines, and the deposits are one of
    them -- rendered BELOW the residual, because 24 000 PLN of funding is the
    easiest number in this repo to misread as profit. While the label read
    "residual not explained above" and the note "cash the lines above do not
    explain", a reader who followed either one computed 20 405.14 PLN on the
    real capture against a line printing -3 594.86: wrong by exactly the
    deposits. Every arithmetic test passed through it, because the arithmetic
    was right and only the prose lied.

    So the terms are named, and they are named FROM THE RECORD: the last test
    is the one that makes that structural rather than a sentence somebody
    happened to write correctly once.
    """

    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.payload = _strict_json(self.totals("--format", "json").stdout)
        self.human = self.totals().stdout
        self.lines = _collapsed_lines(self.human)
        self.line = self.lines[_line_index(self.lines, _RESIDUAL_LABEL)]

    def test_the_premise_is_a_term_this_render_puts_below_the_residual(self) -> None:
        """Positive control: without a term rendered below it, "above" would
        have been true and this whole class says nothing."""
        self.assertIn("deposits_acct", self.payload["residual_terms"])
        self.assertGreater(
            _line_index(self.lines, _DEPOSITS_LABEL),
            _line_index(self.lines, _RESIDUAL_LABEL),
        )

    def test_the_lines_rendered_above_it_do_not_explain_the_number(self) -> None:
        """The measurement F1 rests on, as an assertion: the three money lines
        above the residual leave 20 405.14 PLN of the cash book sum, and the
        line prints -3 594.86. The gap is the deposits."""
        above = [
            field
            for field, _label in _ADDED_LINES[
                : _ADDED_LINES.index(("residual_acct", _RESIDUAL_LABEL))
            ]
        ]
        self.assertEqual(above, ["net_closed_acct", "income_acct", "unclassified_acct"])

        left = self.payload["cash_book_sum_acct"]["value"] - sum(
            self.payload[field]["value"] for field in above
        )

        self.assertAlmostEqual(left, 20405.14, places=2)
        self.assertAlmostEqual(self.payload["residual_acct"]["value"], -3594.86, places=2)
        self.assertAlmostEqual(left - self.payload["residual_acct"]["value"], 24000.00, places=2)

    def test_neither_the_label_nor_the_note_claims_the_lines_above(self) -> None:
        """**Mutation it catches:** the old wording, in either half of the row."""
        self.assertNotIn("above", self.line)

    def test_the_note_names_every_term_the_record_subtracted(self) -> None:
        for term in self.payload["residual_terms"]:
            with self.subTest(term=term):
                self.assertIn(f" {term} ", f" {self.line} ")

    def test_the_note_names_no_line_the_residual_did_not_subtract(self) -> None:
        """The other half, and the one a hand-written sentence fails: a note
        that listed the open cost basis, or the subtotal, would describe a
        subtraction the record never made."""
        for field, _label in _MONEY_LINES:
            if field == "residual_acct" or field in self.payload["residual_terms"]:
                continue
            with self.subTest(field=field):
                self.assertNotIn(field, self.line)

    def test_a_term_the_record_stops_subtracting_leaves_the_note(self) -> None:
        """**Mutation it catches:** a note that keeps naming a term the record
        has stopped subtracting.

        Here the record's own term list loses the deposits, so the residual
        becomes the 20 405.14 PLN a reader of the OLD label computed, and the
        note has to stop naming a term nothing subtracted.

        What it does NOT catch, stated because the first version of this
        docstring claimed it did: reverting the note to the four names spelled
        in the renderer. Under that mutation the note happens to name the same
        four terms this record still subtracts, so it reads correctly here.
        The hand-written form is caught by the test above, which asserts the
        note carries every term the record publishes and no other money field
        -- a sentence can only stay true by being derived.

        Patched on ``record``, which DEFINES the tuple, and not on ``report``,
        which re-exports it (#1805). A function reads a global from its
        defining module, so patching the re-export would drive only the
        subtraction and leave ``CashBookTotals.to_jsonable`` publishing the
        four original names -- the mutation would then be half-applied and the
        test would pass for the wrong reason. ``build_totals`` reads
        ``record.RESIDUAL_TERMS`` through the module for the same reason.
        """
        shorter = tuple(name for name in record.RESIDUAL_TERMS if name != "deposits_acct")
        with mock.patch.object(record, "RESIDUAL_TERMS", shorter):
            lines = _collapsed_lines(self.totals().stdout)

        line = lines[_line_index(lines, _RESIDUAL_LABEL)]

        self.assertNotIn(" deposits_acct ", f" {line} ")
        self.assertIn(" 20405.14 PLN ", f" {line} ")


class ALineWithNoNumberPrintsItsReasonAndKeepsTheColumn(_CashbookTotalsCase):
    """One booking row restated into ``EUR`` -- a vendor change or a bug.

    The report's documented degradation path: the lines that would have added
    two units refuse BY NAME rather than picking one or raising, and the
    command has to render that. A zero standing in for "unknown" is the same
    defect as a number beside a threshold in another unit, with no unit at all.

    This is also the ONLY shape in which a published money line renders longer
    than a number -- ``currency_mixed`` is 14 characters against
    ``20738.68 PLN``'s 12 -- so it is what gives the money column's test its
    power: a width literal that happens to fit the seeded capture overflows
    here.
    """

    def setUp(self) -> None:
        super().setUp()
        self.seed(events=(replace(BOOKINGS[0], account_currency="EUR"), *BOOKINGS[1:]))
        self.human = self.totals().stdout
        self.lines = [line for line in self.human.splitlines() if line.strip()]

    def test_the_premise_is_a_store_holding_two_account_currencies(self) -> None:
        """Positive control: without the second currency every line below is a
        number and the class says nothing the others do not."""
        payload = _strict_json(self.totals("--format", "json").stdout)

        self.assertIsNone(payload["basis"]["currency"])
        self.assertEqual(payload["cash_book_sum_acct"]["null_reason"], "currency_mixed")

    def test_the_line_prints_the_reason_and_never_a_zero(self) -> None:
        line = self.lines[_line_index(self.lines, _CASH_BOOK_SUM_LABEL)]

        self.assertIn("currency_mixed", line)
        self.assertNotIn("0.00", line)

    def test_the_money_column_still_ends_at_one_offset(self) -> None:
        """**Mutation it catches:** a money-column width written as a literal.
        12 fits every amount on the seeded capture, so only a longer cell can
        tell a literal from the derived width."""
        self.assertEqual(len(_money_cell_ends(self.totals, self.lines)), 1, self.human)

    def test_every_leaf_of_the_body_appears_in_the_prose(self) -> None:
        payload = _strict_json(self.totals("--format", "json").stdout)

        _assert_prose_carries_every_leaf(self, payload, self.human)


class AWindowTheStoreCannotAnswerIsAFailureAndNotASmallerNumber(_CashbookTotalsCase):
    """The three refusals ``build_totals`` makes, as one CLI code with a reason.

    A refusal is a non-zero exit with one failure object on stderr and nothing
    on stdout -- the shape ``cashbook sync`` already answers with, and the
    reason it is not a rendered answer carrying a refusal flag: this schema
    publishes no field a client could branch on but never observe, and a
    report whose lines were all null would still read as an answer.

    Each of the three names a different reason from a different branch, so
    none of them dies only together with another.
    """

    def test_a_sim_window_refuses_rather_than_summing_an_empty_store(self) -> None:
        """The store refuses SIM writes, so a SIM store is empty by
        construction and a sum over it answers 0.00 for an account nobody
        read. ``build_totals`` refuses before the first read; this is that
        refusal reaching a caller.
        """
        self.seed()

        result = self.totals("--format", "json", env="sim")

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "cashbook_totals_refused")
        self.assertEqual(failure["details"], {"reason": "sim_reports_unusable", "env": "sim"})
        self.assertFalse(failure["retryable"])

    def test_a_since_before_the_first_sweep_refuses_rather_than_clamping(self) -> None:
        self.seed()

        result = self.totals("--format", "json", since="2026-07-01")

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "cashbook_totals_refused")
        self.assertEqual(
            failure["details"],
            # The watermark rides along, because the recovery the published
            # table names is to ask for that day and a refusal leaves stdout
            # empty. `ThePublishedRecoveryIsOneTheCallerCanFollow` is where
            # that key is pinned against the table.
            {"reason": "before_first_sweep", "env": _LIVE, "history_begins": _SWEPT_SINCE},
        )

    def test_an_unswept_store_refuses_nothing_swept(self) -> None:
        result = self.totals("--format", "json")

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "cashbook_totals_refused")
        self.assertEqual(failure["details"], {"reason": "nothing_swept", "env": _LIVE})

    def test_a_window_past_the_watermark_refuses_rather_than_printing_zeros(self) -> None:
        """The answer that used to come back for a month nobody read: exit 0,
        ``cash book sum 0.00 PLN 0 rows``, ``trading and income subtotal
        0.00`` and ``balance cumulative reconciles``. The coverage block did
        carry the watermark honestly, two blocks under a headline that said
        the account was flat.
        """
        self.seed()

        result = self.totals("--format", "json", since="2026-11-01", until="2026-11-30")

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "cashbook_totals_refused")
        self.assertEqual(failure["details"]["reason"], "window_never_swept")
        self.assertEqual(failure["details"]["env"], _LIVE)

    def test_a_window_that_overlaps_the_swept_range_is_still_answered(self) -> None:
        """The negative control: the refusal is about a window NO sweep
        covered a day of, not about an ``until`` reaching past the watermark --
        which stays answered, with the shortfall on the coverage line."""
        self.seed()

        result = self.totals(since=_SWEPT_UNTIL, until="2026-11-30")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("cash book sum", result.stdout)

    def test_a_refusal_renders_as_prose_when_the_caller_asked_for_prose(self) -> None:
        result = self.totals()

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(result.stdout, "")
        self.assertIn("nothing_swept", result.stderr)

    def test_the_published_reasons_are_the_reports_own_vocabulary(self) -> None:
        """**Mutation it catches:** a fifth reason added to the report and
        left unpublished, or one of these four renamed on one side only. The
        table in the contract README is checked against this same tuple by
        ``tests/brokers/test_broker_failure_reasons_published.py``; this says
        the vocabulary is closed and spells it out, so neither side can be
        read back off the other.
        """
        self.assertEqual(
            report.TOTALS_REFUSALS,
            (
                "sim_reports_unusable",
                "before_first_sweep",
                "window_never_swept",
                "nothing_swept",
            ),
        )


class AStoreLevelFailureIsARefusalAndNotATraceback(_CashbookTotalsCase):
    """The two things the record raises that are not refusals of the WINDOW.

    ``build_totals`` raises :class:`CashBookIdentityError` on a published
    partition that does not hold -- deliberately, because a number beside a
    broken identity is read as the number -- and the journal reads raise
    ``OSError`` on a store root or a journal this process cannot read. Only
    ``CashBookTotalsRefusedError`` was handled, so either one escaped the
    command: with ``--format json`` the caller got a Python traceback and NO
    object with a code, which is the one thing "exactly one stderr line is a
    JSON object, and it is the last line" forbids.

    Both get a CODE rather than ``unclassified``. The repo rule is that
    ``unclassified`` marks a refusal not yet given a code and is never a branch
    condition -- and these two are known, named, and reached by different
    recoveries: an unreadable journal is fixed outside the process and the same
    command then answers, while a broken identity answers the same way for
    ever and needs somebody to look at the store. That is exactly the
    distinction a code exists to carry, and it is why ONE code for both would
    not do either: `retryable` is a property of the code, and these two differ
    in it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.seed()

    def break_the_bucket_identity(self):
        """``all_rows`` moved away from the buckets it must agree with.

        The observable shape of a row the classification lost, and the same
        doctoring ``test_report.py`` uses -- so the real ``build_totals``
        raises on its own rather than being replaced by a mock that raises.
        """
        real = buckets.classify_buckets

        def doctored(rows: object) -> buckets.BucketFold:
            fold = real(rows)  # type: ignore[arg-type]
            return replace(fold, all_rows=replace(fold.all_rows, value=1.0))

        return mock.patch.object(buckets, "classify_buckets", doctored)

    def make_a_journal_unreadable(self) -> Path:
        path = paths.cash_events_path(_LIVE)
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)
        return path

    def test_a_broken_identity_is_one_json_object_on_stderr(self) -> None:
        with self.break_the_bucket_identity():
            result = self.totals("--format", "json")

        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "cashbook_identity_broken")
        self.assertEqual(failure["details"], {"identity": "bucket"})
        self.assertFalse(failure["retryable"])
        self.assertEqual(result.exit_code, _EXIT_FAILED)

    def test_a_broken_identity_renders_as_prose_when_asked_for_prose(self) -> None:
        """And no total is published either way, which is what the identity is
        for: stdout is EMPTY, and the prose names the partition and both sides
        of the comparison that failed.

        The two sides belong in the message: they are the diagnostic, and a
        refusal that said only "an identity broke" would send an operator to
        read four partitions by hand.
        """
        with self.break_the_bucket_identity():
            result = self.totals()

        self.assertEqual(result.stdout, "")
        self.assertIn("the bucket identity does not hold", result.stderr)
        self.assertIn("no total is published", result.stderr)

    def test_an_unreadable_journal_is_one_json_object_on_stderr(self) -> None:
        path = self.make_a_journal_unreadable()

        result = self.totals("--format", "json")

        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "cashbook_store_unreadable")
        self.assertEqual(failure["details"], {"journal": str(path)})
        self.assertTrue(failure["retryable"])
        self.assertEqual(result.exit_code, _EXIT_TRANSIENT)

    def test_the_premise_is_that_both_shapes_really_reach_the_command(self) -> None:
        """Positive control: with the store readable and the identity holding,
        this same window answers -- so each test above measures its own cause
        and not a store that was broken all along."""
        self.assertEqual(self.totals().exit_code, 0)


class TheTermNamingHelperReadsAsASentenceAtEveryLength(unittest.TestCase):
    """The list-to-prose helper, at the lengths the render cannot reach today.

    Four terms is what the record publishes, so the rendered note only ever
    exercises that one branch. The shorter ones are reachable the moment
    ``RESIDUAL_TERMS`` changes, and replacing their branch with a raise left
    the whole CLI module green -- an unreached branch in the one helper whose
    output is a published sentence.
    """

    def test_no_terms_name_nothing(self) -> None:
        self.assertEqual(cashbook._named_terms([]), cashbook._joined([]))

    def test_one_term_stands_alone_without_a_conjunction(self) -> None:
        self.assertEqual(cashbook._named_terms(["net_closed_acct"]), "net_closed_acct")

    def test_two_terms_are_joined_by_and_with_no_comma(self) -> None:
        self.assertEqual(
            cashbook._named_terms(["net_closed_acct", "income_acct"]),
            "net_closed_acct and income_acct",
        )

    def test_more_than_two_take_commas_and_one_and(self) -> None:
        self.assertEqual(
            cashbook._named_terms(["a_acct", "b_acct", "c_acct"]),
            "a_acct, b_acct and c_acct",
        )

    def test_every_name_stays_bounded_so_a_reader_finds_it_whole(self) -> None:
        """The property the helper's docstring claims, at every length.

        A reader looking for one line's name must not find it inside another
        word, which is what a bare concatenation would produce.
        """
        for terms in ([], ["a_acct"], ["a_acct", "b_acct"], ["a_acct", "b_acct", "c_acct"]):
            with self.subTest(terms=len(terms)):
                rendered = f" {cashbook._named_terms(terms)} "
                for term in terms:
                    self.assertTrue(
                        f" {term} " in rendered or f" {term}," in rendered,
                        f"{term!r} is not bounded in {rendered!r}",
                    )


class ThePublishedRecoveryIsOneTheCallerCanFollow(_CashbookTotalsCase):
    """Each refusal's README row, against the object the command really publishes.

    The ``before_first_sweep`` row used to tell an operator that ``alphalens
    cashbook totals --format json`` publishes ``coverage.history_begins``, "the
    earliest ``--since`` that is answerable". On a refusal stdout is EMPTY and
    ``details`` carried only ``reason`` and ``env``, so following that sentence
    reproduced the same refusal and the watermark was reachable only by
    hand-reading ``sweeps.jsonl``. A recovery that cannot be run is worse than
    none: it reads as a way out.

    It is not a ``suggestions`` argv because the registry holds those PER CODE
    (``_SUGGESTIONS_BY_CODE``, so a code promising a way out does not depend on
    each emitter remembering one) and they are therefore static -- they cannot
    name this store's own date. The watermark is a fact about the refusal, so it
    rides in ``details``.

    Pinned BOTH ways, per reason: the keys beyond ``reason`` and ``env`` that
    the published object carries are exactly the ``details.<key>`` names its row
    promises. A key the table does not mention and a row promising a key the
    code never sends are each red.
    """

    def details_of(self, *extra: str, **window: str) -> dict:
        result = self.totals("--format", "json", *extra, **window)

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "cashbook_totals_refused")
        return failure["details"]

    def assert_row_names_the_keys(self, reason: str, details: dict) -> None:
        self.assertEqual(details["reason"], reason)
        self.assertEqual(
            set(details) - _REFUSAL_KEYS_EVERY_ROW_CARRIES,
            _named_details_keys(_totals_reason_rows()[reason]),
        )

    def test_the_parse_found_every_published_reason(self) -> None:
        """Positive control: a slice that matched nothing, or a header row read
        as a reason, would make every comparison below vacuous."""
        self.assertEqual(set(_totals_reason_rows()), set(report.TOTALS_REFUSALS))

    def test_the_parse_can_see_a_named_details_key(self) -> None:
        """The second positive control: a key parse that always answered the
        empty set would agree with a refusal that publishes nothing extra."""
        rows = _totals_reason_rows()

        self.assertEqual(_named_details_keys(rows["before_first_sweep"]), {"history_begins"})
        self.assertEqual(_named_details_keys(rows["sim_reports_unusable"]), set())

    def test_a_since_before_the_first_sweep_publishes_the_earliest_answerable_one(self) -> None:
        self.seed()

        details = self.details_of(since="2026-07-01")

        self.assertEqual(details["history_begins"], _SWEPT_SINCE)
        self.assert_row_names_the_keys("before_first_sweep", details)

    def test_a_window_outside_the_swept_range_publishes_both_watermarks(self) -> None:
        self.seed()

        details = self.details_of(since="2026-11-01", until="2026-11-30")

        self.assertEqual(details["history_begins"], _SWEPT_SINCE)
        self.assertEqual(details["swept_through"], _SWEPT_UNTIL)
        self.assert_row_names_the_keys("window_never_swept", details)

    def test_a_sim_refusal_promises_no_watermark_and_publishes_none(self) -> None:
        """The other direction of the same pin, on a reason whose recovery is
        not a window: there is no answerable window on SIM at all."""
        self.seed()

        self.assert_row_names_the_keys("sim_reports_unusable", self.details_of(env="sim"))

    def test_an_unswept_store_promises_no_watermark_and_publishes_none(self) -> None:
        """And on the one reason that HAS no watermark to publish: nothing was
        ever swept, so there is no date the store could offer."""
        self.assert_row_names_the_keys("nothing_swept", self.details_of())


class TheCommandHoldsNoBroker(unittest.TestCase):
    """``totals`` answers offline, off the local journals.

    **Mutation it catches:** a vendor read grown into a command that must
    answer with no network and no credentials -- which is also what keeps the
    places-nothing gate with nothing to find here.
    """

    def setUp(self) -> None:
        tree = ast.parse(_CASHBOOK_SOURCE.read_text(encoding="utf-8"))
        self.bodies = {
            node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }

    def test_the_scan_found_both_commands(self) -> None:
        """Positive control: a renamed function would make the assertion below
        vacuous rather than red."""
        self.assertIn("sync_command", self.bodies)
        self.assertIn("totals_command", self.bodies)

    def test_the_sync_command_does_hold_one(self) -> None:
        """The other direction: the scan can see a broker where there is one,
        so its silence on ``totals`` is a measurement."""
        self.assertIn("_cli_broker", self._names_called("sync_command"))

    def test_the_totals_command_calls_no_broker_builder(self) -> None:
        self.assertNotIn("_cli_broker", self._names_called("totals_command"))

    def _names_called(self, function: str) -> set[str]:
        return {
            node.func.id
            for node in ast.walk(self.bodies[function])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }


if __name__ == "__main__":
    unittest.main()
