"""Enforcement: no raw OpenRouter HTTP calls outside the canonical client.

PR-G (2026-05-30) routed the thematic pipeline's two LLM call sites
(extract Flash + mapper Pro + brief Pro/Flash) through
:class:`alphalens_pipeline.data.alt_data.openrouter_client.OpenRouterClient`.
The canonical client centralises Bearer auth, the OpenAI-compatible
request shape, response-shape translation (``.text`` matching Gemini),
finish_reason mapping, and the long-lived ``httpx.Client`` (one TCP /
TLS keepalive pool per process).

A future shadow client would fragment that surface and (more painfully)
break the OpenRouter dashboard cost-attribution which depends on the
``HTTP-Referer`` + ``X-Title`` headers the canonical client sets.

This test fails red if any file under ``alphalens_pipeline/`` /
``alphalens_cli/`` / ``alphalens_research/`` / ``scripts/`` contains BOTH:
* an ``openrouter.ai`` URL fragment AND
* a raw HTTP-call pattern (``httpx.post``, ``requests.post``, ``urlopen``,
  ``aiohttp``, ``openai.OpenAI(`` with OpenRouter base_url etc.)

Mirror of :mod:`tests.test_no_raw_av_http`,
:mod:`tests.test_no_raw_sec_http`, :mod:`tests.test_no_raw_gemini_sdk`,
:mod:`tests.test_no_raw_polygon_http` — same structure (positive
control + negative scan).
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
# enforcement. The canonical client itself MUST POST to openrouter.ai.
EXEMPT_PATH_PREFIXES = (
    "apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/openrouter_client.py",
)

# OpenRouter URL fragments — any reference to the API host counts.
_URL_PATTERN = re.compile(r"\bopenrouter\.ai\b", re.IGNORECASE)

# Raw HTTP call shapes — anything that would issue an actual request
# bypassing the canonical client's httpx.Client pool + headers.
# Word-boundary forms so prose mentions don't false-positive.
_HTTP_CALL_PATTERNS = (
    re.compile(r"\bhttpx\.(?:get|post|put|delete|patch|request)\s*\("),
    re.compile(r"\bhttpx\.Client\s*\("),
    re.compile(r"\brequests\.(?:get|post|put|delete|patch|request|Session)\s*\("),
    re.compile(r"\burlopen\s*\("),
    re.compile(r"\baiohttp\.(?:ClientSession|request|get|post)\s*\("),
    # OpenAI SDK pointed at OpenRouter base_url is the most plausible
    # shadow shape (drop-in client, but bypasses our header + retry
    # surface). Flag any OpenAI SDK construction in a file that also
    # mentions openrouter.ai.
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


class TestNoRawOpenRouterHttp(unittest.TestCase):
    """Conjunction logic: a file is flagged only if it has BOTH an
    openrouter.ai URL fragment AND a raw HTTP call shape. This avoids
    false-positives on files that just mention OpenRouter in prose, or
    files that use httpx/requests for unrelated APIs (e.g. Polygon).
    """

    def test_url_pattern_locks_openrouter_host(self):
        """Positive control on the URL detector. If this regex rots to
        empty, the conjunction test below silently passes — pin it."""
        url_samples = [
            "https://openrouter.ai/api/v1/chat/completions",
            "openrouter.ai",
            'base_url="https://openrouter.ai/api/v1"',
            "# call openrouter.ai/api/v1 for ...",  # comments still URL-positive
        ]
        for sample in url_samples:
            self.assertTrue(_has_url(sample), f"URL detector missed: {sample!r}")

        non_url_samples = [
            "https://api.openai.com/v1/chat/completions",
            "https://api.anthropic.com",
            "https://api.polygon.io/v2/reference/news",
        ]
        for sample in non_url_samples:
            self.assertFalse(_has_url(sample), f"URL detector false-positive: {sample!r}")

    def test_http_call_pattern_locks_shadow_shapes(self):
        """Positive control on the HTTP-call detector. Same rationale as
        above — without it, a regex that rots to empty silently lets
        shadow shapes through."""
        shadow_samples = [
            "response = httpx.post('https://openrouter.ai/api/v1/chat/completions')",
            "client = httpx.Client(base_url='https://openrouter.ai/api/v1')",
            "r = requests.post('https://openrouter.ai/api/v1/...')",
            "s = requests.Session()",
            "from openai import OpenAI; c = OpenAI(base_url='https://openrouter.ai/api/v1')",
            "from openai import AsyncOpenAI; c = AsyncOpenAI(base_url='...')",
            "urlopen('https://openrouter.ai/api/v1/...')",
        ]
        for sample in shadow_samples:
            hits = _find_http_call_lines(sample)
            self.assertEqual(len(hits), 1, f"expected one hit on shadow sample: {sample!r}")

        safe_samples = [
            "client = OpenRouterClient(api_key='x')",
            "from alphalens_pipeline.data.alt_data.openrouter_client import OpenRouterClient",
            "client.generate_content(model=m, contents=p)",
            "self._http.post('/chat/completions', json=body)",  # canonical-client pattern uses self._http
            '"""Docstring mentioning httpx.post for prose context."""',
            "# httpx.post in a comment must never trip detection",
        ]
        for sample in safe_samples:
            hits = _find_http_call_lines(sample)
            self.assertEqual(
                len(hits), 0, f"expected zero hits on safe sample: {sample!r} (got {hits})"
            )

    def test_no_shadow_openrouter_clients_outside_canonical(self):
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
                    # File doesn't mention OpenRouter at all — by
                    # construction it can't be a shadow client.
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
                "Raw OpenRouter HTTP call / shadow client detected.\n"
                "Route the call through "
                "alphalens_pipeline.data.alt_data.openrouter_client.OpenRouterClient "
                "(use get_default_openrouter_client() if you don't have one to inject).\n"
                f"Offenders:\n{details}"
            )


if __name__ == "__main__":
    unittest.main()
