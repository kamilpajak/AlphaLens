"""Every `mapper_config_version` call must record which news store was read.

The cohort key exists so a frozen candidate set cannot be reused under different
rules. Reading the append-only lake changes which catalyst is rendered, so the
key has to carry it — at EVERY call site, not only the ones someone remembered.

This is a SOURCE gate rather than a behaviour test on purpose. One of the three
call sites, `orchestrator.write_empty_candidates`, stamps a zero-row frame, so
nothing it writes ever reaches disk and no behavioural test can see the omission.
An adversarial review found the third site (`scripts/record_golden_map.py`) left
out, and found that dropping the argument from the zero-row site keeps the whole
suite green. Only reading the source catches either one.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
#: Every file that composes the token. A new one added without the argument is a
#: silent cohort break, which is why this list is checked rather than trusted.
_CALLERS = (
    "apps/alphalens-pipeline/alphalens_pipeline/thematic/mapping/orchestrator.py",
    "apps/alphalens-research/scripts/record_golden_map.py",
)
_FUNCTION = "mapper_config_version"
_REQUIRED_KEYWORD = "news_source"


def _unstamped_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    missing: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != _FUNCTION:
            continue
        if not any(kw.arg == _REQUIRED_KEYWORD for kw in node.keywords):
            missing.append(f"{path.name}:{node.lineno}")
    return missing


class EveryConfigVersionCallRecordsTheNewsSource(unittest.TestCase):
    def test_no_call_site_omits_the_news_source(self) -> None:
        missing: list[str] = []
        for rel in _CALLERS:
            path = _REPO / rel
            self.assertTrue(path.exists(), f"{rel} moved; update _CALLERS")
            missing.extend(_unstamped_calls(path))
        self.assertEqual(
            missing,
            [],
            "these mapper_config_version calls do not record news_source, so a run "
            "that reads the lake would be stamped as if it had read the view alone",
        )

    def test_the_gate_can_see_an_omission(self) -> None:
        """Positive control: the detector must fire on a call with no keyword."""
        source = "mapper_config_version(market_cap_range=(1, 2), channel_config_version='x')\n"
        tmp = Path(self._tmp()) / "probe.py"
        tmp.write_text(source, encoding="utf-8")
        self.assertEqual(_unstamped_calls(tmp), ["probe.py:1"])

    def _tmp(self) -> str:
        import tempfile

        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return d.name


if __name__ == "__main__":
    unittest.main()
