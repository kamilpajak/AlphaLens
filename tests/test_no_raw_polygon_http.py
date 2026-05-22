"""Enforcement: no raw Polygon HTTP outside the canonical client.

The 2026-05-22 vendor-client consolidation routes every Polygon REST fetch
in the repo (thematic news ingest, press verification, short-interest
features, options-contracts reference) through
:class:`alphalens.data.alt_data.polygon_client.PolygonClient`.

Polygon's Starter-tier rate limit (5 req/min) is per-API-key, so any
uncoordinated shadow client drains budget from every other consumer in
the process — a single forgotten ``urlopen("https://api.polygon.io/...")``
can trigger the 429 cascade that broke press-gate verification in
production on 2026-05-22 (see
``docs/research/thematic_verification_gate_audit_2026_05_22.md``).

This test fails red if anyone reintroduces a raw Polygon HTTP call
(defined as ``urllib.request.urlopen`` / ``urllib.request.Request`` /
``requests.get(`` / ``httpx.get(`` / ``aiohttp.ClientSession``) in a
file that also mentions a Polygon URL fragment.

Mirror of :mod:`tests.test_no_raw_sec_http` and :mod:`tests.test_no_raw_av_http`;
same conjunction logic (URL fragment AND raw HTTP pattern, both in the
same file). Archive layer (``alphalens/archive/``) is exempt per ADR 0005
— the closed-layer anti-pattern catalog freezes shadow clients
intentionally; migrating them would defeat the catalog purpose.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = (
    REPO_ROOT / "alphalens",
    REPO_ROOT / "alphalens_cli",
    REPO_ROOT / "scripts",
)

# The canonical client itself — only file allowed to make raw Polygon HTTP.
CANONICAL_CLIENT_REL = "alphalens/data/alt_data/polygon_client.py"

# Path-prefix exemption for the closed-layer anti-pattern catalog (ADR 0005).
# These shadow clients are frozen as historical reference; the enforcement
# test should not chase them. Same exemption shape as
# ``tests/test_no_raw_gemini_sdk.py``.
EXEMPT_PATH_PREFIXES = ("alphalens/archive/",)

# Fragments that uniquely identify Polygon endpoints used by the project.
POLYGON_URL_FRAGMENTS = (
    "api.polygon.io",
    "polygon.io",
)

# Module-level patterns that constitute a raw HTTP call. Word-boundary +
# call-shape ensures "burning further requests." in a docstring doesn't
# match, but ``requests.get(``, ``urlopen(``, ``httpx.post(``,
# ``aiohttp.ClientSession`` all do. The canonical client uses
# ``self._session.get(...)`` (injected); ``self.`` defeats the word boundary
# on the left, so it's exempt.
RAW_HTTP_PATTERNS = (
    re.compile(r"(?<![\w.])urlopen\("),
    re.compile(r"(?<![\w.])urllib\.request\.\w+"),
    re.compile(r"(?<![\w.])requests\.\w+\("),
    re.compile(r"(?<![\w.])httpx\.\w+\("),
    re.compile(r"(?<![\w.])aiohttp\.\w+"),
)


def _file_uses_polygon_url(text: str) -> bool:
    return any(frag in text for frag in POLYGON_URL_FRAGMENTS)


def _is_exempt(rel_path: str) -> bool:
    if rel_path == CANONICAL_CLIENT_REL:
        return True
    return any(rel_path.startswith(prefix) for prefix in EXEMPT_PATH_PREFIXES)


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


class TestNoRawPolygonHttp(unittest.TestCase):
    def test_detection_regex_locks_shadow_patterns(self):
        """Positive control on the detection regex itself. The negative test
        below could silently pass if the regex / URL-fragment lists rot to
        empty; this test asserts that each shape we MEAN to catch (bare
        urlopen, urllib.request.urlopen, requests.get, httpx.post,
        aiohttp.ClientSession) is flagged, and that known-safe shapes
        (canonical DI ``self._session.get`` and docstring prose) are NOT.
        """
        shadow_samples = [
            'with urlopen("https://api.polygon.io/v2/reference/news") as resp:',
            'urllib.request.urlopen("https://api.polygon.io/v2/reference/news")',
            'resp = requests.get("https://api.polygon.io/v2/reference/news")',
            'await httpx.post("https://api.polygon.io/v2/reference/news")',
            "aiohttp.ClientSession()  # https://api.polygon.io/v2/reference/news",
        ]
        for sample in shadow_samples:
            hits = _find_raw_http_lines(sample)
            self.assertEqual(len(hits), 1, f"expected exactly one hit on shadow sample: {sample!r}")

        safe_samples = [
            "with self._session.get(url, headers=headers) as resp:",
            "return self._session.get(url, headers=headers, params=params, timeout=self._timeout)",
            '"""...persistent rate-limit (retry exhausted), the exception """',
            "# urlopen line in a comment must never trip detection",
            "from alphalens.data.alt_data.polygon_client import PolygonClient",
            "client = get_default_polygon_client()",
        ]
        for sample in safe_samples:
            hits = _find_raw_http_lines(sample)
            self.assertEqual(
                len(hits), 0, f"expected zero hits on safe sample: {sample!r} (got {hits})"
            )

        # And the URL fragment list must cover the Polygon base URL itself.
        self.assertTrue(_file_uses_polygon_url('"https://api.polygon.io/v2/reference/news"'))

    # Activated by the migration commit. This commit introduces the canonical
    # PolygonClient + tests + enforcement scaffold but does not yet migrate the
    # three shadow callers (polygon_news.py, recent_press.py,
    # scripts/build_optionable_universe.py). The decorator is removed in the
    # follow-up migration commit. Keep the test body so the regex + path-walk
    # logic still gets exercised end-to-end in CI; expectedFailure flags the
    # known shadow set as "expected" without failing the suite.
    @unittest.expectedFailure
    def test_no_shadow_polygon_http_outside_canonical_client(self):
        offenders: list[tuple[str, int, str]] = []
        for root in SCAN_DIRS:
            if not root.exists():
                continue
            for py in root.rglob("*.py"):
                rel = py.relative_to(REPO_ROOT).as_posix()
                if _is_exempt(rel):
                    continue
                text = py.read_text(encoding="utf-8", errors="replace")
                if not _file_uses_polygon_url(text):
                    continue
                for lineno, src in _find_raw_http_lines(text):
                    offenders.append((rel, lineno, src))

        if offenders:
            details = "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in offenders)
            self.fail(
                "Raw Polygon HTTP detected outside PolygonClient.\n"
                "Route the call through "
                "alphalens.data.alt_data.polygon_client (use\n"
                "get_default_polygon_client() if you don't have one to inject).\n"
                f"Offenders:\n{details}"
            )


if __name__ == "__main__":
    unittest.main()
