"""Enforcement: no raw Saxo HTTP outside the canonical client.

The broker-agnostic execution layer (ADR 0014) routes every Saxo OpenAPI call
through :class:`alphalens_pipeline.brokers.saxo.client.SaxoClient` — the
surface that carries the SIM-only structural rail, the Bearer-token discipline,
the 0.5s throttle, and the ``x-request-id`` idempotency header P2's order
dedup depends on — and every OAuth token-endpoint call through
:class:`alphalens_pipeline.brokers.saxo.oauth.SaxoAuthClient` (P4), which
carries the same SIM-only rail for the authentication host plus the
secrets-hygiene discipline. A shadow client would bypass the LIVE-URL refusal,
which is a safety rail, not just a quota concern.

This test fails red if anyone reintroduces a raw Saxo HTTP call (defined as
``urllib.request.urlopen`` / ``urllib.request.Request`` / ``requests.get(`` /
``httpx.get(`` / ``aiohttp.ClientSession``) in a file that also mentions a
Saxo URL fragment.

Mirror of :mod:`tests.test_no_raw_polygon_http`; same conjunction logic (URL
fragment AND raw HTTP pattern, both in the same file).
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

# The canonical Saxo HTTP surfaces — the ONLY files allowed to make raw Saxo
# HTTP: the gateway client (reads/writes) and the P4 OAuth token-endpoint
# transport (authorize/exchange/refresh).
CANONICAL_CLIENT_RELS = (
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/client.py",
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/oauth.py",
)

# Path-prefix exemption (empty — no legacy Saxo code survives ADR 0012).
EXEMPT_PATH_PREFIXES: tuple[str, ...] = ()

# Fragments that uniquely identify Saxo endpoints used by the project.
SAXO_URL_FRAGMENTS = (
    "gateway.saxobank.com",
    "logonvalidation.net",
    "saxobank.com/sim/openapi",
)

# Module-level patterns that constitute a raw HTTP call. Word-boundary +
# call-shape ensures docstring prose doesn't match, but ``requests.get(``,
# ``urlopen(``, ``httpx.post(``, ``aiohttp.ClientSession`` all do. The
# canonical client uses ``self._session.get(...)`` (injected); ``self.``
# defeats the word boundary on the left, so it's exempt.
RAW_HTTP_PATTERNS = (
    re.compile(r"(?<![\w.])urlopen\("),
    re.compile(r"(?<![\w.])urllib\.request\.\w+"),
    re.compile(r"(?<![\w.])requests\.\w+\("),
    re.compile(r"(?<![\w.])httpx\.\w+\("),
    re.compile(r"(?<![\w.])aiohttp\.\w+"),
)


def _file_uses_saxo_url(text: str) -> bool:
    return any(frag in text for frag in SAXO_URL_FRAGMENTS)


def _is_exempt(rel_path: str) -> bool:
    if rel_path in CANONICAL_CLIENT_RELS:
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


class TestNoRawSaxoHttp(unittest.TestCase):
    def test_detection_regex_locks_shadow_patterns(self):
        """Positive control on the detection regex itself. The negative test
        below could silently pass if the regex / URL-fragment lists rot to
        empty; this test asserts that each shape we MEAN to catch is flagged,
        that known-safe shapes are NOT, and that the fragment list covers the
        real SIM base URL the canonical client uses.
        """
        shadow_samples = [
            'with urlopen("https://gateway.saxobank.com/sim/openapi/port/v1/users/me") as r:',
            'urllib.request.urlopen("https://gateway.saxobank.com/sim/openapi/port/v1/users/me")',
            'resp = requests.get("https://gateway.saxobank.com/sim/openapi/port/v1/users/me")',
            'await httpx.post("https://sim.logonvalidation.net/token")',
            "aiohttp.ClientSession()  # https://gateway.saxobank.com/sim/openapi",
        ]
        for sample in shadow_samples:
            hits = _find_raw_http_lines(sample)
            self.assertEqual(len(hits), 1, f"expected exactly one hit on shadow sample: {sample!r}")

        safe_samples = [
            "with self._session.get(url, headers=headers) as resp:",
            "return self._session.get(url, headers=headers, params=params, timeout=self._timeout)",
            "# urlopen line in a comment must never trip detection",
            "from alphalens_pipeline.brokers.saxo.client import SaxoClient",
            "client = get_default_saxo_client()",
        ]
        for sample in safe_samples:
            hits = _find_raw_http_lines(sample)
            self.assertEqual(
                len(hits), 0, f"expected zero hits on safe sample: {sample!r} (got {hits})"
            )

        # The fragment list must cover the canonical client's real SIM base URL
        # — this pin fails if either side drifts (URL change or fragment rot).
        from alphalens_pipeline.brokers.saxo.client import SIM_AUTH_BASE_URL, SIM_BASE_URL

        self.assertTrue(_file_uses_saxo_url(SIM_BASE_URL))
        self.assertTrue(_file_uses_saxo_url(SIM_AUTH_BASE_URL))

    def test_canonical_clients_exist(self):
        """Every exemption must point at a real file — otherwise the scan below
        would 'pass' while a canonical surface had been moved without updating
        this enforcement."""
        for rel in CANONICAL_CLIENT_RELS:
            with self.subTest(rel=rel):
                self.assertTrue((WORKSPACE_ROOT / rel).is_file())

    def test_no_shadow_saxo_http_outside_canonical_client(self):
        offenders: list[tuple[str, int, str]] = []
        for root in SCAN_DIRS:
            if not root.exists():
                continue
            for py in root.rglob("*.py"):
                rel = py.relative_to(WORKSPACE_ROOT).as_posix()
                if _is_exempt(rel):
                    continue
                text = py.read_text(encoding="utf-8", errors="replace")
                if not _file_uses_saxo_url(text):
                    continue
                for lineno, src in _find_raw_http_lines(text):
                    offenders.append((rel, lineno, src))

        if offenders:
            details = "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in offenders)
            self.fail(
                "Raw Saxo HTTP detected outside SaxoClient.\n"
                "Route the call through "
                "alphalens_pipeline.brokers.saxo.client (use\n"
                "get_default_saxo_client() if you don't have one to inject).\n"
                f"Offenders:\n{details}"
            )


if __name__ == "__main__":
    unittest.main()
