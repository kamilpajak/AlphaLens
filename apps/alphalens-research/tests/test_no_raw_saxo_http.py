"""Enforcement: no raw Saxo HTTP calls outside the canonical client.

Every Saxo OpenAPI call (OAuth ``/token`` + the read-only gateway probe) must
route through
:class:`alphalens_pipeline.data.alt_data.saxo_client.SaxoClient`. A shadow
client would fragment the request stream AND — far worse for a money system —
bypass the ``_redact`` boundary, risking a live brokerage refresh token in
journald.

Mirror of :mod:`tests.test_no_raw_openrouter_http`: conjunction logic — a file
is flagged only if it has BOTH a Saxo host fragment
(``logonvalidation.net`` / ``gateway.saxobank.com``) AND a raw HTTP-call shape
(``httpx.post``, ``requests.*``, ``urlopen``, ``aiohttp``). Positive controls
pin both detectors so they cannot rot to empty. Only the canonical client +
the token manager (which imports the client's classifier) are exempt.
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

# The canonical client itself MUST construct an httpx.Client to the Saxo hosts.
# The manager is exempt because it imports the client's classifier helpers in a
# module that legitimately references the hosts in docstrings (it never issues
# a raw call — it always goes through the injected SaxoClient).
EXEMPT_PATH_PREFIXES = (
    "apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_client.py",
    "apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_token_manager.py",
)

# Saxo host fragments — any reference to an auth/gateway host counts.
_URL_PATTERNS = (
    re.compile(r"\blogonvalidation\.net\b", re.IGNORECASE),
    re.compile(r"\bgateway\.saxobank\.com\b", re.IGNORECASE),
)

_HTTP_CALL_PATTERNS = (
    re.compile(r"\bhttpx\.(?:get|post|put|delete|patch|request)\s*\("),
    re.compile(r"\bhttpx\.Client\s*\("),
    re.compile(r"\brequests\.(?:get|post|put|delete|patch|request|Session)\s*\("),
    re.compile(r"\burlopen\s*\("),
    re.compile(r"\baiohttp\.(?:ClientSession|request|get|post)\s*\("),
)


def _has_url(text: str) -> bool:
    return any(p.search(text) for p in _URL_PATTERNS)


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


class TestNoRawSaxoHttp(unittest.TestCase):
    def test_url_pattern_locks_saxo_hosts(self) -> None:
        for sample in (
            "https://sim.logonvalidation.net/token",
            "https://live.logonvalidation.net/authorize",
            "https://gateway.saxobank.com/sim/openapi/port/v1/users/me",
            "# call gateway.saxobank.com for ...",
        ):
            self.assertTrue(_has_url(sample), f"URL detector missed: {sample!r}")
        for sample in (
            "https://api.openai.com/v1/chat/completions",
            "https://api.polygon.io/v2/reference/news",
            "https://sim.logonvalidation.example",  # different TLD
        ):
            self.assertFalse(_has_url(sample), f"URL detector false-positive: {sample!r}")

    def test_http_call_pattern_locks_shadow_shapes(self) -> None:
        for sample in (
            "r = httpx.post('https://sim.logonvalidation.net/token')",
            "c = httpx.Client(base_url='https://gateway.saxobank.com/openapi')",
            "r = requests.post('https://live.logonvalidation.net/token')",
            "s = requests.Session()",
            "urlopen('https://gateway.saxobank.com/openapi/...')",
        ):
            hits = _find_http_call_lines(sample)
            self.assertEqual(len(hits), 1, f"expected one hit: {sample!r}")
        for sample in (
            "client = SaxoClient(app_key='x')",
            "from alphalens_pipeline.data.alt_data.saxo_client import SaxoClient",
            "client.refresh_token(refresh_token=rt)",
            "self._http.post('/token', data=body)",  # canonical-client pattern
            "# httpx.post in a comment must never trip detection",
        ):
            hits = _find_http_call_lines(sample)
            self.assertEqual(len(hits), 0, f"expected zero hits: {sample!r} (got {hits})")

    def test_no_shadow_saxo_clients_outside_canonical(self) -> None:
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
                "Raw Saxo HTTP call / shadow client detected.\n"
                "Route the call through "
                "alphalens_pipeline.data.alt_data.saxo_client.SaxoClient "
                "(use get_default_saxo_client() if you don't have one to inject).\n"
                f"Offenders:\n{details}"
            )


if __name__ == "__main__":
    unittest.main()
