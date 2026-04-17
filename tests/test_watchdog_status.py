import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from typer.testing import CliRunner


def _classified_digest_event(ticker: str, accession: str):
    from alphalens.watchdog.classifier import Action, ClassifiedEvent, Severity
    from alphalens.watchdog.portfolio import Relevance
    from alphalens.watchdog.types import Event, FormType

    return ClassifiedEvent(
        event=Event(
            ticker=ticker,
            form_type=FormType.FORM_8K,
            accession_number=accession,
            filed_at=datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc),
            url=f"https://sec.gov/{accession}",
            raw_data={},
        ),
        severity=Severity.LOW,
        relevance=Relevance.HELD,
        action=Action.DIGEST,
    )


class TestCollectStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.queue_path = self.home / "queue.db"
        self.digest_path = self.home / "digest.db"
        self.seen_path = self.home / "seen.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_dbs_return_zero_counts(self):
        from alphalens.watchdog.status import collect_status

        s = collect_status(self.queue_path, self.digest_path, self.seen_path)

        self.assertEqual(s["queue"]["pending"], 0)
        self.assertEqual(s["queue"]["done_today"], 0)
        self.assertEqual(s["digest"]["total"], 0)
        self.assertEqual(s["seen_events"]["total"], 0)

    def test_queue_breakdown_includes_pending_done_failed_and_latest(self):
        from alphalens.watchdog.queue import AutoTriggerQueue
        from alphalens.watchdog.status import collect_status

        q = AutoTriggerQueue(self.queue_path)
        q.enqueue("AAPL", "A1", "u")
        q.enqueue("MSFT", "A2", "u")
        q.enqueue("NVDA", "A3", "u")

        job1 = q.claim_next()
        q.mark_done(job1["id"], "BUY")
        job2 = q.claim_next()
        q.mark_failed(job2["id"], "LLM rate limit")
        q.close()

        s = collect_status(self.queue_path, self.digest_path, self.seen_path, budget_per_day=5)

        self.assertEqual(s["queue"]["pending"], 1)  # only NVDA left
        self.assertEqual(s["queue"]["done_today"], 1)
        self.assertEqual(s["queue"]["failed"], 1)
        self.assertEqual(s["queue"]["budget_per_day"], 5)
        self.assertEqual(s["queue"]["latest_done"]["ticker"], "AAPL")
        self.assertEqual(s["queue"]["latest_done"]["decision"], "BUY")

    def test_digest_breakdown_counts_per_ticker(self):
        from alphalens.watchdog.dispatch.handlers.digest import DigestHandler
        from alphalens.watchdog.status import collect_status

        h = DigestHandler(db_path=self.digest_path, sender=MagicMock())
        h.handle(_classified_digest_event("AAPL", "D1"))
        h.handle(_classified_digest_event("AAPL", "D2"))
        h.handle(_classified_digest_event("MSFT", "D3"))
        h.close()

        s = collect_status(self.queue_path, self.digest_path, self.seen_path)

        self.assertEqual(s["digest"]["total"], 3)
        self.assertEqual(s["digest"]["per_ticker"]["AAPL"], 2)
        self.assertEqual(s["digest"]["per_ticker"]["MSFT"], 1)

    def test_seen_events_total_is_counted(self):
        from alphalens.watchdog.status import collect_status
        from alphalens.watchdog.storage import SeenEventStore

        store = SeenEventStore(self.seen_path)
        store.mark_seen("ACC-1")
        store.mark_seen("ACC-2")
        store.mark_seen("ACC-3")
        store.close()

        s = collect_status(self.queue_path, self.digest_path, self.seen_path)
        self.assertEqual(s["seen_events"]["total"], 3)


class TestFormatStatus(unittest.TestCase):
    def test_format_surfaces_key_counts_and_budget(self):
        from alphalens.watchdog.status import format_status

        status = {
            "queue": {
                "pending": 2, "in_progress": 0,
                "done_today": 3, "done_week": 7, "failed": 1,
                "budget_per_day": 5,
                "latest_done": {"ticker": "AAPL", "decision": "BUY", "finished_at": "2026-04-17T14:35:00+00:00"},
            },
            "digest": {
                "total": 12, "per_ticker": {"AAPL": 8, "MSFT": 4},
                "latest": {"ticker": "AAPL", "at": "2026-04-17T12:00:00+00:00"},
            },
            "seen_events": {"total": 134},
        }
        text = format_status(status)

        self.assertIn("2", text)  # pending count
        self.assertIn("3 / 5", text)  # done_today / budget
        self.assertIn("AAPL", text)
        self.assertIn("BUY", text)
        self.assertIn("134", text)


class TestStatusCLICommand(unittest.TestCase):
    def test_status_subcommand_is_registered(self):
        from cli.watchdog_main import watchdog_app

        result = CliRunner().invoke(watchdog_app, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("status", result.stdout)


if __name__ == "__main__":
    unittest.main()
