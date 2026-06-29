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

    def test_exotic_request_exception_returns_false_not_raise(self):
        """An exotic requests failure (not Timeout/ConnectionError) must NOT
        propagate — it would crash the live dispatch caller and could leak the
        bot-token URL in its repr. Permanent: no retry, return False."""
        sess = MagicMock()
        sess.post.side_effect = requests.TooManyRedirects("too many redirects")
        self.assertFalse(_client(sess).send_message("C", "x"))
        self.assertEqual(sess.post.call_count, 1)


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


class TestChunking(unittest.TestCase):
    """Telegram's Bot API rejects a ``sendMessage`` whose ``text`` exceeds 4096
    characters. The literature-scan digest is the only caller that can cross
    that ceiling, so the canonical client splits a long message into ≤4096-char
    sends (preferring newline boundaries) and reports success only if EVERY
    chunk lands.
    """

    @staticmethod
    def _sent_texts(sess: MagicMock) -> list[str]:
        return [c.kwargs["json"]["text"] for c in sess.post.call_args_list]

    def test_short_text_sends_single_message(self):
        sess = MagicMock()
        sess.post.return_value = _resp(200)
        ok = _client(sess).send_message("C", "short body")
        self.assertTrue(ok)
        self.assertEqual(sess.post.call_count, 1)

    def test_long_text_split_into_multiple_messages_each_within_limit(self):
        sess = MagicMock()
        sess.post.return_value = _resp(200)
        text = "x" * 9000  # > 2 * 4096
        ok = _client(sess).send_message("C", text)
        self.assertTrue(ok)
        sent = self._sent_texts(sess)
        self.assertGreater(len(sent), 1)
        for chunk in sent:
            self.assertLessEqual(len(chunk), tc.TelegramClient._MAX_MESSAGE_CHARS)

    def test_split_preserves_full_content_when_joined(self):
        sess = MagicMock()
        sess.post.return_value = _resp(200)
        # multi-line body comfortably over one message
        text = "\n".join(f"line {i} " + "y" * 50 for i in range(200))
        _client(sess).send_message("C", text)
        self.assertEqual("".join(self._sent_texts(sess)), text)

    def test_split_prefers_newline_boundary(self):
        sess = MagicMock()
        sess.post.return_value = _resp(200)
        limit = tc.TelegramClient._MAX_MESSAGE_CHARS
        # a newline sits 10 chars under the limit, then a long unbroken tail.
        head = "a" * (limit - 10)
        text = head + "\n" + "b" * 200
        _client(sess).send_message("C", text)
        sent = self._sent_texts(sess)
        # first chunk must end at the newline, not mid-run at the hard limit.
        self.assertEqual(sent[0], head + "\n")
        self.assertEqual(sent[1], "b" * 200)

    def test_all_chunks_succeed_returns_true(self):
        sess = MagicMock()
        sess.post.return_value = _resp(200)
        self.assertTrue(_client(sess).send_message("C", "z" * 9000))

    def test_one_chunk_failure_returns_false(self):
        sess = MagicMock()
        # first chunk ok, second chunk permanent 400 → overall False
        sess.post.side_effect = [_resp(200), _resp(400)]
        self.assertFalse(_client(sess).send_message("C", "z" * 5000))

    def test_each_chunk_carries_parse_mode(self):
        sess = MagicMock()
        sess.post.return_value = _resp(200)
        _client(sess).send_message("C", "z" * 9000, parse_mode="Markdown")
        for call in sess.post.call_args_list:
            self.assertEqual(call.kwargs["json"]["parse_mode"], "Markdown")


if __name__ == "__main__":
    unittest.main()
