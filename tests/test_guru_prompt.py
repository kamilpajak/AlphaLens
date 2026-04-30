"""Tests for alphalens.archive.guru.prompt — Buffett-style prompt loader + SHA fingerprint."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SAMPLE_PROMPT = """You are a Warren Buffett-style value investor.
Given the financial context for a company, score it 0-100 for investability.

Criteria:
- Economic moat (durability of competitive advantage)
- ROE consistency (>15% over multiple years)
- Valuation (avoid overpaying)
- Management quality (capital allocation, incentives)

Output JSON: {"conviction": <0-100>, "rationale": "<2-3 sentences>"}
"""


class TestGuruPromptLoader(unittest.TestCase):
    def test_loads_prompt_text_file(self):
        from alphalens.archive.guru.prompt import GuruPrompt, load_guru_prompt

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt.txt"
            path.write_text(_SAMPLE_PROMPT)

            sha = "d" * 40
            completed = MagicMock(returncode=0, stdout=f"{sha}\n", stderr="")
            with patch("alphalens.archive.guru.prompt.subprocess.run", return_value=completed):
                prompt = load_guru_prompt(path, allow_dirty=True)

        self.assertIsInstance(prompt, GuruPrompt)
        self.assertEqual(prompt.text, _SAMPLE_PROMPT)
        self.assertEqual(len(prompt.content_sha256), 64)
        self.assertEqual(prompt.git_sha, sha)
        self.assertEqual(prompt.path, str(path))

    def test_rejects_empty_prompt(self):
        from alphalens.archive.guru.prompt import GuruPromptError, load_guru_prompt

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.txt"
            path.write_text("")

            with self.assertRaises(GuruPromptError):
                load_guru_prompt(path, allow_dirty=True)

    def test_rejects_missing_file(self):
        from alphalens.archive.guru.prompt import GuruPromptError, load_guru_prompt

        with self.assertRaises(GuruPromptError):
            load_guru_prompt(Path("/nonexistent/prompt.txt"), allow_dirty=True)

    def test_fingerprint_stable_across_loads(self):
        from alphalens.archive.guru.prompt import load_guru_prompt

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt.txt"
            path.write_text(_SAMPLE_PROMPT)

            sha = "e" * 40
            completed = MagicMock(returncode=0, stdout=f"{sha}\n", stderr="")
            with patch("alphalens.archive.guru.prompt.subprocess.run", return_value=completed):
                p1 = load_guru_prompt(path, allow_dirty=True)
                p2 = load_guru_prompt(path, allow_dirty=True)

        self.assertEqual(p1.content_sha256, p2.content_sha256)


if __name__ == "__main__":
    unittest.main()
