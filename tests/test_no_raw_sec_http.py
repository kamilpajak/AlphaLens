"""Enforcement: no raw SEC HTTP outside the canonical SecEdgarClient.

The 2026-05-19 vendor-client consolidation routed every SEC fetch in the
repo (watchdog, thematic verification, EDGAR fundamentals) through
:class:`alphalens.data.alt_data.sec_edgar_client.SecEdgarClient`. SEC's
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
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories where production / operator code lives. Tests + the canonical
# client itself are intentionally excluded — tests can hardcode URL fragments
# in fixtures and assertions, and the canonical client owns these URLs.
SCAN_DIRS = (
    REPO_ROOT / "alphalens",
    REPO_ROOT / "alphalens_cli",
    REPO_ROOT / "scripts",
)

# The canonical client itself — only file allowed to make raw SEC HTTP.
CANONICAL_CLIENT_REL = "alphalens/data/alt_data/sec_edgar_client.py"

# Fragments that uniquely identify SEC endpoints used by the project.
SEC_URL_FRAGMENTS = (
    "data.sec.gov",
    "www.sec.gov/Archives",
    "www.sec.gov/cgi-bin/browse-edgar",
    "www.sec.gov/files/company_tickers",
    "efts.sec.gov",
)

# Patterns that constitute a raw HTTP call. The canonical client uses
# ``self._session.get(...)`` which does not match ``requests.get(``.
RAW_HTTP_PATTERNS = (
    "urllib.request.urlopen(",
    "urllib.request.Request(",
    "requests.get(",
)


def _file_uses_sec_url(text: str) -> bool:
    return any(frag in text for frag in SEC_URL_FRAGMENTS)


def _find_raw_http_lines(text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        for pattern in RAW_HTTP_PATTERNS:
            if pattern in line:
                hits.append((lineno, line.rstrip()))
                break
    return hits


class TestNoRawSecHttp(unittest.TestCase):
    def test_no_shadow_sec_http_outside_canonical_client(self):
        offenders: list[tuple[str, int, str]] = []
        for root in SCAN_DIRS:
            if not root.exists():
                continue
            for py in root.rglob("*.py"):
                rel = py.relative_to(REPO_ROOT).as_posix()
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
                "call through alphalens.data.alt_data.sec_edgar_client (use\n"
                "get_default_sec_client() if you don't have one to inject).\n"
                f"Offenders:\n{details}"
            )


if __name__ == "__main__":
    unittest.main()
