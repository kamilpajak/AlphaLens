import tempfile
import unittest
from datetime import UTC, datetime
from unittest.mock import MagicMock


def _mk_event(ticker="AAPL", accession="ACC-1"):
    from alphalens_pipeline.edgar_detector.types import Event, FormType

    return Event(
        ticker=ticker,
        form_type=FormType.FORM_8K,
        accession_number=accession,
        filed_at=datetime(2026, 4, 17, 12, 0, tzinfo=UTC),
        url=f"https://sec.gov/{accession}",
        raw_data={"items": ["4.02"]},
    )


class TestDetectorOrchestrator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _build_detector(self, sources, router=None, portfolio=None, classifier=None):
        from alphalens_pipeline.edgar_detector.classifier import SignalClassifier
        from alphalens_pipeline.edgar_detector.detector import Detector
        from alphalens_pipeline.edgar_detector.portfolio import PortfolioState

        return Detector(
            sources=sources,
            classifier=classifier or SignalClassifier(),
            portfolio=portfolio or PortfolioState(held=["AAPL"]),
            router=router or MagicMock(),
        )

    def test_run_once_polls_all_sources(self):
        source_a = MagicMock()
        source_a.detect.return_value = [_mk_event("AAPL", "ACC-A1")]
        source_b = MagicMock()
        source_b.detect.return_value = [_mk_event("MSFT", "ACC-B1")]

        detector = self._build_detector(sources=[source_a, source_b])
        detector.run_once()

        source_a.detect.assert_called_once()
        source_b.detect.assert_called_once()

    def test_run_once_classifies_and_dispatches_each_event(self):
        router = MagicMock()
        source = MagicMock()
        source.detect.return_value = [
            _mk_event("AAPL", "ACC-1"),
            _mk_event("MSFT", "ACC-2"),
        ]

        detector = self._build_detector(sources=[source], router=router)
        detector.run_once()

        self.assertEqual(router.dispatch.call_count, 2)

    def test_run_once_continues_after_source_error(self):
        router = MagicMock()
        bad = MagicMock()
        bad.detect.side_effect = RuntimeError("source down")
        good = MagicMock()
        good.detect.return_value = [_mk_event("AAPL")]

        detector = self._build_detector(sources=[bad, good], router=router)
        detector.run_once()

        router.dispatch.assert_called_once()

    def test_run_once_returns_summary_stats(self):
        source = MagicMock()
        source.detect.return_value = [_mk_event(), _mk_event("MSFT", "ACC-2")]

        detector = self._build_detector(sources=[source])
        result = detector.run_once()

        self.assertEqual(result["events_detected"], 2)
        self.assertEqual(result["events_dispatched"], 2)


if __name__ == "__main__":
    unittest.main()
