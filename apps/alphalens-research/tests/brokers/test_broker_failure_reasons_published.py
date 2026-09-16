"""Every `details.reason` the arming door can emit is in the published table.

`intent_malformed` and `pick_not_writable` publish a closed reason vocabulary in
`apps/alphalens-broker-contract/README.md`, and a client branches on it. Nothing
tied that table to the code before #1468, so a new reason could be emitted while
the catalogue denied it, or a retired one could stay published forever.

Emitted reasons come from two places: literal first arguments of
`_intent_malformed(...)` in the CLI, and the `reason` of each refusal class in
`intent_door` (mapped to its code the way `_refusal_to_exit` maps it).
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
_ROW_RE = re.compile(r"^\|\s*(?:`([a-z_]+)`)?\s*\|\s*`([a-z_]+)`\s*\|", re.MULTILINE)
_NOT_WRITABLE = (intent_door.GenerationSpentError, intent_door.AlreadyPlacedError)


def published_reasons() -> dict[str, set[str]]:
    text = README.read_text(encoding="utf-8")
    section = text[text.index(_SECTION_START) :]
    section = section[: section.index("\n####", len(_SECTION_START))]
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


if __name__ == "__main__":
    unittest.main()
