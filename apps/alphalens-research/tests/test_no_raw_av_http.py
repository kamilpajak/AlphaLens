"""Enforcement: no raw Alpha Vantage HTTP outside the canonical client.

The 2026-05-20 vendor-client consolidation routed every Alpha Vantage
fetch in the repo (fundamentals fetcher + EARNINGS bulk cache) through
:class:`alphalens_research.data.alt_data.alphavantage_client.AlphaVantageClient`.
AV's free-tier quota (25 req/day) is per-API-key, so any uncoordinated
shadow client eats quota from every other consumer in the process — a
single forgotten ``urlopen("https://www.alphavantage.co/...")`` can
exhaust the daily window before the EARNINGS backfill gets its first
ticker.

This test fails red if anyone reintroduces a raw AV HTTP call (defined as
``urllib.request.urlopen`` / ``urllib.request.Request`` / ``requests.get(``)
in a file that also mentions an AV URL fragment.

Mirror of :mod:`tests.test_no_raw_sec_http`; same conjunction logic
(URL fragment AND raw HTTP pattern, both in the same file).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = (
    REPO_ROOT / "alphalens_research",
    REPO_ROOT / "alphalens_cli",
    REPO_ROOT / "scripts",
)

# The canonical client itself — only file allowed to make raw AV HTTP.
CANONICAL_CLIENT_REL = "alphalens_research/data/alt_data/alphavantage_client.py"

# Fragments that uniquely identify AV endpoints used by the project.
AV_URL_FRAGMENTS = (
    "alphavantage.co",
    "www.alphavantage.co",
)

# Module-level patterns that constitute a raw HTTP call. Word-boundary +
# call-shape ensures "burning further requests." in a docstring doesn't
# match, but `requests.get(`, `urlopen(`, `httpx.post(`, `aiohttp.ClientSession`
# all do. The canonical client uses ``self._urlopen(...)`` (injected); ``self.``
# defeats the word boundary on the left, so it's exempt.
RAW_HTTP_PATTERNS = (
    re.compile(r"(?<![\w.])urlopen\("),
    re.compile(r"(?<![\w.])urllib\.request\.\w+"),
    re.compile(r"(?<![\w.])requests\.\w+\("),
    re.compile(r"(?<![\w.])httpx\.\w+\("),
    re.compile(r"(?<![\w.])aiohttp\.\w+"),
)


def _file_uses_av_url(text: str) -> bool:
    return any(frag in text for frag in AV_URL_FRAGMENTS)


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


class TestNoRawAvHttp(unittest.TestCase):
    def test_detection_regex_locks_shadow_patterns(self):
        """Positive control on the detection regex itself. The negative test
        could silently pass if the regex / URL-fragment lists rot to empty;
        this test asserts that each shape we MEAN to catch (bare urlopen,
        urllib.request.urlopen, requests.get, httpx.post, aiohttp.Client) is
        flagged, and that two known-safe shapes (canonical DI ``self._urlopen``
        and docstring prose like ``"burning further requests."``) are NOT.
        """
        shadow_samples = [
            'with urlopen("https://www.alphavantage.co/query") as resp:',
            'urllib.request.urlopen("https://www.alphavantage.co/query")',
            'resp = requests.get("https://www.alphavantage.co/query")',
            'await httpx.post("https://www.alphavantage.co/query")',
            "aiohttp.ClientSession()  # https://www.alphavantage.co/query",
        ]
        for sample in shadow_samples:
            hits = _find_raw_http_lines(sample)
            self.assertEqual(len(hits), 1, f"expected exactly one hit on shadow sample: {sample!r}")

        safe_samples = [
            "with self._urlopen(url, timeout=self._timeout) as resp:",
            '"""...persistent rate-limit (retry exhausted), the exception """',
            "# urlopen line in a comment must never trip detection",
            "the operator can resume on next quota window without burning further requests.",
        ]
        for sample in safe_samples:
            hits = _find_raw_http_lines(sample)
            self.assertEqual(
                len(hits), 0, f"expected zero hits on safe sample: {sample!r} (got {hits})"
            )

        # And the URL fragment list must cover the AV base URL itself.
        self.assertTrue(_file_uses_av_url('"https://www.alphavantage.co/query"'))

    def test_no_shadow_av_http_outside_canonical_client(self):
        offenders: list[tuple[str, int, str]] = []
        for root in SCAN_DIRS:
            if not root.exists():
                continue
            for py in root.rglob("*.py"):
                rel = py.relative_to(REPO_ROOT).as_posix()
                if rel == CANONICAL_CLIENT_REL:
                    continue
                text = py.read_text(encoding="utf-8", errors="replace")
                if not _file_uses_av_url(text):
                    continue
                for lineno, src in _find_raw_http_lines(text):
                    offenders.append((rel, lineno, src))

        if offenders:
            details = "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in offenders)
            self.fail(
                "Raw Alpha Vantage HTTP detected outside AlphaVantageClient.\n"
                "Route the call through "
                "alphalens_research.data.alt_data.alphavantage_client (use\n"
                "get_default_av_client() if you don't have one to inject).\n"
                f"Offenders:\n{details}"
            )


if __name__ == "__main__":
    unittest.main()
