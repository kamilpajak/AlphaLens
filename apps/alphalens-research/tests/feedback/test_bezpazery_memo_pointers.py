"""The pre-registration ledger must point at text that exists (#1114).

``edge_hypothesis_budget_2026_07.md`` is the auditable artifact for the ADR 0013
R4 multiplicity budget. Its row for a registered lens carries the ONLY pointer
into the pre-registration text for that lens, so a pointer at a section that was
never written sends the September walk-forward auditor to an empty file. These
tests read both documents and check the pointer resolves.

The second guard is about a claim rather than a pointer: the amendment must
record that the two anchors do NOT share a per-row cohort. The earlier prose said
they did (the shared no-fill gate was generalized into "the SAME cohort"), which
is refuted by ``TestTheAnchorAlsoMovesTheCohort`` in
``tests/feedback/test_atr_bracket_anchor_mode.py``.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_MEMO = _REPO_ROOT / "docs/research/bezpazery_lens_design_2026_07_16.md"
_LEDGER = _REPO_ROOT / "docs/research/edge_hypothesis_budget_2026_07.md"
_MEMO_FILENAME = "bezpazery_lens_design_2026_07_16.md"

# "amendment §7 of `bezpazery_lens_design_2026_07_16.md`" -> "7"
_POINTER_RE = re.compile(r"amendment\s+§\s*(\d+(?:\.\d+)*)\s+of\s+`?" + re.escape(_MEMO_FILENAME))
_HEADING_RE = re.compile(r"^(#{2,4})\s+(\d+(?:\.\d+)*)\.?\s", re.MULTILINE)

# The regex above names ONE memo, so a row pointing at any other memo was
# unchecked — which is how the #1227 amendment (2026-09-21) would have shipped
# with a pointer nobody verified. This second form is memo-agnostic and reads
# the other word order the ledger actually uses:
#   "`ml_label_registry_design_2026_09_16.md` §8 step 3" -> (memo, "8")
_ANY_MEMO_POINTER_RE = re.compile(r"`([a-z0-9_]+\.md)`\s+§\s*(\d+(?:\.\d+)*)")


def _memo_section_numbers(memo: Path | None = None) -> set[str]:
    text = (memo or _MEMO).read_text(encoding="utf-8")
    return {match.group(2) for match in _HEADING_RE.finditer(text)}


class TestTheLedgerPointsAtRealMemoSections(unittest.TestCase):
    def test_every_amendment_pointer_names_a_section_the_memo_has(self):
        pointers = _POINTER_RE.findall(_LEDGER.read_text(encoding="utf-8"))
        self.assertTrue(pointers, "positive control: the ledger carries an amendment pointer")
        sections = _memo_section_numbers()
        self.assertTrue(sections, "positive control: the memo has numbered headings")
        for pointer in pointers:
            self.assertIn(
                pointer,
                sections,
                f"ledger points at section {pointer} of {_MEMO_FILENAME}, which has no such heading",
            )

    def test_the_positive_control_catches_a_pointer_at_a_missing_section(self):
        # Anti-rot: a regex that silently stopped matching would make the guard
        # above vacuous, so exercise the failure arm on a synthetic pointer.
        sections = _memo_section_numbers()
        self.assertNotIn("99", sections)
        synthetic = f"amendment §99 of `{_MEMO_FILENAME}`"
        self.assertEqual(_POINTER_RE.findall(synthetic), ["99"])


class TestEveryMemoPointerResolves(unittest.TestCase):
    """The same guarantee as above, for every memo the ledger cites.

    The single-memo regex made a pointer at any other design memo silently
    unchecked. A ledger row is the only route from the budget to the text that
    justifies a registration, so a §N that does not exist sends a later auditor
    to nothing — regardless of which memo it names.
    """

    def test_each_cited_section_exists_in_the_memo_it_names(self):
        pairs = _ANY_MEMO_POINTER_RE.findall(_LEDGER.read_text(encoding="utf-8"))
        self.assertTrue(pairs, "positive control: the ledger cites at least one memo section")
        for filename, section in pairs:
            memo = _REPO_ROOT / "docs/research" / filename
            with self.subTest(memo=filename, section=section):
                self.assertTrue(
                    memo.exists(), f"ledger cites {filename}, which is not in docs/research"
                )
                self.assertIn(
                    section,
                    _memo_section_numbers(memo),
                    f"ledger points at §{section} of {filename}, which has no such heading",
                )

    def test_the_memo_agnostic_regex_can_refute(self):
        # Anti-rot on the new regex itself, matching the older guard's shape.
        synthetic = "`some_memo_2026_01_01.md` §99 step 1"
        self.assertEqual(
            _ANY_MEMO_POINTER_RE.findall(synthetic), [("some_memo_2026_01_01.md", "99")]
        )


class TestTheAmendmentRecordsTheCohortCaveat(unittest.TestCase):
    def test_the_memo_carries_a_subsection_naming_the_cohort(self):
        headings = [
            line
            for line in _MEMO.read_text(encoding="utf-8").splitlines()
            if line.startswith("#") and "cohort" in line.lower()
        ]
        self.assertTrue(
            headings,
            "the amendment must carry a subsection about the per-row cohort: the two "
            "anchors share the no-fill gate but not the constructibility gates",
        )
