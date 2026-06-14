"""Telegram Bot API client — single canonical entry point for outbound alerts.

Three live services deliver operator alerts via Telegram ``sendMessage``: the
edgar-detector dispatch (every 15 min), the thematic build (6x daily), and the
literature scan. Routing every send through one client gives the whole process
ONE retry + credential-sanitisation seam, and lets the
``test_no_raw_telegram_http`` enforcement test keep raw ``requests.post`` calls
out of the dispatch handlers (the same shadow-client doctrine as the SEC / AV /
Polygon / OpenRouter / yfinance clients).

Mirrors the other canonical clients structurally: DI (``session`` / ``sleep``),
bounded retry that NEVER raises to the caller — :meth:`send_message` collapses to
``False`` so a failed alert can't crash the live pipeline. Telegram has no
single-key quota worth throttling (a handful of alerts per fire), so there is no
min-interval throttle, only the transient retry safety net.

Credential safety: the bot token is embedded in the request URL
(``api.telegram.org/bot{TOKEN}/sendMessage``), and ``requests`` puts that URL in
its exception repr — so every logged message is sanitised to replace the token
with ``***`` before it reaches the logs.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import requests

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"
# Telegram returns 429 (Too Many Requests) and standard 5xx on transient
# trouble; everything else 4xx (400 bad payload, 401 bad token, 403 blocked) is
# permanent and must NOT be retried.
_TRANSIENT_STATUS = (429, 500, 502, 503, 504)


class TelegramError(RuntimeError):
    """Non-transient Telegram delivery failure.

    NEVER raised to the caller — :meth:`TelegramClient.send_message` collapses to
    ``False`` so a failed alert can't crash the live edgar-detect / thematic /
    literature pipelines — but defined for typed internal signalling and parity
    with the other canonical clients' ``*Error`` types.
    """


class TelegramClient:
    """Canonical client for the Telegram Bot API.

    One instance per process (constructed by the dispatch handler from the bot
    token). The token is held here and woven into the ``sendMessage`` URL so no
    caller has to build it.
    """

    _MAX_ATTEMPTS = 3  # 1 + 2 retries, like the SEC / yfinance clients
    _BACKOFFS = (1, 3)  # seconds; Telegram 429s carry a retry_after but are rare here

    def __init__(
        self,
        bot_token: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not bot_token:
            raise ValueError("bot_token required")
        self._bot_token = bot_token
        self._session = session or requests.Session()
        self._timeout = timeout
        self._sleep = sleep
        self._send_url = f"{_API_BASE}/bot{bot_token}/sendMessage"

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str = "Markdown",
        disable_web_page_preview: bool = False,
    ) -> bool:
        """Send ``text`` to ``chat_id``; return ``True`` on success, ``False`` on
        failure. NEVER raises — a delivery failure must not crash the caller.

        Retries the transient cases (network blip, HTTP 429/5xx) with a short
        backoff up to ``_MAX_ATTEMPTS``; a permanent 4xx (bad payload / token /
        blocked chat) returns ``False`` immediately. All failure logs are
        token-sanitised.
        """
        if not chat_id:
            raise ValueError("chat_id required")
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        for attempt in range(self._MAX_ATTEMPTS):
            try:
                resp = self._session.post(self._send_url, json=payload, timeout=self._timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if self._retry(attempt, "network error", exc):
                    continue
                return False
            if resp.status_code in _TRANSIENT_STATUS:
                if self._retry(attempt, f"HTTP {resp.status_code}", None):
                    continue
                return False
            try:
                resp.raise_for_status()
            except requests.HTTPError as exc:
                logger.error("Telegram send failed (permanent): %s", self._sanitize(str(exc)))
                return False
            return True
        return False

    # ----- internals -----

    def _retry(self, attempt: int, reason: str, exc: Exception | None) -> bool:
        """Sleep + signal retry (return ``True``) unless attempts are exhausted,
        in which case log a sanitised error and return ``False``."""
        if attempt >= self._MAX_ATTEMPTS - 1:
            detail = f": {self._sanitize(str(exc))}" if exc is not None else ""
            logger.error(
                "Telegram send failed after %d attempts (%s)%s",
                self._MAX_ATTEMPTS,
                reason,
                detail,
            )
            return False
        self._sleep(self._BACKOFFS[min(attempt, len(self._BACKOFFS) - 1)])
        return True

    def _sanitize(self, message: str) -> str:
        """Strip the bot token from a message before it reaches the logs (the
        token lives in the request URL that ``requests`` embeds in its repr)."""
        return message.replace(self._bot_token, "***")


__all__ = ["TelegramClient", "TelegramError"]
