"""CI guard: the EDGAR fundamentals validation gate memo must exist and PASS.

The gate itself is operator-triggered (live SEC + yfinance fetches) — running
it on every CI invocation would burn quota and flake. Instead this test
asserts that the committed memo is present, the verdict is ``PASS``, and
every required anchor was checked. Pins the *EDGAR* migration specifically;
a future migration to a different vendor MUST delete this file and write a
new vendor-specific evidence guard rather than relying on this stale memo.

Refresh procedure: re-run ``scripts/edgar_fundamentals_validation_gate.py``
and commit the regenerated memo at ``docs/research/edgar_fundamentals_validation_2026_05_19.md``.
"""

from __future__ import annotations

import re
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MEMO = REPO_ROOT / "docs" / "research" / "edgar_fundamentals_validation_2026_05_19.md"

# Anchors the gate run must cover. Matches DEFAULT_ANCHORS in the harness.
REQUIRED_ANCHORS = ("MANH", "SYM", "JPM", "CAT", "UNH")

# The gate was first run on 2026-05-20. Older memos must not satisfy the
# guard — forces a refresh if SimFin / EDGAR / concept_chains.py change in
# a way that could invalidate the prior result.
MIN_GATE_DATE = date(2026, 5, 20)


class TestEdgarMigrationEvidence(unittest.TestCase):
    """EDGAR-specific evidence guard. Named to make it obvious that this test
    pins the EDGAR migration; a future vendor migration (Alpha Vantage,
    Polygon, …) cannot reuse this memo / test class — operator MUST delete
    this file and write a new vendor-specific one for that migration's gate
    run. Static date floor below isn't sufficient on its own to enforce that.
    """

    def setUp(self):
        self.assertTrue(MEMO.exists(), f"gate memo missing at {MEMO}")
        self.text = MEMO.read_text(encoding="utf-8")

    def test_verdict_is_pass(self):
        self.assertIn("**Gate verdict:** PASS", self.text)

    def test_all_required_anchors_present(self):
        for anchor in REQUIRED_ANCHORS:
            # assertRegex doesn't take a flags arg; do the match manually so
            # ^ anchors to line start under MULTILINE.
            self.assertIsNotNone(
                re.search(rf"^## {anchor} —", self.text, re.MULTILINE),
                f"anchor {anchor} missing from gate memo section header",
            )

    def test_gate_date_is_recent(self):
        # Header format: "# EDGAR fundamentals validation gate — YYYY-MM-DD"
        m = re.search(
            r"^#\s+EDGAR fundamentals validation gate\s+—\s+(\d{4}-\d{2}-\d{2})",
            self.text,
            re.MULTILINE,
        )
        self.assertIsNotNone(m, "could not parse gate date from memo header")
        memo_date = date.fromisoformat(m.group(1))
        self.assertGreaterEqual(
            memo_date,
            MIN_GATE_DATE,
            f"gate memo dated {memo_date}, must be on or after {MIN_GATE_DATE}",
        )


if __name__ == "__main__":
    unittest.main()
