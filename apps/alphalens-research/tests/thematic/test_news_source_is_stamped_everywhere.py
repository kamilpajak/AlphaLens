"""Every production `mapper_config_version` call must record which news store was read.

The cohort key exists so a frozen candidate set cannot be reused under different
rules. Reading the append-only news lake changes which catalyst is rendered, so
the key has to carry it — at EVERY call site, not only the ones someone
remembered.

This is a SOURCE gate rather than a behaviour test on purpose. One of the call
sites, ``orchestrator.write_empty_candidates``, stamps a zero-row frame, so
nothing it writes ever reaches disk and no behavioural test can see an omission
there.

It WALKS the production roots rather than listing files. An earlier version
listed two paths and claimed in its own docstring that "a new one added without
the argument is a silent cohort break, which is why this list is checked rather
than trusted" — but it checked the list, not the set of calls. A review added a
new production function calling the token builder without the argument and all
2,014 tests stayed green. Walking is the same shape as the cross-tier rule in
``test_module_dependencies``.

Test call sites are deliberately OUT of scope: a test that exercises the token
builder itself has every right to pass its own arguments.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
#: The production roots. A call anywhere under these must stamp the news source.
_ROOTS = (
    "apps/alphalens-pipeline/alphalens_pipeline",
    "apps/alphalens-research/scripts",
)
_FUNCTION = "mapper_config_version"
_REQUIRED_KEYWORD = "news_source"
#: Positive control. Below this the walk found nothing and would pass vacuously —
#: the same floor `test_layer_status.test_discovery_is_non_empty` puts under its
#: own discovery. Measured 2026-11-09: 3 production call sites.
_MIN_CALL_SITES = 3


def _calls_in(path: Path) -> list[tuple[str, bool]]:
    """Every `mapper_config_version` CALL in one file, and whether it stamps.

    A ``def`` of that name is not a call and is skipped by construction, since
    only ``ast.Call`` nodes are inspected.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    found: list[tuple[str, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != _FUNCTION:
            continue
        stamped = any(kw.arg == _REQUIRED_KEYWORD for kw in node.keywords)
        found.append((f"{path.relative_to(_REPO)}:{node.lineno}", stamped))
    return found


def _all_production_calls() -> list[tuple[str, bool]]:
    calls: list[tuple[str, bool]] = []
    for rel in _ROOTS:
        root = _REPO / rel
        if not root.exists():
            raise AssertionError(f"{rel} moved; update _ROOTS")
        for path in sorted(root.rglob("*.py")):
            calls.extend(_calls_in(path))
    return calls


class EveryProductionCallRecordsTheNewsSource(unittest.TestCase):
    def test_no_production_call_site_omits_the_news_source(self) -> None:
        missing = [where for where, stamped in _all_production_calls() if not stamped]
        self.assertEqual(
            missing,
            [],
            "these mapper_config_version calls do not record news_source, so a run "
            "that reads the lake would be stamped as if it had read the view alone",
        )

    def test_the_walk_actually_finds_the_call_sites(self) -> None:
        """Without this floor a broken walk passes by finding nothing."""
        calls = _all_production_calls()
        self.assertGreaterEqual(
            len(calls),
            _MIN_CALL_SITES,
            f"the walk found {len(calls)} call sites, fewer than the {_MIN_CALL_SITES} "
            "measured; the roots or the detector are broken, not the code",
        )

    def test_the_detector_can_see_an_omission(self) -> None:
        """Positive control on the detector itself."""
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        probe = _REPO / "apps" / "alphalens-pipeline" / "alphalens_pipeline" / "_probe_tmp.py"
        probe.write_text(
            "mapper_config_version(market_cap_range=(1, 2), channel_config_version='x')\n",
            encoding="utf-8",
        )
        self.addCleanup(probe.unlink)
        self.assertIn(
            (f"{probe.relative_to(_REPO)}:1", False),
            _calls_in(probe),
        )
        self.assertIn(
            f"{probe.relative_to(_REPO)}:1",
            [where for where, stamped in _all_production_calls() if not stamped],
            "the WALK must reach a new file under a production root, not only the "
            "detector when handed that file directly",
        )


if __name__ == "__main__":
    unittest.main()
