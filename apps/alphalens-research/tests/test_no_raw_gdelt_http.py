"""Enforcement: no raw GDELT HTTP outside the canonical client.

Every GDELT 2.0 DOC API fetch — one query per thematic bucket per day in the
live news ingest (1 of 4 sources) — goes through
:class:`alphalens_pipeline.data.alt_data.gdelt_client.GdeltClient` (the
module-level :func:`_http_get_json` it wraps). GDELT is keyless, so there is no
API key to leak, but the canonical-client doctrine still applies: one shared
``urlopen`` + retry + permanent-vs-transient seam so a stray second fetcher can't
trip GDELT's ~5s/req soft cap for the next bucket.

This test fails red if a raw HTTP call (``urlopen`` / ``urllib.request`` /
``requests.*`` / ``httpx.*`` / ``aiohttp``) appears in a file that also mentions
a GDELT URL fragment. Mirror of :mod:`tests.test_no_raw_polygon_http` with a
positive control + anti-rot check so the detection regex / URL-fragment list
cannot silently rot to empty.
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

# The canonical client itself — only file allowed to make raw GDELT HTTP.
CANONICAL_CLIENT_REL = "apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/gdelt_client.py"

# Fragments that uniquely identify GDELT DOC API endpoints.
GDELT_URL_FRAGMENTS = (
    "api.gdeltproject.org",
    "gdeltproject.org",
)

RAW_HTTP_PATTERNS = (
    re.compile(r"(?<![\w.])urlopen\("),
    re.compile(r"(?<![\w.])urllib\.request\.\w+"),
    re.compile(r"(?<![\w.])requests\.\w+\("),
    re.compile(r"(?<![\w.])httpx\.\w+\("),
    re.compile(r"(?<![\w.])aiohttp\.\w+"),
)


def _file_uses_gdelt_url(text: str) -> bool:
    return any(frag in text for frag in GDELT_URL_FRAGMENTS)


def _find_raw_http_lines(text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        for pattern in RAW_HTTP_PATTERNS:
            if pattern.search(line):
                hits.append((lineno, line.rstrip()))
                break
    return hits


class TestNoRawGdeltHttp(unittest.TestCase):
    def test_detection_regex_locks_shadow_patterns(self):
        """Positive control on the detection regex + URL-fragment list."""
        shadow_samples = [
            'with urlopen("https://api.gdeltproject.org/api/v2/doc/doc") as r:',
            'urllib.request.urlopen("https://api.gdeltproject.org/api/v2/doc/doc")',
            'resp = requests.get("https://api.gdeltproject.org/api/v2/doc/doc")',
            'await httpx.get("https://api.gdeltproject.org/api/v2/doc/doc")',
            "aiohttp.ClientSession()  # https://api.gdeltproject.org/api/v2/doc/doc",
        ]
        for sample in shadow_samples:
            self.assertEqual(
                len(_find_raw_http_lines(sample)), 1, f"missed shadow sample: {sample!r}"
            )

        safe_samples = [
            "data = get_default_gdelt_client().fetch_doc(query=query)",
            "# urlopen line in a comment must never trip detection",
            "from alphalens_pipeline.data.alt_data.gdelt_client import get_default_gdelt_client",
        ]
        for sample in safe_samples:
            self.assertEqual(len(_find_raw_http_lines(sample)), 0, f"false positive: {sample!r}")

        self.assertTrue(
            _file_uses_gdelt_url('ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"')
        )

    def test_canonical_client_is_present_and_scanned(self):
        """Anti-rot: the canonical client must exist and itself make a raw GDELT
        call (otherwise the exemption guards nothing)."""
        client = WORKSPACE_ROOT / CANONICAL_CLIENT_REL
        self.assertTrue(client.exists(), f"canonical GDELT client missing: {CANONICAL_CLIENT_REL}")
        text = client.read_text(encoding="utf-8")
        self.assertTrue(
            _file_uses_gdelt_url(text), "canonical client no longer references a GDELT URL"
        )
        self.assertNotEqual(
            _find_raw_http_lines(text), [], "canonical client no longer makes a raw GDELT HTTP call"
        )

    def test_no_shadow_gdelt_http_outside_canonical_client(self):
        offenders: list[str] = []
        for root in SCAN_DIRS:
            if not root.exists():
                continue
            for py in root.rglob("*.py"):
                rel = py.relative_to(WORKSPACE_ROOT).as_posix()
                if rel == CANONICAL_CLIENT_REL:
                    continue
                text = py.read_text(encoding="utf-8", errors="replace")
                if not _file_uses_gdelt_url(text):
                    continue
                offenders.extend(f"  {rel}:{ln}  {src}" for ln, src in _find_raw_http_lines(text))

        self.assertEqual(
            offenders,
            [],
            "Raw GDELT HTTP detected outside GdeltClient. Route it through "
            "alphalens_pipeline.data.alt_data.gdelt_client.get_default_gdelt_client():\n"
            + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
