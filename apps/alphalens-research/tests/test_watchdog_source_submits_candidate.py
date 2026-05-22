"""Integration: AutoTriggerEnqueueHandler must produce a watchdog_sec Candidate in the new queue."""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path


def _classified(ticker="AAPL", accession="ACC-001", url="https://sec.gov/f"):
    from alphalens_research.watchdog.classifier import Action, ClassifiedEvent, Severity
    from alphalens_research.watchdog.portfolio import Relevance
    from alphalens_research.watchdog.types import Event, FormType

    return ClassifiedEvent(
        event=Event(
            ticker=ticker,
            form_type=FormType.FORM_8K,
            accession_number=accession,
            filed_at=datetime(2026, 4, 17, 12, 0, tzinfo=UTC),
            url=url,
            raw_data={"items": ["4.02"]},
        ),
        severity=Severity.HIGH,
        relevance=Relevance.HELD,
        action=Action.AUTO_TRIGGER,
    )


class TestAutoTriggerHandlerWritesCandidate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "candidates.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_handle_submits_watchdog_sec_candidate(self):
        from alphalens_research.core.queue import CandidateQueue
        from alphalens_research.watchdog.dispatch.handlers.auto_trigger import (
            AutoTriggerEnqueueHandler,
        )

        handler = AutoTriggerEnqueueHandler(queue_path=self.db)
        handler.handle(_classified(ticker="AAPL", accession="ACC-XYZ", url="https://sec.gov/aapl"))
        handler.close()

        with CandidateQueue(self.db) as q:
            rows = q.list_by_status("pending")
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["ticker"], "AAPL")
            self.assertEqual(row["source"], "watchdog_sec")
            self.assertEqual(row["priority"], 0)
            import json

            payload = json.loads(row["payload"])
            self.assertEqual(payload["accession"], "ACC-XYZ")
            self.assertEqual(payload["url"], "https://sec.gov/aapl")
            self.assertEqual(payload["form"], "8-K")

    def test_handle_deduplicates_by_accession_number(self):
        from alphalens_research.core.queue import CandidateQueue
        from alphalens_research.watchdog.dispatch.handlers.auto_trigger import (
            AutoTriggerEnqueueHandler,
        )

        handler = AutoTriggerEnqueueHandler(queue_path=self.db)
        handler.handle(_classified(ticker="AAPL", accession="SAME"))
        handler.handle(_classified(ticker="AAPL", accession="SAME"))
        handler.close()

        with CandidateQueue(self.db) as q:
            self.assertEqual(len(q.list_by_status("pending")), 1)


if __name__ == "__main__":
    unittest.main()
