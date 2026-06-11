"""Enforcement: no raw Perplexity HTTP calls outside the canonical client.

Every Perplexity call must route through
:class:`alphalens_pipeline.literature_scanner.perplexity_client.PerplexityClient`
(Bearer auth, the chat/completions request shape, web_search_options). PR-7a
(#507) added the scuttlebutt layer as a second consumer; this test keeps the
request stream single-sourced so a future shadow client can't fragment it.

A file is flagged only if it contains BOTH an ``api.perplexity.ai`` URL fragment
AND a raw HTTP-call shape (``requests.post``, ``httpx.post``, ``urlopen``, ...).
The canonical client itself is exempt — it MUST POST to the host.

Mirror of :mod:`tests.test_no_raw_openrouter_http`, :mod:`tests.test_no_raw_sec_http`,
etc. — same structure (positive control + negative scan).
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

# The canonical client MUST POST to the Perplexity host — exempt it.
EXEMPT_PATH_PREFIXES = (
    "apps/alphalens-pipeline/alphalens_pipeline/literature_scanner/perplexity_client.py",
)

_URL_PATTERN = re.compile(r"\bapi\.perplexity\.ai\b", re.IGNORECASE)

_HTTP_CALL_PATTERNS = (
    re.compile(r"\bhttpx\.(?:get|post|put|delete|patch|request)\s*\("),
    re.compile(r"\bhttpx\.Client\s*\("),
    re.compile(r"\brequests\.(?:get|post|put|delete|patch|request|Session)\s*\("),
    re.compile(r"\burlopen\s*\("),
    re.compile(r"\baiohttp\.(?:ClientSession|request|get|post)\s*\("),
    re.compile(r"\bOpenAI\s*\("),
    re.compile(r"\bAsyncOpenAI\s*\("),
)


def _has_url(text: str) -> bool:
    return bool(_URL_PATTERN.search(text))


def _find_http_call_lines(text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        for pattern in _HTTP_CALL_PATTERNS:
            if pattern.search(line):
                hits.append((lineno, line.rstrip()))
                break
    return hits


def _path_is_exempt(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in EXEMPT_PATH_PREFIXES)


class TestNoRawPerplexityHttp(unittest.TestCase):
    def test_url_pattern_locks_perplexity_host(self):
        """Positive control: if this regex rots to empty the scan silently passes."""
        for sample in [
            "https://api.perplexity.ai/chat/completions",
            "api.perplexity.ai",
            'API_URL = "https://api.perplexity.ai/chat/completions"',
        ]:
            self.assertTrue(_has_url(sample), f"URL detector missed: {sample!r}")
        for sample in [
            "https://api.openai.com/v1/chat/completions",
            "https://openrouter.ai/api/v1",
        ]:
            self.assertFalse(_has_url(sample), f"URL detector false-positive: {sample!r}")

    def test_http_call_pattern_locks_shadow_shapes(self):
        for sample in [
            "r = requests.post('https://api.perplexity.ai/chat/completions')",
            "resp = httpx.post('https://api.perplexity.ai/...')",
            "urlopen('https://api.perplexity.ai/...')",
        ]:
            self.assertEqual(len(_find_http_call_lines(sample)), 1, sample)
        for sample in [
            "client = PerplexityClient(api_key='x')",
            "answer = client.ask(query, search_context_size='medium')",
            "# requests.post in a comment must never trip detection",
        ]:
            self.assertEqual(len(_find_http_call_lines(sample)), 0, sample)

    def test_no_shadow_perplexity_clients_outside_canonical(self):
        offenders: list[tuple[str, list[tuple[int, str]]]] = []
        for root in SCAN_DIRS:
            if not root.exists():
                continue
            for py in root.rglob("*.py"):
                rel = py.relative_to(WORKSPACE_ROOT).as_posix()
                if _path_is_exempt(rel):
                    continue
                text = py.read_text(encoding="utf-8", errors="replace")
                if not _has_url(text):
                    continue
                hits = _find_http_call_lines(text)
                if hits:
                    offenders.append((rel, hits))
        if offenders:
            details = "\n".join(
                f"  {p}:\n" + "\n".join(f"    line {ln}: {src}" for ln, src in hits)
                for p, hits in offenders
            )
            self.fail(
                "Raw Perplexity HTTP call / shadow client detected.\n"
                "Route the call through "
                "alphalens_pipeline.literature_scanner.perplexity_client.PerplexityClient.\n"
                f"Offenders:\n{details}"
            )


if __name__ == "__main__":
    unittest.main()
