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

from alphalens_pipeline.account_cash import money, report, sweep
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

_EXIT_FAILED = 1
_EXIT_USAGE = 2

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

#: The labels of the eight published money lines, in render order, keyed by
#: the body field each one renders.
_MONEY_LINES: tuple[tuple[str, str], ...] = (
    ("net_closed_acct", "net on closed episodes"),
    ("cost_basis_open_acct", "cost basis of open episodes"),
    ("income_acct", "dividends and withholding tax"),
    ("unclassified_acct", "unclassified cash events"),
    ("residual_acct", "residual not explained above"),
    ("subtotal_acct", "trading and income subtotal"),
    ("deposits_acct", "deposits and withdrawals"),
    ("cash_book_sum_acct", "cash book sum"),
)
_MONEY_LABELS = tuple(label for _key, label in _MONEY_LINES)
_MONEY_LABEL_BY_FIELD = dict(_MONEY_LINES)
_NET_LABEL = _MONEY_LINES[0][1]
_COST_BASIS_LABEL = _MONEY_LINES[1][1]
_UNCLASSIFIED_LABEL = _MONEY_LINES[3][1]
_RESIDUAL_LABEL = _MONEY_LINES[4][1]
#: The one DERIVED money line: the five lines above it add up to it, which is
#: what makes it the line a CLI could recompute instead of rendering.
_SUBTOTAL_LINE = _MONEY_LINES[5]
_SUBTOTAL_LABEL = _SUBTOTAL_LINE[1]
_DEPOSITS_LABEL = _MONEY_LINES[6][1]
_CASH_BOOK_SUM_LABEL = _MONEY_LINES[7][1]

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
        """Measured at the 2026-09-12 cut, where it is -4334.10 PLN: at the
        full span it is exactly 0.00, so a renderer that dropped the line
        would be indistinguishable from one that printed it."""
        human = self.totals(until="2026-09-12").stdout
        lines = [line for line in human.splitlines() if line.strip()]

        line = lines[_line_index(lines, _RESIDUAL_LABEL)]

        self.assertIn("-4334.10 PLN", line)

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
        self.assertEqual(failure["details"], {"reason": "before_first_sweep", "env": _LIVE})

    def test_an_unswept_store_refuses_nothing_swept(self) -> None:
        result = self.totals("--format", "json")

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "cashbook_totals_refused")
        self.assertEqual(failure["details"], {"reason": "nothing_swept", "env": _LIVE})

    def test_a_refusal_renders_as_prose_when_the_caller_asked_for_prose(self) -> None:
        result = self.totals()

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(result.stdout, "")
        self.assertIn("nothing_swept", result.stderr)

    def test_the_published_reasons_are_the_reports_own_vocabulary(self) -> None:
        """**Mutation it catches:** a fourth reason added to the report and
        left unpublished, or one of these three renamed on one side only. The
        table in the contract README is checked against this same tuple by
        ``tests/brokers/test_broker_failure_reasons_published.py``; this says
        the vocabulary is closed and spells it out, so neither side can be
        read back off the other.
        """
        self.assertEqual(
            report.TOTALS_REFUSALS,
            ("sim_reports_unusable", "before_first_sweep", "nothing_swept"),
        )


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
