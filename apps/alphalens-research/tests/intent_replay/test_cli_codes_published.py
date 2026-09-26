"""Every code `intent-replay` can answer with is in the package README, owned
correctly, and every reason of the two CLI-owned codes has a row.

The precedent is `tests/brokers/test_broker_failure_codes_published.py`: a
registered code missing from the table is unpublished, a row naming no
registered code is a stale promise, and an owner column disagreeing with the
registry sends a reader to the wrong half of the split.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from intent_replay.cli import (
    CONFIG_MALFORMED_REASONS,
    FAILURE_CODES,
    INTENT_MALFORMED_REASONS,
    OWNERS,
)

README = Path(__file__).resolve().parents[4] / "apps" / "intent-replay" / "README.md"

# `| `code` | owner | ... ` — the owner column is the second cell.
_CODE_ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|\s*(engine|contract|CLI)\s*\|", re.MULTILINE)
# `| `code` | `reason` | ... ` or `| | `reason` | ...` (a continuation row).
_REASON_ROW = re.compile(r"^\|\s*(?:`([a-z_]+)`)?\s*\|\s*`([a-z_]+)`\s*\|", re.MULTILINE)


def _text() -> str:
    return README.read_text(encoding="utf-8")


def published_codes() -> dict[str, str]:
    return dict(_CODE_ROW.findall(_text()))


def published_reasons() -> dict[str, set[str]]:
    reasons: dict[str, set[str]] = {}
    current = ""
    for code, reason in _REASON_ROW.findall(_text()):
        current = code or current
        reasons.setdefault(current, set()).add(reason)
    return reasons


class TheCodeTableAndTheRegistryAgree(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = published_codes()

    def test_the_table_was_parsed_at_all(self) -> None:
        self.assertGreaterEqual(len(self.rows), len(FAILURE_CODES))

    def test_every_registered_code_is_published(self) -> None:
        self.assertEqual(sorted(set(FAILURE_CODES) - set(self.rows)), [])

    def test_every_published_row_names_a_registered_code(self) -> None:
        self.assertEqual(sorted(set(self.rows) - set(FAILURE_CODES)), [])

    def test_each_row_names_the_half_that_defines_it(self) -> None:
        for code, owner in sorted(self.rows.items()):
            with self.subTest(code=code):
                self.assertEqual(owner, OWNERS[code])


class TheReasonTablesAndTheVocabulariesAgree(unittest.TestCase):
    def test_intent_malformed_reasons(self) -> None:
        self.assertEqual(published_reasons()["intent_malformed"], set(INTENT_MALFORMED_REASONS))

    def test_config_malformed_reasons(self) -> None:
        self.assertEqual(published_reasons()["config_malformed"], set(CONFIG_MALFORMED_REASONS))


if __name__ == "__main__":
    unittest.main()
