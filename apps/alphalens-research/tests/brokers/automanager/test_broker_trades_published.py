"""The ``broker trades`` vocabularies as published in the contract README (#1701).

Memo §3.0 and §8: every ``source``, ``null_reason``, exit ``reason``,
``attribution``, ``state``, ``state_reason`` and warning code is published in
``apps/alphalens-broker-contract/README.md`` ("broker trades"), and the exit
reasons carry a mapping to the daemon's ``trade_alerts.ExitReason``. Each table
is checked in both directions, the way ``test_broker_failure_codes_published``
checks the failure codes: a value the builder emits with no row is red, and a
row naming a value the builder does not have is red.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from alphalens_pipeline.brokers.automanager import trade_alerts, trades

README = Path(__file__).resolve().parents[5] / "apps" / "alphalens-broker-contract" / "README.md"

_SECTION_RE = re.compile(r"^## `alphalens broker trades`.*?(?=^## )", re.MULTILINE | re.DOTALL)
_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|(.*)$", re.MULTILINE)


def _section() -> str:
    text = README.read_text(encoding="utf-8")
    match = _SECTION_RE.search(text + "\n## end\n")
    if match is None:
        raise AssertionError("README has no '## `alphalens broker trades`' section")
    return match.group(0)


def _table_after(heading: str) -> dict[str, str]:
    """Rows of the first table under ``#### <heading>``: first cell -> rest."""
    section = _section()
    start = section.find(f"#### {heading}\n")
    if start < 0:
        raise AssertionError(f"no '#### {heading}' in the broker trades section")
    body = section[start + len(heading) + 6 :]
    end = body.find("\n#### ")
    block = body if end < 0 else body[:end]
    return {m.group(1): m.group(2) for m in _ROW_RE.finditer(block)}


class EveryVocabularyIsPublished(unittest.TestCase):
    def test_each_table_matches_the_builder(self) -> None:
        cases = {
            "Sources": trades.SOURCES,
            "Null reasons": trades.NULL_REASONS,
            "Exit reasons": trades.EXIT_REASONS,
            "Attribution": trades.ATTRIBUTIONS,
            "States": trades.STATES,
            "State reasons": trades.STATE_REASONS,
            "Warnings": trades.WARNING_CODES,
            "Replay exclusions": trades.REPLAY_EXCLUSIONS,
        }
        for heading, vocabulary in cases.items():
            with self.subTest(table=heading):
                published = _table_after(heading)
                self.assertEqual(sorted(published), sorted(vocabulary))

    def test_every_alert_reason_has_a_row_in_the_mapping(self) -> None:
        rows = _table_after("Exit reasons")
        for member in trade_alerts.ExitReason:
            mapped = trades.EXIT_REASON_BY_ALERT_REASON[member.name]
            with self.subTest(alert=member.name):
                self.assertIsNotNone(mapped)
                self.assertIn(f"`{member.name}`", rows[str(mapped)])

    def test_the_schema_file_and_the_consumer_example_are_named(self) -> None:
        section = _section()
        self.assertIn("docs/broker-trades-v1.schema.json", section)
        self.assertIn("alphalens broker trades --env live --state all --all --format json", section)

    def test_the_parser_can_say_no(self) -> None:
        # Positive control: a table that misses a value must fail the equality
        # above, so the parser must return real rows, not an empty dict.
        self.assertGreaterEqual(len(_table_after("Null reasons")), len(trades.NULL_REASONS))


if __name__ == "__main__":
    unittest.main()
