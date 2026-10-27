"""Every `details.reason` the arming door can emit is in the published table.

`intent_malformed` and `pick_not_writable` publish a closed reason vocabulary in
`apps/alphalens-broker-contract/README.md`, and a client branches on it. Nothing
tied that table to the code before #1468, so a new reason could be emitted while
the catalogue denied it, or a retired one could stay published forever.

Emitted reasons come from two places: literal first arguments of
`_intent_malformed(...)` in the CLI, and the `reason` of each refusal class in
`intent_door` (mapped to its code the way `_refusal_to_exit` maps it).

`cashbook_sync_refused` (#1689) publishes a reason vocabulary of the same shape
under its own `####` heading, and that section was outside every gate in both
directions — a renamed row stayed green. It is checked here against the two
constants the two refusal paths carry, which closes the direction that cost
nothing to break. What it does NOT close: there is no runtime registry of those
reasons to compare against, because a frozenset with no production consumer is
dead code the deadcode report would report, so a THIRD reason added to the code
has to be added to this test the way `CLI_OWNED_CODES` is maintained by hand in
`test_broker_contract_has_no_cli_codes.py`.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from alphalens_pipeline.brokers.automanager import intent_door

REPO_ROOT = Path(__file__).resolve().parents[4]
README = REPO_ROOT / "apps" / "alphalens-broker-contract" / "README.md"
BROKER_CLI = REPO_ROOT / "apps" / "alphalens-pipeline" / "alphalens_cli" / "commands" / "broker.py"

_SECTION_START = "#### `intent_malformed` and `pick_not_writable` publish a `reason` too"
_CASHBOOK_SECTION_START = "#### `cashbook_sync_refused` publishes a `reason` too"
_TOTALS_SECTION_START = "#### `cashbook_totals_refused` publishes a `reason` too"
_ROW_RE = re.compile(r"^\|\s*(?:`([a-z_]+)`)?\s*\|\s*`([a-z_]+)`\s*\|", re.MULTILINE)
_NOT_WRITABLE = intent_door.NOT_WRITABLE


def published_reasons(section_start: str = _SECTION_START) -> dict[str, set[str]]:
    text = README.read_text(encoding="utf-8")
    section = text[text.index(section_start) :]
    section = section[: section.index("\n####", len(section_start))]
    reasons: dict[str, set[str]] = {}
    code = ""
    for named_code, reason in _ROW_RE.findall(section):
        code = named_code or code
        reasons.setdefault(code, set()).add(reason)
    return reasons


def emitted_reasons() -> dict[str, set[str]]:
    reasons: dict[str, set[str]] = {"intent_malformed": set(), "pick_not_writable": set()}
    tree = ast.parse(BROKER_CLI.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_intent_malformed"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            reasons["intent_malformed"].add(node.args[0].value)
    for refusal in intent_door.REFUSALS:
        if refusal.reason is None:
            continue
        code = "pick_not_writable" if issubclass(refusal, _NOT_WRITABLE) else "intent_malformed"
        reasons[code].add(refusal.reason)
    return reasons


def cashbook_emitted_reasons() -> dict[str, set[str]]:
    """The reasons the sweep's two refusal paths carry.

    Read off the constants rather than off a literal in the CLI: the command
    publishes ``plan.refused_reason`` and ``SimCashBookRefusedError.reason``,
    so neither reason is spelled at the call site. One belongs to the READ (the
    vendor's SIM reports are other accounts' canned rows) and one to the WRITE
    (the store's guard refuses a non-live environment).
    """
    from alphalens_pipeline.account_cash.store import SIM_STORE_REFUSED
    from alphalens_pipeline.brokers.account_cash import SIM_REPORTS_UNUSABLE

    return {"cashbook_sync_refused": {SIM_REPORTS_UNUSABLE, SIM_STORE_REFUSED}}


def cashbook_totals_emitted_reasons() -> dict[str, set[str]]:
    """The reasons the totals command can publish, off the report's own tuple.

    A SECOND function rather than a third entry in the one above, because
    :func:`published_reasons` slices from its section heading to the next
    ``####`` -- one call can never return two sections, so one comparison can
    never cover two. Read off ``TOTALS_REFUSALS`` because the command publishes
    ``CashBookTotalsRefusedError.reason`` and spells no reason at its own call
    site; a literal here would agree with a renamed constant.
    """
    from alphalens_pipeline.account_cash.report import TOTALS_REFUSALS

    return {"cashbook_totals_refused": set(TOTALS_REFUSALS)}


class TheReasonTableAndTheCodeAgree(unittest.TestCase):
    def test_the_table_was_parsed_at_all(self) -> None:
        """Positive control: an empty parse would make the comparison vacuous."""
        published = published_reasons()
        self.assertIn("not_json", published.get("intent_malformed", set()))
        self.assertIn("already_placed", published.get("pick_not_writable", set()))

    def test_the_code_scan_found_the_literal_sites(self) -> None:
        self.assertIn("duplicate_key", emitted_reasons()["intent_malformed"])

    def test_every_emitted_reason_is_published_and_nothing_else_is(self) -> None:
        self.assertEqual(published_reasons(), emitted_reasons())

    def test_the_cashbook_table_was_parsed_at_all(self) -> None:
        """Positive control for the second section: it sits under its own
        ``####`` heading, deliberately outside the slice above, so an unparsed
        heading would make the comparison below vacuous rather than red."""
        published = published_reasons(_CASHBOOK_SECTION_START)

        self.assertEqual(set(published), {"cashbook_sync_refused"})
        self.assertEqual(len(published["cashbook_sync_refused"]), 2)

    def test_every_cashbook_reason_is_published_and_nothing_else_is(self) -> None:
        self.assertEqual(published_reasons(_CASHBOOK_SECTION_START), cashbook_emitted_reasons())

    def test_the_totals_table_was_parsed_at_all(self) -> None:
        """Positive control for the third section, which sits under its own
        ``####`` heading and is therefore outside both slices above."""
        published = published_reasons(_TOTALS_SECTION_START)

        self.assertEqual(set(published), {"cashbook_totals_refused"})
        self.assertEqual(len(published["cashbook_totals_refused"]), 4)

    def test_every_totals_reason_is_published_and_nothing_else_is(self) -> None:
        self.assertEqual(
            published_reasons(_TOTALS_SECTION_START), cashbook_totals_emitted_reasons()
        )


if __name__ == "__main__":
    unittest.main()
