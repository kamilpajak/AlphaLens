"""Enforcement: no raw FRED HTTP outside the canonical client.

Every FRED (St. Louis Fed) macro-series fetch — DGS10 / DGS2 / VIXCLS for the
Tactical Sector Rotation layer, refreshed daily on the live VPS via
``alphalens cache refresh-vix`` — goes through
:class:`alphalens_pipeline.data.macro.fred_client.FREDClient`. FRED's free tier
is 120 req/min per API key; an uncoordinated shadow ``requests.get`` would drain
that shared budget and bypass the client's one-day-per-series disk cache.

This test closes the same enforcement gap the yfinance migration (PR #573)
exposed: FREDClient already exists and is correctly used, but nothing stopped a
future shadow FRED call from creeping in. It fails red if a raw HTTP call
(``urlopen`` / ``urllib.request`` / ``requests.*`` / ``httpx.*`` / ``aiohttp``)
appears in a file that also mentions a FRED URL fragment.

Mirror of :mod:`tests.test_no_raw_polygon_http` (same conjunction logic: URL
fragment AND raw HTTP pattern in the same file) with a positive control so the
detection regex / URL-fragment list cannot silently rot to empty.
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

# The canonical client itself — only file allowed to make raw FRED HTTP.
CANONICAL_CLIENT_REL = "apps/alphalens-pipeline/alphalens_pipeline/data/macro/fred_client.py"

# Fragments that uniquely identify FRED endpoints used by the project.
FRED_URL_FRAGMENTS = (
    "api.stlouisfed.org",
    "stlouisfed.org",
    "/fred/series",
)

RAW_HTTP_PATTERNS = (
    re.compile(r"(?<![\w.])urlopen\("),
    re.compile(r"(?<![\w.])urllib\.request\.\w+"),
    re.compile(r"(?<![\w.])requests\.\w+\("),
    re.compile(r"(?<![\w.])httpx\.\w+\("),
    re.compile(r"(?<![\w.])aiohttp\.\w+"),
)


def _file_uses_fred_url(text: str) -> bool:
    return any(frag in text for frag in FRED_URL_FRAGMENTS)


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


class TestNoRawFredHttp(unittest.TestCase):
    def test_detection_regex_locks_shadow_patterns(self):
        """Positive control: the negative test below would silently pass if the
        regex / URL-fragment lists rot to empty. Assert each shape we MEAN to
        catch is flagged and known-safe shapes (canonical DI ``self._session``,
        prose) are NOT."""
        shadow_samples = [
            'with urlopen("https://api.stlouisfed.org/fred/series/observations") as r:',
            'urllib.request.urlopen("https://api.stlouisfed.org/fred/series")',
            'resp = requests.get("https://api.stlouisfed.org/fred/series/observations")',
            'await httpx.get("https://api.stlouisfed.org/fred/series")',
            "aiohttp.ClientSession()  # https://api.stlouisfed.org/fred/series",
        ]
        for sample in shadow_samples:
            self.assertEqual(
                len(_find_raw_http_lines(sample)), 1, f"missed shadow sample: {sample!r}"
            )

        safe_samples = [
            "resp = self._session.get(url, timeout=30)",
            "# urlopen line in a comment must never trip detection",
            "from alphalens_pipeline.data.macro.fred_client import FREDClient",
        ]
        for sample in safe_samples:
            self.assertEqual(len(_find_raw_http_lines(sample)), 0, f"false positive: {sample!r}")

        self.assertTrue(
            _file_uses_fred_url('"https://api.stlouisfed.org/fred/series/observations"')
        )

    def test_canonical_client_is_present_and_scanned(self):
        """Anti-rot: the canonical client must exist and itself contain a raw
        FRED call (otherwise the exemption guards nothing)."""
        client = WORKSPACE_ROOT / CANONICAL_CLIENT_REL
        self.assertTrue(client.exists(), f"canonical FRED client missing: {CANONICAL_CLIENT_REL}")
        text = client.read_text(encoding="utf-8")
        self.assertTrue(
            _file_uses_fred_url(text), "canonical client no longer references a FRED URL"
        )

    def test_no_shadow_fred_http_outside_canonical_client(self):
        offenders: list[str] = []
        for root in SCAN_DIRS:
            if not root.exists():
                continue
            for py in root.rglob("*.py"):
                rel = py.relative_to(WORKSPACE_ROOT).as_posix()
                if rel == CANONICAL_CLIENT_REL:
                    continue
                text = py.read_text(encoding="utf-8", errors="replace")
                if not _file_uses_fred_url(text):
                    continue
                offenders.extend(f"  {rel}:{ln}  {src}" for ln, src in _find_raw_http_lines(text))

        self.assertEqual(
            offenders,
            [],
            "Raw FRED HTTP detected outside FREDClient. Route it through "
            "alphalens_pipeline.data.macro.fred_client.FREDClient:\n" + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
