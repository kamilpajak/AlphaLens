"""Enforcement: no raw Telegram HTTP outside the canonical client.

Operator alerts are delivered via the Telegram Bot API from THREE live services
— the edgar-detector dispatch (every 15 min), the thematic build (6x daily), and
the literature scan. Every ``sendMessage`` goes through
:class:`alphalens_pipeline.data.alt_data.telegram_client.TelegramClient`, which
owns the bounded retry AND the credential sanitisation: the bot token lives in
the request URL (``api.telegram.org/bot{TOKEN}/sendMessage``) and ``requests``
embeds that URL in exception reprs, so a stray ``requests.post`` that logs an
error would leak the token. Routing through one client keeps that seam single.

This test fails red if a raw HTTP call (``urlopen`` / ``urllib.request`` /
``requests.*`` / ``httpx.*`` / ``aiohttp``) appears in a file that also mentions
a Telegram URL fragment. Mirror of :mod:`tests.test_no_raw_polygon_http` with a
positive control so the detection regex / URL-fragment list cannot rot to empty.
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

# The canonical client itself — only file allowed to make raw Telegram HTTP.
CANONICAL_CLIENT_REL = "apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/telegram_client.py"

# Fragments that uniquely identify the Telegram Bot API.
TELEGRAM_URL_FRAGMENTS = (
    "api.telegram.org",
    "telegram.org/bot",
)

RAW_HTTP_PATTERNS = (
    re.compile(r"(?<![\w.])urlopen\("),
    re.compile(r"(?<![\w.])urllib\.request\.\w+"),
    re.compile(r"(?<![\w.])requests\.\w+\("),
    re.compile(r"(?<![\w.])httpx\.\w+\("),
    re.compile(r"(?<![\w.])aiohttp\.\w+"),
)


def _file_uses_telegram_url(text: str) -> bool:
    return any(frag in text for frag in TELEGRAM_URL_FRAGMENTS)


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


class TestNoRawTelegramHttp(unittest.TestCase):
    def test_detection_regex_locks_shadow_patterns(self):
        """Positive control on the detection regex + URL-fragment list."""
        shadow_samples = [
            'resp = requests.post("https://api.telegram.org/bot123/sendMessage", json=p)',
            'urllib.request.urlopen("https://api.telegram.org/bot123/sendMessage")',
            'with urlopen("https://api.telegram.org/bot123/sendMessage") as r:',
            'await httpx.post("https://api.telegram.org/bot123/sendMessage")',
            "aiohttp.ClientSession()  # https://api.telegram.org/bot123/sendMessage",
        ]
        for sample in shadow_samples:
            self.assertEqual(
                len(_find_raw_http_lines(sample)), 1, f"missed shadow sample: {sample!r}"
            )

        safe_samples = [
            "resp = self._session.post(self._send_url, json=payload, timeout=self._timeout)",
            "self._client.send_message(self.chat_id, text)",
            "# requests.post to api.telegram.org in a comment must not trip detection",
            "from alphalens_pipeline.data.alt_data.telegram_client import TelegramClient",
        ]
        for sample in safe_samples:
            self.assertEqual(len(_find_raw_http_lines(sample)), 0, f"false positive: {sample!r}")

        self.assertTrue(
            _file_uses_telegram_url(
                'self._send_url = f"https://api.telegram.org/bot{token}/sendMessage"'
            )
        )

    def test_canonical_client_is_present_and_scanned(self):
        """Anti-rot: the canonical client must exist and itself reference the
        Telegram URL (otherwise the exemption guards nothing)."""
        client = WORKSPACE_ROOT / CANONICAL_CLIENT_REL
        self.assertTrue(
            client.exists(), f"canonical Telegram client missing: {CANONICAL_CLIENT_REL}"
        )
        text = client.read_text(encoding="utf-8")
        self.assertTrue(
            _file_uses_telegram_url(text), "canonical client no longer references the Telegram URL"
        )

    def test_no_shadow_telegram_http_outside_canonical_client(self):
        offenders: list[str] = []
        for root in SCAN_DIRS:
            if not root.exists():
                continue
            for py in root.rglob("*.py"):
                rel = py.relative_to(WORKSPACE_ROOT).as_posix()
                if rel == CANONICAL_CLIENT_REL:
                    continue
                text = py.read_text(encoding="utf-8", errors="replace")
                if not _file_uses_telegram_url(text):
                    continue
                offenders.extend(f"  {rel}:{ln}  {src}" for ln, src in _find_raw_http_lines(text))

        self.assertEqual(
            offenders,
            [],
            "Raw Telegram HTTP detected outside TelegramClient. Route it through "
            "alphalens_pipeline.data.alt_data.telegram_client.TelegramClient:\n"
            + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
