"""Enforcement: no raw SEC HTTP outside the canonical SecEdgarClient.

The 2026-05-19 vendor-client consolidation routed every SEC fetch in the
repo (edgar_detector, thematic verification, EDGAR fundamentals) through
:class:`alphalens_pipeline.data.alt_data.sec_edgar_client.SecEdgarClient`. SEC's
fair-access policy (10 req/s per IP, mandatory descriptive User-Agent)
is enforced at the IP level — every shadow client is a vector for a 403
that takes down EVERY SEC consumer at once.

This test fails red if anyone reintroduces a raw SEC HTTP call (defined as
``urllib.request.urlopen`` / ``urllib.request.Request`` / ``requests.get(``)
in a file that also mentions a SEC URL fragment.

Why the conjunction: a constant like ``SEC_SEARCH_URL = "https://efts..."``
is fine when the URL is built locally and handed to ``client.get_json(url)``.
The smell is a SEC URL paired with a raw HTTP call in the same module — that
means the file bypasses the client's throttle + retry + UA contract.

Mirror of :mod:`tests.test_no_raw_av_http` and :mod:`tests.test_no_raw_polygon_http`;
same conjunction logic (URL fragment AND raw HTTP pattern, both in the same
file), compiled word-boundary regexes, and a ``test_detection_regex_locks_shadow_patterns``
positive control so the matcher / URL-fragment lists cannot rot to empty silently.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]

# Directories where production / operator code lives. Tests + the canonical
# client itself are intentionally excluded — tests can hardcode URL fragments
# in fixtures and assertions, and the canonical client owns these URLs.
SCAN_DIRS = (
    WORKSPACE_ROOT / "apps" / "alphalens-pipeline" / "alphalens_pipeline",
    WORKSPACE_ROOT / "apps" / "alphalens-pipeline" / "alphalens_cli",
    WORKSPACE_ROOT / "apps" / "alphalens-research" / "alphalens_research",
    WORKSPACE_ROOT / "apps" / "alphalens-research" / "scripts",
)

# The canonical client itself — only file allowed to make raw SEC HTTP.
CANONICAL_CLIENT_REL = (
    "apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/sec_edgar_client.py"
)

# Fragments that uniquely identify SEC endpoints used by the project.
SEC_URL_FRAGMENTS = (
    "data.sec.gov",
    "www.sec.gov/Archives",
    "www.sec.gov/cgi-bin/browse-edgar",
    "www.sec.gov/files/company_tickers",
    "efts.sec.gov",
)

# Module-level patterns that constitute a raw HTTP call. Word-boundary +
# call-shape ensures a substring like "burning further requests." in a
# docstring doesn't match, but ``requests.get(``, ``urlopen(``, ``httpx.post(``,
# ``aiohttp.ClientSession`` all do. We match the module-and-attribute prefix
# rather than specific function names so the enforcement covers requests.post /
# requests.Session().get / httpx.* / aiohttp.ClientSession() if a future
# contributor reaches for a different HTTP library. The canonical client uses
# ``self._session.get(...)``; ``self.`` defeats the word boundary on the left,
# so it's exempt. The ``urllib.parse.urlencode`` helper used to build SEC URLs
# in edgar_detector edgar.py is fine — it doesn't fetch anything — and the
# ``urllib\.request\.`` pattern below does not match ``urllib.parse.``.
RAW_HTTP_PATTERNS = (
    re.compile(r"(?<![\w.])urlopen\("),
    re.compile(r"(?<![\w.])urllib\.request\.\w+"),
    re.compile(r"(?<![\w.])requests\.\w+\("),
    re.compile(r"(?<![\w.])httpx\.\w+\("),
    re.compile(r"(?<![\w.])aiohttp\.\w+"),
)


def _file_uses_sec_url(text: str) -> bool:
    return any(frag in text for frag in SEC_URL_FRAGMENTS)


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


class TestNoRawSecHttp(unittest.TestCase):
    def test_detection_regex_locks_shadow_patterns(self):
        """Positive control on the detection regex itself. The negative test
        below could silently pass if the regex / URL-fragment lists rot to
        empty; this test asserts that each shape we MEAN to catch (bare
        urlopen, urllib.request.urlopen, requests.get, httpx.post,
        aiohttp.ClientSession) is flagged, and that known-safe shapes
        (canonical DI ``self._session.get``, ``urllib.parse.urlencode`` URL
        building, and docstring prose) are NOT.
        """
        shadow_samples = [
            'with urlopen("https://www.sec.gov/Archives/edgar/data/320193") as resp:',
            'urllib.request.urlopen("https://data.sec.gov/submissions/CIK0000320193.json")',
            'resp = requests.get("https://efts.sec.gov/LATEST/search-index")',
            'await httpx.post("https://www.sec.gov/cgi-bin/browse-edgar")',
            "aiohttp.ClientSession()  # https://data.sec.gov/submissions/",
        ]
        for sample in shadow_samples:
            hits = _find_raw_http_lines(sample)
            self.assertEqual(len(hits), 1, f"expected exactly one hit on shadow sample: {sample!r}")

        safe_samples = [
            "with self._session.get(url, headers=headers) as resp:",
            "query = urllib.parse.urlencode({'action': 'getcompany'})",
            '"""...persistent rate-limit (retry exhausted), the exception """',
            "# urlopen line in a comment must never trip detection",
            "from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient",
            "client = get_default_sec_client()",
        ]
        for sample in safe_samples:
            hits = _find_raw_http_lines(sample)
            self.assertEqual(
                len(hits), 0, f"expected zero hits on safe sample: {sample!r} (got {hits})"
            )

        # And the URL fragment list must cover the canonical SEC base URLs.
        self.assertTrue(_file_uses_sec_url('"https://data.sec.gov/submissions/CIK0000320193.json"'))
        self.assertTrue(_file_uses_sec_url('"https://www.sec.gov/Archives/edgar/data/320193"'))

    def test_no_shadow_sec_http_outside_canonical_client(self):
        offenders: list[tuple[str, int, str]] = []
        for root in SCAN_DIRS:
            if not root.exists():
                continue
            for py in root.rglob("*.py"):
                rel = py.relative_to(WORKSPACE_ROOT).as_posix()
                if rel == CANONICAL_CLIENT_REL:
                    continue
                text = py.read_text(encoding="utf-8", errors="replace")
                if not _file_uses_sec_url(text):
                    continue
                for lineno, src in _find_raw_http_lines(text):
                    offenders.append((rel, lineno, src))

        if offenders:
            details = "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in offenders)
            self.fail(
                "Raw SEC HTTP detected outside SecEdgarClient. Route the\n"
                "call through alphalens_pipeline.data.alt_data.sec_edgar_client (use\n"
                "get_default_sec_client() if you don't have one to inject).\n"
                f"Offenders:\n{details}"
            )


if __name__ == "__main__":
    unittest.main()
