"""Tests for the canonical :class:`TelegramClient`.

Telegram Bot API delivers operator alerts from THREE live services
(edgar-detect, thematic-build, literature-scan). Routing every ``sendMessage``
through one client gives a single retry + credential-sanitisation seam, and lets
the ``test_no_raw_telegram_http`` enforcement test keep shadow ``requests.post``
calls out of the dispatch handlers.

These tests inject a fake ``requests.Session`` (no real network) and a no-op
``sleep`` so the retry path runs instantly.
"""

from __future__ import annotations

import logging
import unittest
from unittest.mock import MagicMock

import requests
from alphalens_pipeline.data.alt_data import telegram_client as tc


def _resp(status_code: int) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    if status_code >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status_code}")
    else:
        r.raise_for_status.return_value = None
    return r


def _client(session: MagicMock) -> tc.TelegramClient:
    return tc.TelegramClient("BOTTOKEN", session=session, sleep=lambda _s: None)


class TestSendMessageSuccess(unittest.TestCase):
    def test_posts_to_sendmessage_endpoint_with_payload(self):
        sess = MagicMock()
        sess.post.return_value = _resp(200)
        ok = _client(sess).send_message("CHATID", "hello", parse_mode="Markdown")
        self.assertTrue(ok)
        url = sess.post.call_args.args[0]
        self.assertEqual(url, "https://api.telegram.org/botBOTTOKEN/sendMessage")
        payload = sess.post.call_args.kwargs["json"]
        self.assertEqual(payload["chat_id"], "CHATID")
        self.assertEqual(payload["text"], "hello")
        self.assertEqual(payload["parse_mode"], "Markdown")

    def test_requires_chat_id(self):
        sess = MagicMock()
        with self.assertRaises(ValueError):
            _client(sess).send_message("", "x")

    def test_requires_bot_token_at_construction(self):
        with self.assertRaises(ValueError):
            tc.TelegramClient("")


class TestRetry(unittest.TestCase):
    def test_retries_on_429_then_succeeds(self):
        sess = MagicMock()
        sess.post.side_effect = [_resp(429), _resp(200)]
        self.assertTrue(_client(sess).send_message("C", "x"))
        self.assertEqual(sess.post.call_count, 2)

    def test_retries_on_503_then_succeeds(self):
        sess = MagicMock()
        sess.post.side_effect = [_resp(503), _resp(200)]
        self.assertTrue(_client(sess).send_message("C", "x"))

    def test_retries_on_connection_error_then_succeeds(self):
        sess = MagicMock()
        sess.post.side_effect = [requests.ConnectionError("down"), _resp(200)]
        self.assertTrue(_client(sess).send_message("C", "x"))

    def test_exhausted_transient_returns_false_not_raise(self):
        sess = MagicMock()
        sess.post.side_effect = [_resp(429), _resp(429), _resp(429)]
        self.assertFalse(_client(sess).send_message("C", "x"))
        self.assertEqual(sess.post.call_count, 3)

    def test_permanent_4xx_returns_false_without_retry(self):
        sess = MagicMock()
        sess.post.return_value = _resp(400)
        self.assertFalse(_client(sess).send_message("C", "x"))
        self.assertEqual(sess.post.call_count, 1)  # no retry on a permanent 400


class TestCredentialSafety(unittest.TestCase):
    def test_bot_token_never_appears_in_logs(self):
        sess = MagicMock()
        # requests embeds the URL (with the token) in the exception repr.
        sess.post.side_effect = requests.ConnectionError(
            "failed for url: https://api.telegram.org/botBOTTOKEN/sendMessage"
        )
        with self.assertLogs(tc.__name__, level=logging.ERROR) as cm:
            ok = _client(sess).send_message("C", "x")
        self.assertFalse(ok)
        joined = "\n".join(cm.output)
        self.assertNotIn("BOTTOKEN", joined)
        self.assertIn("***", joined)


if __name__ == "__main__":
    unittest.main()
