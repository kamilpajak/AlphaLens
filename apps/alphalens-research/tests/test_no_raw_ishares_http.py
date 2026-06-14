"""Enforcement: no raw iShares HTTP outside the canonical client.

The PIT-universe refreshers (IWM / Russell 2000, and the Core S&P mid/small/500
ETFs) fetch the undocumented iShares AJAX holdings CSV. Every such fetch goes
through :class:`alphalens_pipeline.data.alt_data.ishares_client.ISharesClient`
so a shadow ``requests.get`` to ishares.com can't creep back in (same
one-client-per-vendor doctrine as the SEC / AV / Polygon / yfinance / GDELT
clients). iShares is keyless and the refreshers are ad-hoc, so this is a
cleanliness / single-home lock rather than a quota concern.

Fails red if a raw HTTP call (``urlopen`` / ``urllib.request`` / ``requests.*`` /
``httpx.*`` / ``aiohttp``) appears in a file that also mentions an iShares URL
fragment. Mirror of :mod:`tests.test_no_raw_polygon_http` with a positive
control + anti-rot check.
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

# The canonical client itself — only file allowed to make raw iShares HTTP.
CANONICAL_CLIENT_REL = "apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/ishares_client.py"

# Fragments that uniquely identify the iShares holdings-CSV endpoint.
ISHARES_URL_FRAGMENTS = (
    "ishares.com",
    ".ajax?fileType=csv",
)

RAW_HTTP_PATTERNS = (
    re.compile(r"(?<![\w.])urlopen\("),
    re.compile(r"(?<![\w.])urllib\.request\.\w+"),
    re.compile(r"(?<![\w.])requests\.\w+\("),
    re.compile(r"(?<![\w.])httpx\.\w+\("),
    re.compile(r"(?<![\w.])aiohttp\.\w+"),
)


def _file_uses_ishares_url(text: str) -> bool:
    return any(frag in text for frag in ISHARES_URL_FRAGMENTS)


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


class TestNoRawISharesHttp(unittest.TestCase):
    def test_detection_regex_locks_shadow_patterns(self):
        """Positive control on the detection regex + URL-fragment list."""
        shadow_samples = [
            'resp = requests.get("https://www.ishares.com/us/products/239710/x.ajax?fileType=csv")',
            'urllib.request.urlopen("https://www.ishares.com/x.ajax?fileType=csv")',
            'with urlopen("https://www.ishares.com/x.ajax?fileType=csv") as r:',
            'await httpx.get("https://www.ishares.com/x.ajax?fileType=csv")',
            "aiohttp.ClientSession()  # https://www.ishares.com/x.ajax?fileType=csv",
        ]
        for sample in shadow_samples:
            self.assertEqual(
                len(_find_raw_http_lines(sample)), 1, f"missed shadow sample: {sample!r}"
            )

        safe_samples = [
            "return get_default_ishares_client().fetch_holdings_csv(url)",
            "resp = self._session.get(url, headers={...}, timeout=self._timeout)",
            "# requests.get to ishares.com in a comment must not trip detection",
            "from alphalens_pipeline.data.alt_data.ishares_client import get_default_ishares_client",
        ]
        for sample in safe_samples:
            self.assertEqual(len(_find_raw_http_lines(sample)), 0, f"false positive: {sample!r}")

        self.assertTrue(
            _file_uses_ishares_url(
                '"https://www.ishares.com/us/products/239710/x.ajax?fileType=csv"'
            )
        )

    def test_canonical_client_is_present_and_is_the_http_transport(self):
        """Anti-rot: the canonical client must exist and still be the single
        HTTP transport (a requests-session GET). It uses ``self._session.get``
        + a URL parameter, so it matches neither the raw-HTTP regex nor the
        URL-fragment list — that's intentional (the refreshers own the URLs).
        Pin the session GET so a refactor that drops it can't hollow out the
        exemption."""
        client = WORKSPACE_ROOT / CANONICAL_CLIENT_REL
        self.assertTrue(
            client.exists(), f"canonical iShares client missing: {CANONICAL_CLIENT_REL}"
        )
        text = client.read_text(encoding="utf-8")
        self.assertIn("_session.get(", text, "canonical client no longer performs the HTTP GET")

    def test_no_shadow_ishares_http_outside_canonical_client(self):
        offenders: list[str] = []
        for root in SCAN_DIRS:
            if not root.exists():
                continue
            for py in root.rglob("*.py"):
                rel = py.relative_to(WORKSPACE_ROOT).as_posix()
                if rel == CANONICAL_CLIENT_REL:
                    continue
                text = py.read_text(encoding="utf-8", errors="replace")
                if not _file_uses_ishares_url(text):
                    continue
                offenders.extend(f"  {rel}:{ln}  {src}" for ln, src in _find_raw_http_lines(text))

        self.assertEqual(
            offenders,
            [],
            "Raw iShares HTTP detected outside ISharesClient. Route it through "
            "alphalens_pipeline.data.alt_data.ishares_client.get_default_ishares_client():\n"
            + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
