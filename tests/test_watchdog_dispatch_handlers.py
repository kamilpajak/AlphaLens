import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


def _classified(ticker="AAPL", severity=None, relevance=None, action=None, form=None, items=None):
    from tradingagents.watchdog.classifier import Action, ClassifiedEvent, Severity
    from tradingagents.watchdog.portfolio import Relevance
    from tradingagents.watchdog.types import Event, FormType

    return ClassifiedEvent(
        event=Event(
            ticker=ticker,
            form_type=form or FormType.FORM_8K,
            accession_number="ACC-001",
            filed_at=datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc),
            url="https://sec.gov/filing",
            raw_data={"items": items or ["4.02"]},
        ),
        severity=severity or Severity.HIGH,
        relevance=relevance or Relevance.HELD,
        action=action or Action.APPROVAL,
    )


class TestAlertHandlerABC(unittest.TestCase):
    def test_abstract_handle_method_required(self):
        from tradingagents.watchdog.dispatch.handlers.base import AlertHandler

        class BadHandler(AlertHandler):
            pass

        with self.assertRaises(TypeError):
            BadHandler()  # type: ignore[abstract]


class TestTelegramHandler(unittest.TestCase):
    @patch("tradingagents.watchdog.dispatch.handlers.telegram.requests.post")
    def test_send_uses_bot_api_endpoint(self, mock_post):
        from tradingagents.watchdog.dispatch.handlers.telegram import TelegramHandler

        mock_post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())
        handler = TelegramHandler(bot_token="BOTTOKEN", chat_id="CHATID")
        handler.handle(_classified())

        self.assertTrue(mock_post.called)
        url = mock_post.call_args.args[0]
        self.assertIn("api.telegram.org/botBOTTOKEN/sendMessage", url)

    @patch("tradingagents.watchdog.dispatch.handlers.telegram.requests.post")
    def test_message_includes_severity_and_url(self, mock_post):
        from tradingagents.watchdog.classifier import Severity
        from tradingagents.watchdog.dispatch.handlers.telegram import TelegramHandler

        mock_post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())
        handler = TelegramHandler(bot_token="T", chat_id="C")
        handler.handle(_classified(severity=Severity.HIGH))

        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args.kwargs.get("data")
        self.assertIsNotNone(payload)
        text = payload["text"]
        self.assertIn("HIGH", text)
        self.assertIn("AAPL", text)
        self.assertIn("https://sec.gov/filing", text)

    @patch("tradingagents.watchdog.dispatch.handlers.telegram.requests.post")
    def test_handles_api_error_without_raising(self, mock_post):
        import requests as req_module

        from tradingagents.watchdog.dispatch.handlers.telegram import TelegramHandler

        mock_post.side_effect = req_module.ConnectionError("down")
        handler = TelegramHandler(bot_token="T", chat_id="C")
        handler.handle(_classified())  # should not raise

    def test_requires_bot_token_and_chat_id(self):
        from tradingagents.watchdog.dispatch.handlers.telegram import TelegramHandler

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
        from tradingagents.watchdog.dispatch.handlers.digest import DigestHandler

        handler = DigestHandler(db_path=self.db_path, sender=MagicMock())
        handler.handle(_classified(ticker="AAPL"))
        handler.handle(_classified(ticker="MSFT"))

        self.assertEqual(len(handler.buffered()), 2)

    def test_flush_sends_combined_message(self):
        from tradingagents.watchdog.dispatch.handlers.digest import DigestHandler

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
        from tradingagents.watchdog.dispatch.handlers.digest import DigestHandler

        h1 = DigestHandler(db_path=self.db_path, sender=MagicMock())
        h1.handle(_classified(ticker="NVDA"))
        h1.close()

        h2 = DigestHandler(db_path=self.db_path, sender=MagicMock())
        buffered = h2.buffered()
        tickers = [e.event.ticker for e in buffered]
        self.assertIn("NVDA", tickers)

    def test_flush_clears_buffer(self):
        from tradingagents.watchdog.dispatch.handlers.digest import DigestHandler

        handler = DigestHandler(db_path=self.db_path, sender=MagicMock())
        handler.handle(_classified(ticker="AAPL"))
        handler.flush()
        self.assertEqual(handler.buffered(), [])


class TestAutoTriggerHandler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.budget_path = Path(self.tmp.name) / "budget.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_calls_tradingagents_propagate_with_ticker_and_date(self):
        from tradingagents.watchdog.dispatch.handlers.auto_trigger import AutoTriggerHandler

        ta_graph = MagicMock()
        ta_graph.propagate.return_value = ({}, "BUY")

        handler = AutoTriggerHandler(
            ta_graph=ta_graph,
            notifier=MagicMock(),
            budget_path=self.budget_path,
            budget_per_day=5,
        )
        handler.handle(_classified(ticker="AAPL"))

        self.assertTrue(ta_graph.propagate.called)
        args = ta_graph.propagate.call_args.args
        self.assertEqual(args[0], "AAPL")
        # Second arg is date string
        self.assertIsNotNone(args[1])

    def test_sends_decision_via_notifier(self):
        from tradingagents.watchdog.dispatch.handlers.auto_trigger import AutoTriggerHandler

        ta_graph = MagicMock()
        ta_graph.propagate.return_value = ({}, "OVERWEIGHT")
        notifier = MagicMock()

        handler = AutoTriggerHandler(
            ta_graph=ta_graph,
            notifier=notifier,
            budget_path=self.budget_path,
        )
        handler.handle(_classified(ticker="AAPL"))

        notifier.send_message.assert_called_once()
        msg = notifier.send_message.call_args.args[0]
        self.assertIn("AAPL", msg)
        self.assertIn("OVERWEIGHT", msg)

    def test_budget_guard_blocks_after_limit(self):
        from tradingagents.watchdog.dispatch.handlers.auto_trigger import AutoTriggerHandler

        ta_graph = MagicMock()
        ta_graph.propagate.return_value = ({}, "BUY")
        notifier = MagicMock()

        handler = AutoTriggerHandler(
            ta_graph=ta_graph,
            notifier=notifier,
            budget_path=self.budget_path,
            budget_per_day=2,
        )
        handler.handle(_classified(ticker="AAPL"))
        handler.handle(_classified(ticker="MSFT"))
        handler.handle(_classified(ticker="NVDA"))  # blocked

        self.assertEqual(ta_graph.propagate.call_count, 2)
        # Notifier should be called for blocked one too (informing user)
        blocked_msgs = [
            c.args[0] for c in notifier.send_message.call_args_list
            if "budget" in c.args[0].lower() or "limit" in c.args[0].lower()
        ]
        self.assertTrue(blocked_msgs)

    def test_propagate_failure_does_not_crash(self):
        from tradingagents.watchdog.dispatch.handlers.auto_trigger import AutoTriggerHandler

        ta_graph = MagicMock()
        ta_graph.propagate.side_effect = RuntimeError("LLM down")

        handler = AutoTriggerHandler(
            ta_graph=ta_graph,
            notifier=MagicMock(),
            budget_path=self.budget_path,
        )
        handler.handle(_classified(ticker="AAPL"))  # should not raise


if __name__ == "__main__":
    unittest.main()
