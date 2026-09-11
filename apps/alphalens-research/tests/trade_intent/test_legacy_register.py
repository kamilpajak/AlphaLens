"""Legacy allowances are marked, and each names the observation that retires it.

A shim outlives its reason quietly. The `brief_date` key was renamed in #1252
and the codec still migrates it; `ceiling_price` has been refused since #1236
and the field is still on the dataclass. Both are correct today and both will
one day be dead weight that nobody dares remove, because nothing records what
would make removal safe.

So each allowance carries three things: what it is, why it is still here, and
the OBSERVATION that retires it — not a date. A date is a wish; an observation
is a measurement, and `RetirementCensusOnARealJournal` below runs it.

The code sites carry a marker naming the allowance, so deleting one is a grep
rather than an excavation. The gate is BIDIRECTIONAL: a marker with no entry is
red, and an entry with no marker is red. Without both directions the register
becomes a second place to forget.
"""

from __future__ import annotations

import json
import os
import re
import unittest
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

from broker_contract.trade_intent.legacy import (
    LEGACY_ALLOWANCES,
    MARKER_PREFIX,
    LegacyAllowance,
)

REPO_ROOT = Path(__file__).resolve().parents[4]

# The register itself is NOT a site: it is the table. Excluded so its own
# examples cannot masquerade as usages of the thing it describes.
REGISTER = (
    REPO_ROOT
    / "apps"
    / "alphalens-broker-contract"
    / "broker_contract"
    / "trade_intent"
    / "legacy.py"
)

SCAN_ROOTS = (
    REPO_ROOT / "apps" / "alphalens-broker-contract" / "broker_contract",
    REPO_ROOT / "apps" / "alphalens-pipeline" / "alphalens_pipeline",
    REPO_ROOT / "apps" / "alphalens-pipeline" / "alphalens_cli",
    REPO_ROOT / "apps" / "alphalens-research" / "tests",
)

# Built from MARKER_PREFIX rather than spelled out, so THIS file never contains
# a literal marker and can therefore host a negative control with a fake key.
_MARKER_RE = re.compile(re.escape(MARKER_PREFIX) + r"([a-z0-9_]+)\)")


def markers_under(*roots: Path) -> dict[str, list[Path]]:
    """Every allowance marker in the tree, mapped to the files carrying it."""
    found: dict[str, list[Path]] = {}
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if path == REGISTER:
                continue
            for key in _MARKER_RE.findall(path.read_text(encoding="utf-8")):
                found.setdefault(key, []).append(path)
    return found


class TheRegisterAndTheTreeAgree(unittest.TestCase):
    """Neither half may drift away from the other."""

    def setUp(self) -> None:
        self.found = markers_under(*SCAN_ROOTS)

    def test_every_registered_allowance_is_marked_somewhere(self) -> None:
        unmarked = sorted(set(LEGACY_ALLOWANCES) - set(self.found))
        self.assertEqual(
            unmarked,
            [],
            "registered but not marked in any source file — either the code is gone "
            "(drop the entry) or the marker is missing (add it)",
        )

    def test_every_marker_names_a_registered_allowance(self) -> None:
        unregistered = {
            key: [str(p.relative_to(REPO_ROOT)) for p in paths]
            for key, paths in self.found.items()
            if key not in LEGACY_ALLOWANCES
        }
        self.assertEqual(
            unregistered,
            {},
            "marked in the tree but absent from the register — a typo in the marker "
            "lands here, which is the point",
        )


class EveryEntrySaysEnoughToActOn(unittest.TestCase):
    def test_each_entry_states_what_why_and_the_retiring_observation(self) -> None:
        for key, allowance in LEGACY_ALLOWANCES.items():
            with self.subTest(allowance=key):
                self.assertTrue(allowance.what.strip(), key)
                self.assertTrue(allowance.why.strip(), key)
                self.assertTrue(allowance.retires_when.strip(), key)

    def test_each_entry_carries_a_runnable_predicate(self) -> None:
        """`retires_when` is prose; the predicate is what the census executes.

        Prose alone would make the observation unfalsifiable — the whole failure
        mode this register exists to avoid.
        """
        document = {"meta": {"trade_date": "2026-09-11"}, "exit": None}
        for key, allowance in LEGACY_ALLOWANCES.items():
            with self.subTest(allowance=key):
                self.assertIsInstance(allowance.still_needed(document), bool)

    def test_a_blank_observation_is_refused(self) -> None:
        """Otherwise the gate above is satisfied by an empty string."""
        with self.assertRaises(ValueError):
            LegacyAllowance(what="x", why="y", retires_when="   ", still_needed=bool)

    def test_a_blank_description_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            LegacyAllowance(what="  ", why="y", retires_when="z", still_needed=bool)

    def test_the_register_is_not_writable(self) -> None:
        with self.assertRaises(TypeError):
            LEGACY_ALLOWANCES["smuggled"] = None  # type: ignore[index]


class TheScanItselfCanFail(unittest.TestCase):
    """Positive control: a gate that cannot go red has tested nothing."""

    def _tree_with(self, text: str) -> Path:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "module.py").write_text(text, encoding="utf-8")
        return root

    def test_an_unregistered_marker_is_found(self) -> None:
        root = self._tree_with(f"x = 1  {MARKER_PREFIX}not_a_real_allowance)\n")
        self.assertEqual(sorted(markers_under(root)), ["not_a_real_allowance"])

    def test_a_file_with_no_marker_contributes_nothing(self) -> None:
        self.assertEqual(markers_under(self._tree_with("x = 1\n")), {})

    def test_a_real_key_is_found_where_it_is_marked(self) -> None:
        key = next(iter(LEGACY_ALLOWANCES))
        root = self._tree_with(f"y = 2  {MARKER_PREFIX}{key})\n")
        self.assertEqual(sorted(markers_under(root)), [key])


def _journal_documents(path: Path) -> Iterator[dict]:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        intent = record.get("intent") if isinstance(record, dict) else None
        if isinstance(intent, dict):
            yield intent


@unittest.skipUnless(
    os.environ.get("ALPHALENS_PICKS_JSONL"),
    "set ALPHALENS_PICKS_JSONL=<path> to census a real journal",
)
class RetirementCensusOnARealJournal(unittest.TestCase):
    """How many real documents still need each allowance — the deletion signal.

    Opt-in on a path, like the L4 vendor probes and #1405's journal check: CI has
    no journal, and a gate that silently skips when its data is missing would be
    a gate that disappears. Measured 2026-09-11 on the VPS journals (63 armed
    documents): brief_date_key 51, reanchor_ceiling_price 45.
    """

    def test_it_reports_what_each_allowance_still_carries(self) -> None:
        documents = list(_journal_documents(Path(os.environ["ALPHALENS_PICKS_JSONL"])))
        self.assertTrue(documents, "the journal carries no decodable intents")
        for key, allowance in LEGACY_ALLOWANCES.items():
            needing = [d for d in documents if allowance.still_needed(d)]
            print(
                f"  {key}: {len(needing)}/{len(documents)} documents still need it"
                f"  ({allowance.retires_when})"
            )


if __name__ == "__main__":
    unittest.main()
