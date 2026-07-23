"""Mutation-hardening pins for ``brokers/submission_log.py``.

Kills the KILLABLE mutation survivors flagged for this module. Each test
states the guarantee it pins in its name; the docstring names the exact
mutant it fails under so the pin cannot rot into a vacuous assert.

Survivor coverage (survivor id -> test group):

- id 77 (L95, ``precheck or []``) -> ``PrecheckDefaultTest``
- id 90 (L115, ``mkdir(parents=True)``) -> ``AppendParentChainTest``
- id 91 (L116, ``json.dumps(sort_keys=True)``) -> ``AppendSortKeysTest``

A hand-written anchor pins each identity / exact-value kill; a hypothesis
sweep SUPPLEMENTS the id-77 truthy-passthrough (never replaces the anchors).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from alphalens_pipeline.brokers.submission_log import (
    append_submission_record,
    build_submission_record,
)
from hypothesis import given, settings
from hypothesis import strategies as st


def _record(**overrides: Any) -> dict[str, Any]:
    """Minimal valid submission record, with per-test overrides."""
    kwargs: dict[str, Any] = {
        "brief_date": "2026-07-22",
        "ticker": "KO",
        "mic": "XNYS",
        "uic": "307",
        "brackets": [],
    }
    kwargs.update(overrides)
    return build_submission_record(**kwargs)


class PrecheckDefaultTest(unittest.TestCase):
    """id 77 (L95): ``precheck or []`` — truthy passes through, None -> []."""

    def test_non_empty_precheck_passes_through_the_same_object(self) -> None:
        """Mutant ``precheck and []`` returns ``[]`` for a truthy list."""
        precheck = [{"ok": True}]
        record = _record(precheck=precheck)
        # ``or`` returns the left operand unchanged when truthy (same object);
        # ``and`` would return the right operand ``[]``.
        self.assertIs(record["precheck"], precheck)

    def test_none_precheck_becomes_empty_list(self) -> None:
        """Mutant ``precheck and []`` returns ``None`` when precheck is None."""
        record = _record(precheck=None)
        self.assertEqual(record["precheck"], [])
        self.assertIsNotNone(record["precheck"])

    @settings(deadline=None, max_examples=200)
    @given(
        precheck=st.lists(
            st.fixed_dictionaries({"ok": st.booleans()}),
            min_size=1,
            max_size=6,
        )
    )
    def test_any_non_empty_precheck_is_preserved_verbatim(
        self, precheck: list[dict[str, Any]]
    ) -> None:
        """Supplement: every truthy list survives; mutant ``and`` yields []."""
        record = _record(precheck=precheck)
        self.assertEqual(record["precheck"], precheck)


class AppendParentChainTest(unittest.TestCase):
    """id 90 (L115): ``mkdir(parents=True)`` builds the full parent chain."""

    def test_missing_grandparent_dirs_are_created(self) -> None:
        """Mutant ``parents=False`` raises FileNotFoundError on a missing chain."""
        with tempfile.TemporaryDirectory() as tmp:
            # None of ``a/``, ``a/b/`` or ``a/b/c/`` exist yet — only the
            # recursive parent creation of ``parents=True`` can place the file.
            target = Path(tmp) / "a" / "b" / "c" / "submissions.jsonl"
            self.assertFalse(target.parent.exists())

            returned = append_submission_record(_record(), path=target)

            self.assertEqual(returned, target)
            self.assertTrue(target.exists())


class AppendSortKeysTest(unittest.TestCase):
    """id 91 (L116): ``json.dumps(sort_keys=True)`` — alphabetical key order."""

    def _write_and_read_line(self, record: dict[str, Any]) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "submissions.jsonl"
            append_submission_record(record, path=target)
            return target.read_text(encoding="utf-8").strip()

    def test_written_line_starts_with_alphabetically_first_key(self) -> None:
        """Mutant ``sort_keys=False`` starts with insertion-order key instead.

        Insertion order starts with ``execution_config_version``; the
        alphabetically-first key is ``brackets``, so the serialized prefix
        differs between the two branches.
        """
        record = _record()
        line = self._write_and_read_line(record)
        self.assertTrue(
            line.startswith('{"brackets"'),
            msg=f"expected sorted-key prefix, got: {line[:40]!r}",
        )

    def test_written_line_equals_sorted_json_dump(self) -> None:
        """Mutant ``sort_keys=False`` produces the insertion-order dump."""
        record = _record()
        line = self._write_and_read_line(record)
        self.assertEqual(line, json.dumps(record, sort_keys=True, default=str))

    def test_insertion_order_dump_is_not_what_gets_written(self) -> None:
        """Guards that the two orderings genuinely differ (non-vacuous pin)."""
        record = _record()
        line = self._write_and_read_line(record)
        insertion_order_dump = json.dumps(record, sort_keys=False, default=str)
        self.assertNotEqual(line, insertion_order_dump)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
