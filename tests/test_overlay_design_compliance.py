"""CI gate: Layer 4 overlay design memos must run vol-regime pre-screen.

Per CLAUDE.md "Research methodology / Layer 4 overlay design pre-screen
(mandatory)" — any new overlay design memo in `docs/research/` must call
``alphalens.attribution.signal_vol_regime.classify_cyclicality()`` on the
base signal AND quote the verdict verbatim in the memo body. This test
fails CI if a future overlay memo lacks the pre-screen evidence.

Empirical justification: PR #88 (2026-05-10) caught a counter-cyclical
signal × pro-cyclical overlay structural mismatch that 4 review passes
(zen × 2, perplexity × 2) failed to surface up-front. The pre-screen
takes 5 minutes and produces a deterministic GO/NO-GO; the reviewer
debate without it took 4+ hours.

Pre-existing overlay docs (written before 2026-05-10) are grandfathered
via explicit allowlist. Adding a file to the allowlist requires that
the file actually exist (defends against silent allowlist rot).
"""

from __future__ import annotations

import unittest
from pathlib import Path

# Allowlist of overlay docs that pre-date the 2026-05-10 pre-screen rule.
# DO NOT ADD new entries here without explicit user instruction — the
# whole point of this test is to prevent the pre-screen step from being
# silently skipped on future overlay tests.
_GRANDFATHERED_PRE_2026_05_10 = frozenset(
    {
        "regime_overlay.md",
        "vol_target_overlay.md",
        "v10_drawdown_overlay_design_2026_05_04.md",
        "v10_drawdown_overlay_postmortem_2026_05_04.md",
    }
)

# Substrings that count as evidence the pre-screen was run + quoted in the memo.
# Any one occurrence is sufficient; the rule is "leave a discoverable trace,"
# not "follow a rigid template."
_PRE_SCREEN_EVIDENCE_MARKERS = (
    "signal_vol_regime",
    "classify_cyclicality",
    "Pre-screen verdict",
    "vol-regime conditional",
    "vol regime conditional",
)

_RESEARCH_DIR = Path(__file__).resolve().parent.parent / "docs" / "research"


def _collect_overlay_memos() -> list[Path]:
    """Return top-level *overlay*.md files in docs/research/.

    Excludes subdirectory contents (e.g. ``v10_drawdown_overlay/``
    audit-output JSON/MD which aren't design memos).
    """
    if not _RESEARCH_DIR.is_dir():
        return []
    return sorted(p for p in _RESEARCH_DIR.glob("*overlay*.md") if p.is_file())


class TestOverlayDesignCompliance(unittest.TestCase):
    def test_every_new_overlay_memo_quotes_pre_screen_evidence(self):
        memos = _collect_overlay_memos()
        # Sanity: there must be at least one overlay memo to scan; if zero,
        # someone deleted them all and the test is silently passing.
        self.assertGreater(
            len(memos),
            0,
            "no overlay memos found in docs/research/ — test is vacuously passing; "
            "verify _RESEARCH_DIR resolves correctly",
        )

        violations: list[tuple[str, str]] = []
        for memo in memos:
            if memo.name in _GRANDFATHERED_PRE_2026_05_10:
                continue
            text = memo.read_text(encoding="utf-8")
            if not any(marker in text for marker in _PRE_SCREEN_EVIDENCE_MARKERS):
                violations.append(
                    (
                        memo.name,
                        f"missing pre-screen evidence; expected one of: "
                        f"{list(_PRE_SCREEN_EVIDENCE_MARKERS)}",
                    )
                )

        if violations:
            msg = (
                "Layer 4 overlay design memos must quote vol-regime pre-screen "
                "evidence per CLAUDE.md / Research methodology. Violations:\n"
            )
            for name, reason in violations:
                msg += f"  - {name}: {reason}\n"
            msg += (
                "\nFix options:\n"
                "  (a) Add `signal_vol_regime.classify_cyclicality()` output to memo body, OR\n"
                "  (b) Add explicit 'Pre-screen verdict:' section quoting the result, OR\n"
                "  (c) If memo legitimately pre-dates 2026-05-10, add filename to "
                "_GRANDFATHERED_PRE_2026_05_10 allowlist (with user authorization)."
            )
            self.fail(msg)

    def test_grandfathered_files_still_exist(self):
        """Removing a grandfathered file requires removing its allowlist entry too.

        Defends against silent allowlist rot: if a memo is deleted/renamed but
        the allowlist isn't cleaned up, the next memo with the same name
        would be silently grandfathered.
        """
        missing = []
        for name in _GRANDFATHERED_PRE_2026_05_10:
            if not (_RESEARCH_DIR / name).is_file():
                missing.append(name)
        self.assertEqual(
            missing,
            [],
            f"grandfathered allowlist entries no longer exist on disk: {missing}. "
            f"Remove from _GRANDFATHERED_PRE_2026_05_10 in this test file.",
        )


if __name__ == "__main__":
    unittest.main()
