import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock


def _classified(ticker="AAPL", severity=None, relevance=None, action=None, form=None, items=None):
    from alphalens_pipeline.edgar_detector.classifier import Action, ClassifiedEvent, Severity
    from alphalens_pipeline.edgar_detector.portfolio import Relevance
    from alphalens_pipeline.edgar_detector.types import Event, FormType

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
        from alphalens_pipeline.edgar_detector.dispatch.handlers.base import AlertHandler

        class BadHandler(AlertHandler):
            pass

        with self.assertRaises(TypeError):
            BadHandler()  # type: ignore[abstract]


class TestTelegramHandler(unittest.TestCase):
    """The handler now formats the message and delegates delivery to the
    canonical :class:`TelegramClient` (URL building + retry + token sanitising
    are the client's job, covered by ``test_telegram_client``)."""

    def test_formats_and_delegates_to_client(self):
        from alphalens_pipeline.edgar_detector.classifier import Severity
        from alphalens_pipeline.edgar_detector.dispatch.handlers.telegram import TelegramHandler

        client = MagicMock()
        handler = TelegramHandler(bot_token="T", chat_id="CHATID", client=client)
        handler.handle(_classified(severity=Severity.HIGH))

        client.send_message.assert_called_once()
        chat_id, text = client.send_message.call_args.args
        self.assertEqual(chat_id, "CHATID")
        self.assertIn("HIGH", text)
        self.assertIn("AAPL", text)
        self.assertIn("https://sec.gov/filing", text)

    def test_delivery_failure_does_not_raise(self):
        from alphalens_pipeline.edgar_detector.dispatch.handlers.telegram import TelegramHandler

        client = MagicMock()
        client.send_message.return_value = False  # client swallows failures
        handler = TelegramHandler(bot_token="T", chat_id="C", client=client)
        handler.handle(_classified())  # should not raise

    def test_requires_bot_token_and_chat_id(self):
        from alphalens_pipeline.edgar_detector.dispatch.handlers.telegram import TelegramHandler

        with self.assertRaises(ValueError):
            TelegramHandler(bot_token="", chat_id="C")  # empty token rejected by the client
        with self.assertRaises(ValueError):
            TelegramHandler(bot_token="T", chat_id="")


class TestDigestHandler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "digest.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_handle_adds_event_to_buffer(self):
        from alphalens_pipeline.edgar_detector.dispatch.handlers.digest import DigestHandler

        handler = DigestHandler(db_path=self.db_path, sender=MagicMock())
        handler.handle(_classified(ticker="AAPL"))
        handler.handle(_classified(ticker="MSFT"))

        self.assertEqual(len(handler.buffered()), 2)

    def test_flush_sends_combined_message(self):
        from alphalens_pipeline.edgar_detector.dispatch.handlers.digest import DigestHandler

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
        from alphalens_pipeline.edgar_detector.dispatch.handlers.digest import DigestHandler

        h1 = DigestHandler(db_path=self.db_path, sender=MagicMock())
        h1.handle(_classified(ticker="NVDA"))
        h1.close()

        h2 = DigestHandler(db_path=self.db_path, sender=MagicMock())
        buffered = h2.buffered()
        tickers = [e.event.ticker for e in buffered]
        self.assertIn("NVDA", tickers)

    def test_flush_clears_buffer(self):
        from alphalens_pipeline.edgar_detector.dispatch.handlers.digest import DigestHandler

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
        from alphalens_pipeline.core.queue import CandidateQueue
        from alphalens_pipeline.edgar_detector.dispatch.handlers.auto_trigger import (
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
        from alphalens_pipeline.edgar_detector.dispatch.handlers.auto_trigger import (
            AutoTriggerEnqueueHandler,
        )

        handler = AutoTriggerEnqueueHandler(queue_path=self.queue_path)
        # Simulate queue failure by closing the underlying connection
        handler.queue.close()

        # Should not raise despite broken queue
        handler.handle(_classified(ticker="AAPL"))


if __name__ == "__main__":
    unittest.main()
