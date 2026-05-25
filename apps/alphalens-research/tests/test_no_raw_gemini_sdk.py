"""Enforcement: no raw google-genai SDK imports outside the canonical client.

The 2026-05-20 vendor-client consolidation routed every Gemini call in
the repo through
:class:`alphalens_pipeline.data.alt_data.gemini_client.GeminiClient`. Five live
sites (backtest LLM scorers + four thematic modules) used to each
duplicate ``from google import genai`` + ``genai.Client(api_key=...)``
+ a hand-rolled SDK-missing error message. One canonical client means
one place to inject quota tracking, one HTTP keepalive pool, one
actionable error when the SDK is absent.

This test fails red if anyone reintroduces a raw SDK import (defined as
``from google import genai`` / ``from google.genai import`` /
``genai.Client(`` / ``google.generativeai`` / ``langchain_google_genai``)
in any file under ``alphalens_research/`` (excluding ``alphalens_research/archive/`` — the
closed-layer catalog is intentionally frozen), ``alphalens_cli/``, or
``scripts/``.

Mirror of :mod:`tests.test_no_raw_av_http` and :mod:`tests.test_no_raw_sec_http`;
same structure (positive control + negative scan).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]

SCAN_DIRS = (
    WORKSPACE_ROOT / "apps" / "alphalens-pipeline" / "alphalens_pipeline",
    WORKSPACE_ROOT / "apps" / "alphalens-pipeline" / "alphalens_cli",
    WORKSPACE_ROOT / "apps" / "alphalens-research" / "alphalens_research",
    WORKSPACE_ROOT / "apps" / "alphalens-research" / "scripts",
)

# Path prefixes inside SCAN_DIRS that are intentionally exempt from the
# enforcement. The canonical client itself is allowed to import the SDK.
EXEMPT_PATH_PREFIXES = (
    "apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/gemini_client.py",
)

# Patterns that constitute a raw SDK import or shadow client construction.
# Word-boundary regex (negative lookbehind on \w. ) so docstring prose
# mentioning "genai" doesn't false-positive, but actual SDK imports / calls
# do. The canonical client uses ``self._sdk_client.models.generate_content``
# which doesn't match because ``self.`` defeats the lookbehind.
SHADOW_PATTERNS = (
    re.compile(r"\bfrom\s+google\s+import\s+genai\b"),
    re.compile(r"\bfrom\s+google\.genai\b"),
    re.compile(r"\bfrom\s+google\.generativeai\b"),
    re.compile(r"\bimport\s+google\.generativeai\b"),
    re.compile(r"(?<![\w.])genai\.Client\("),
    re.compile(r"\blangchain_google_genai\b"),
)


def _find_shadow_lines(text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        for pattern in SHADOW_PATTERNS:
            if pattern.search(line):
                hits.append((lineno, line.rstrip()))
                break
    return hits


def _path_is_exempt(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in EXEMPT_PATH_PREFIXES)


class TestNoRawGeminiSdk(unittest.TestCase):
    def test_detection_regex_locks_shadow_patterns(self):
        """Positive control: each shape we MEAN to catch is flagged, each
        known-safe shape is NOT. Pinning this lets the negative test below
        be trusted — without it, a regex that rotted to ``""`` would let
        every shadow client through silently."""
        shadow_samples = [
            "from google import genai",
            "from google.genai import types",
            "from google.generativeai import GenerativeModel",
            "import google.generativeai as genai",
            "client = genai.Client(api_key=key)",
            "from langchain_google_genai import ChatGoogleGenerativeAI",
        ]
        for sample in shadow_samples:
            hits = _find_shadow_lines(sample)
            self.assertEqual(len(hits), 1, f"expected one hit on shadow sample: {sample!r}")

        safe_samples = [
            "self._sdk_client.models.generate_content(model=m)",
            '"""Docstring mentioning genai and google.genai for context."""',
            "# from google import genai in a comment must never trip detection",
            "client = GeminiClient(api_key='x')",
            "from alphalens_pipeline.data.alt_data.gemini_client import GeminiClient",
        ]
        for sample in safe_samples:
            hits = _find_shadow_lines(sample)
            self.assertEqual(
                len(hits), 0, f"expected zero hits on safe sample: {sample!r} (got {hits})"
            )

    def test_no_shadow_gemini_sdk_outside_canonical_client(self):
        offenders: list[tuple[str, int, str]] = []
        for root in SCAN_DIRS:
            if not root.exists():
                continue
            for py in root.rglob("*.py"):
                rel = py.relative_to(WORKSPACE_ROOT).as_posix()
                if _path_is_exempt(rel):
                    continue
                text = py.read_text(encoding="utf-8", errors="replace")
                for lineno, src in _find_shadow_lines(text):
                    offenders.append((rel, lineno, src))

        if offenders:
            details = "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in offenders)
            self.fail(
                "Raw google-genai SDK import / shadow client detected.\n"
                "Route the call through "
                "alphalens_pipeline.data.alt_data.gemini_client (use\n"
                "get_default_gemini_client() if you don't have one to inject).\n"
                f"Offenders:\n{details}"
            )


if __name__ == "__main__":
    unittest.main()
