import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch


def _classified(ticker="AAPL", severity=None, relevance=None, action=None, form=None, items=None):
    from alphalens_research.watchdog.classifier import Action, ClassifiedEvent, Severity
    from alphalens_research.watchdog.portfolio import Relevance
    from alphalens_research.watchdog.types import Event, FormType

    return ClassifiedEvent(
        event=Event(
            ticker=ticker,
            form_type=form or FormType.FORM_8K,
            accession_number="ACC-001",
            filed_at=datetime(2026, 4, 17, 12, 0, tzinfo=UTC),
            url="https://sec.gov/filing",
            raw_data={"items": items or ["4.02"]},
        ),
        severity=severity or Severity.HIGH,
        relevance=relevance or Relevance.HELD,
        action=action or Action.APPROVAL,
    )


class TestAlertHandlerABC(unittest.TestCase):
    def test_abstract_handle_method_required(self):
        from alphalens_research.watchdog.dispatch.handlers.base import AlertHandler

        class BadHandler(AlertHandler):
            pass

        with self.assertRaises(TypeError):
            BadHandler()  # type: ignore[abstract]


class TestTelegramHandler(unittest.TestCase):
    @patch("alphalens_research.watchdog.dispatch.handlers.telegram.requests.post")
    def test_send_uses_bot_api_endpoint(self, mock_post):
        from alphalens_research.watchdog.dispatch.handlers.telegram import TelegramHandler

        mock_post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())
        handler = TelegramHandler(bot_token="BOTTOKEN", chat_id="CHATID")
        handler.handle(_classified())

        self.assertTrue(mock_post.called)
        url = mock_post.call_args.args[0]
        self.assertIn("api.telegram.org/botBOTTOKEN/sendMessage", url)

    @patch("alphalens_research.watchdog.dispatch.handlers.telegram.requests.post")
    def test_message_includes_severity_and_url(self, mock_post):
        from alphalens_research.watchdog.classifier import Severity
        from alphalens_research.watchdog.dispatch.handlers.telegram import TelegramHandler

        mock_post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())
        handler = TelegramHandler(bot_token="T", chat_id="C")
        handler.handle(_classified(severity=Severity.HIGH))

        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args.kwargs.get("data")
        self.assertIsNotNone(payload)
        text = payload["text"]
        self.assertIn("HIGH", text)
        self.assertIn("AAPL", text)
        self.assertIn("https://sec.gov/filing", text)

    @patch("alphalens_research.watchdog.dispatch.handlers.telegram.requests.post")
    def test_handles_api_error_without_raising(self, mock_post):
        import requests as req_module
        from alphalens_research.watchdog.dispatch.handlers.telegram import TelegramHandler

        mock_post.side_effect = req_module.ConnectionError("down")
        handler = TelegramHandler(bot_token="T", chat_id="C")
        handler.handle(_classified())  # should not raise

    def test_requires_bot_token_and_chat_id(self):
        from alphalens_research.watchdog.dispatch.handlers.telegram import TelegramHandler

        with self.assertRaises(ValueError):
            TelegramHandler(bot_token="", chat_id="C")
        with self.assertRaises(ValueError):
            TelegramHandler(bot_token="T", chat_id="")


class TestDigestHandler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "digest.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_handle_adds_event_to_buffer(self):
        from alphalens_research.watchdog.dispatch.handlers.digest import DigestHandler

        handler = DigestHandler(db_path=self.db_path, sender=MagicMock())
        handler.handle(_classified(ticker="AAPL"))
        handler.handle(_classified(ticker="MSFT"))

        self.assertEqual(len(handler.buffered()), 2)

    def test_flush_sends_combined_message(self):
        from alphalens_research.watchdog.dispatch.handlers.digest import DigestHandler

        sender = MagicMock()
        handler = DigestHandler(db_path=self.db_path, sender=sender)
        handler.handle(_classified(ticker="AAPL"))
        handler.handle(_classified(ticker="MSFT"))
        handler.flush()

        sender.send_message.assert_called_once()
        msg = sender.send_message.call_args.args[0]
        self.assertIn("AAPL", msg)
        self.assertIn("MSFT", msg)

    def test_buffer_persists_across_instances(self):
        from alphalens_research.watchdog.dispatch.handlers.digest import DigestHandler

        h1 = DigestHandler(db_path=self.db_path, sender=MagicMock())
        h1.handle(_classified(ticker="NVDA"))
        h1.close()

        h2 = DigestHandler(db_path=self.db_path, sender=MagicMock())
        buffered = h2.buffered()
        tickers = [e.event.ticker for e in buffered]
        self.assertIn("NVDA", tickers)

    def test_flush_clears_buffer(self):
        from alphalens_research.watchdog.dispatch.handlers.digest import DigestHandler

        handler = DigestHandler(db_path=self.db_path, sender=MagicMock())
        handler.handle(_classified(ticker="AAPL"))
        handler.flush()
        self.assertEqual(handler.buffered(), [])


class TestAutoTriggerEnqueueHandler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue_path = Path(self.tmp.name) / "queue.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_handle_enqueues_classified_event(self):
        from alphalens_research.core.queue import CandidateQueue
        from alphalens_research.watchdog.dispatch.handlers.auto_trigger import (
            AutoTriggerEnqueueHandler,
        )

        handler = AutoTriggerEnqueueHandler(queue_path=self.queue_path)
        handler.handle(_classified(ticker="AAPL"))
        handler.close()

        with CandidateQueue(self.queue_path) as q:
            pending = q.list_by_status("pending")
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["ticker"], "AAPL")
            self.assertEqual(pending[0]["source"], "watchdog_sec")

    def test_handle_does_not_raise_on_queue_write_error(self):
        from alphalens_research.watchdog.dispatch.handlers.auto_trigger import (
            AutoTriggerEnqueueHandler,
        )

        handler = AutoTriggerEnqueueHandler(queue_path=self.queue_path)
        # Simulate queue failure by closing the underlying connection
        handler.queue.close()

        # Should not raise despite broken queue
        handler.handle(_classified(ticker="AAPL"))


class TestObsoleteSyncHandlerRemoved(unittest.TestCase):
    def test_legacy_class_no_longer_exported(self):
        from alphalens_research.watchdog.dispatch.handlers import auto_trigger

        self.assertFalse(
            hasattr(auto_trigger, "AutoTriggerHandler"),
            "Sync AutoTriggerHandler replaced by AutoTriggerEnqueueHandler + worker",
        )


if __name__ == "__main__":
    unittest.main()
