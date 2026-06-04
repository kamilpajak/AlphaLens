"""Pin the doc/config-drift cleanup contract (audit fixes L4 + L5).

Three drift items the architecture audit surfaced:

  - **L4** — ``CLAUDE.md`` (Conventions) and ``README.md`` both reference a
    ``test_lean_config_parity.py`` enforcement test that was DELETED with the
    lean stack (ADR 0010). Neither doc may keep claiming it exists.

  - **L5** — there was no root ``.env.example``. The live root ``.env`` carries
    ~17 keys while ``CLAUDE.md`` ``## Environment`` listed ~6, and DEAD keys
    (``GOOGLE_API_KEY`` after PR #416 removed the Gemini client; the ``ALPACA_*``
    broker keys after ADR 0012 decommissioned the paper-trade chain) lingered
    undocumented. This file pins a root ``.env.example`` that (a) exists,
    (b) documents the live keys, (c) carries NO real secret values (placeholders
    / empty only), and (d) explicitly marks the dead keys.

The L6-doc item (a "double-checked locking used by ALL client factories"
overclaim) is intentionally NOT pinned here: no such claim exists in any
tracked doc, so there is nothing to guard against (verified by grep over
``CLAUDE.md`` / ``README.md`` / ``docs/`` during the drift-cleanup PR).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

# Test file lives at apps/alphalens-research/tests/<name>.py; the repo root is
# three parents up.
REPO_ROOT = Path(__file__).resolve().parents[3]
ROOT_ENV_EXAMPLE = REPO_ROOT / ".env.example"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
README_MD = REPO_ROOT / "README.md"

# A ``KEY=value`` line in a dotenv-style file; value half optional. Anchored to
# a line start so a commented ``# KEY=`` line is ignored.
ENV_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", re.MULTILINE)

# Keys the live root .env carries that are still wired to running code today.
LIVE_KEYS = frozenset(
    {
        "OPENROUTER_API_KEY",
        "ALPHA_VANTAGE_API_KEY",
        "POLYGON_API_KEY",
        "FRED_API_KEY",
        "PERPLEXITY_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    }
)

# Keys that are DEAD — present in the live .env but with zero source consumers.
# GOOGLE_API_KEY: Gemini client removed (PR #416). ALPACA_*: broker chain
# decommissioned (ADR 0012).
DEAD_KEYS = frozenset(
    {
        "GOOGLE_API_KEY",
        "ALPACA_API_BASE_URL",
        "ALPACA_API_KEY",
        "ALPACA_API_SECRET",
        "ALPACA_TEST_API_KEY",
        "ALPACA_TEST_API_SECRET",
    }
)


def _env_keys(text: str) -> set[str]:
    return set(ENV_KEY_RE.findall(text))


class TestRootEnvExample(unittest.TestCase):
    """L5 — a root .env.example with the right keys and no real secrets."""

    def setUp(self) -> None:
        self.assertTrue(
            ROOT_ENV_EXAMPLE.is_file(),
            f"missing root .env.example at {ROOT_ENV_EXAMPLE} (audit fix L5)",
        )
        self.text = ROOT_ENV_EXAMPLE.read_text()
        self.keys = _env_keys(self.text)

    def test_documents_every_live_key(self) -> None:
        missing = sorted(LIVE_KEYS - self.keys)
        self.assertEqual(
            missing,
            [],
            f"root .env.example is missing live key(s): {missing}",
        )

    def test_marks_dead_keys(self) -> None:
        # Each dead key must appear in the file AND be flagged DEAD in prose so
        # an operator does not bother filling it in. The dead key may be on a
        # commented or active line; the DEAD marker must sit near it.
        upper = self.text.upper()
        self.assertIn("DEAD", upper, "no DEAD marker in root .env.example")
        for key in sorted(DEAD_KEYS):
            self.assertIn(
                key,
                self.text,
                f"dead key {key} not mentioned in root .env.example",
            )

    def test_no_real_secret_values(self) -> None:
        # Every active KEY=... line must have an empty or placeholder value —
        # never a real secret. Placeholders allowed: empty, or wrapped in
        # angle brackets / ALL-CAPS-WITH-UNDERSCORES tokens.
        offenders: list[str] = []
        placeholder_re = re.compile(r"^(|<.*>|[A-Z0-9_]+|your[-_].*|changeme)$", re.IGNORECASE)
        for raw in self.text.splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", stripped)
            if not m:
                continue
            value = m.group(2).strip()
            if value and not placeholder_re.match(value):
                offenders.append(stripped)
        self.assertEqual(
            offenders,
            [],
            "root .env.example carries non-placeholder value(s) (possible real "
            f"secret): {offenders}",
        )


class TestLeanConfigParityReferenceRemoved(unittest.TestCase):
    """L4 — no tracked doc may claim test_lean_config_parity.py still exists."""

    def test_file_really_is_gone(self) -> None:
        # Guard the premise: if the test file were somehow re-added, the doc
        # references would be correct and these assertions would be wrong.
        stale = REPO_ROOT / "apps/alphalens-research/tests/test_lean_config_parity.py"
        self.assertFalse(
            stale.is_file(),
            "test_lean_config_parity.py exists again — the doc references are "
            "no longer stale; revert this guard.",
        )

    def test_claude_md_does_not_reference_it(self) -> None:
        self.assertNotIn(
            "test_lean_config_parity",
            CLAUDE_MD.read_text(),
            "CLAUDE.md still references the deleted test_lean_config_parity.py",
        )

    def test_readme_does_not_reference_it(self) -> None:
        self.assertNotIn(
            "test_lean_config_parity",
            README_MD.read_text(),
            "README.md still references the deleted test_lean_config_parity.py",
        )


if __name__ == "__main__":
    unittest.main()
