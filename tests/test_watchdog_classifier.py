import unittest
from datetime import datetime, timezone


def _event(form_type, ticker="AAPL", raw_data=None, accession="ACC-1"):
    from tradingagents.watchdog.types import Event

    return Event(
        ticker=ticker,
        form_type=form_type,
        accession_number=accession,
        filed_at=datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc),
        url=f"https://sec.gov/{accession}",
        raw_data=raw_data or {},
    )


def _portfolio(held=None, watchlist=None):
    from tradingagents.watchdog.portfolio import PortfolioState

    return PortfolioState(held=held or [], watchlist=watchlist or [])


class TestSeverityAndActionEnums(unittest.TestCase):
    def test_severity_has_low_medium_high(self):
        from tradingagents.watchdog.classifier import Severity

        names = {s.name for s in Severity}
        self.assertEqual(names, {"LOW", "MEDIUM", "HIGH"})

    def test_action_has_required_members(self):
        from tradingagents.watchdog.classifier import Action

        names = {a.name for a in Action}
        self.assertEqual(names, {"AUTO_TRIGGER", "APPROVAL", "DIGEST", "IGNORE"})


class TestSignalClassifier(unittest.TestCase):
    def test_form4_classified_as_medium_by_default(self):
        from tradingagents.watchdog.classifier import Severity, SignalClassifier
        from tradingagents.watchdog.types import FormType

        classifier = SignalClassifier()
        result = classifier.classify(_event(FormType.FORM_4), _portfolio())
        self.assertEqual(result.severity, Severity.MEDIUM)

    def test_form4_large_insider_buy_is_high(self):
        from tradingagents.watchdog.classifier import Severity, SignalClassifier
        from tradingagents.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(
            FormType.FORM_4,
            raw_data={"insider_action": "BUY", "transaction_value_usd": 1_000_000},
        )
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.HIGH)

    def test_8k_item_402_is_high(self):
        from tradingagents.watchdog.classifier import Severity, SignalClassifier
        from tradingagents.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"items": ["4.02"]})
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.HIGH)

    def test_8k_item_502_is_high(self):
        from tradingagents.watchdog.classifier import Severity, SignalClassifier
        from tradingagents.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"items": ["5.02"]})
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.HIGH)

    def test_8k_other_items_are_medium(self):
        from tradingagents.watchdog.classifier import Severity, SignalClassifier
        from tradingagents.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"items": ["2.02"]})
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.MEDIUM)

    def test_13d_filing_is_high(self):
        from tradingagents.watchdog.classifier import Severity, SignalClassifier
        from tradingagents.watchdog.types import FormType

        classifier = SignalClassifier()
        result = classifier.classify(_event(FormType.FORM_13D), _portfolio())
        self.assertEqual(result.severity, Severity.HIGH)

    def test_classify_returns_severity_relevance_action(self):
        from tradingagents.watchdog.classifier import ClassifiedEvent, SignalClassifier
        from tradingagents.watchdog.types import FormType

        classifier = SignalClassifier()
        result = classifier.classify(_event(FormType.FORM_4), _portfolio(held=["AAPL"]))
        self.assertIsInstance(result, ClassifiedEvent)
        self.assertIsNotNone(result.severity)
        self.assertIsNotNone(result.relevance)
        self.assertIsNotNone(result.action)
        self.assertIsNotNone(result.event)

    def test_action_matrix_strong_held_is_auto_trigger(self):
        from tradingagents.watchdog.classifier import Action, SignalClassifier
        from tradingagents.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"items": ["4.02"]})
        result = classifier.classify(event, _portfolio(held=["AAPL"]))
        self.assertEqual(result.action, Action.AUTO_TRIGGER)

    def test_action_matrix_covers_all_cells(self):
        """Verify the 3x3 matrix from memory: severity × relevance → action."""
        from tradingagents.watchdog.classifier import Action, Severity, SignalClassifier
        from tradingagents.watchdog.portfolio import Relevance
        from tradingagents.watchdog.types import FormType

        classifier = SignalClassifier()

        expected = {
            (Severity.HIGH, Relevance.HELD): Action.AUTO_TRIGGER,
            (Severity.HIGH, Relevance.WATCHLIST): Action.APPROVAL,
            (Severity.HIGH, Relevance.FOREIGN): Action.APPROVAL,
            (Severity.MEDIUM, Relevance.HELD): Action.APPROVAL,
            (Severity.MEDIUM, Relevance.WATCHLIST): Action.APPROVAL,
            (Severity.MEDIUM, Relevance.FOREIGN): Action.DIGEST,
            (Severity.LOW, Relevance.HELD): Action.DIGEST,
            (Severity.LOW, Relevance.WATCHLIST): Action.DIGEST,
            (Severity.LOW, Relevance.FOREIGN): Action.IGNORE,
        }

        for (sev, rel), expected_action in expected.items():
            with self.subTest(severity=sev, relevance=rel):
                actual = classifier._action_for(sev, rel)
                self.assertEqual(
                    actual, expected_action,
                    f"Expected {expected_action} for ({sev}, {rel}), got {actual}"
                )

    def test_low_signal_for_routine_10q_would_be_low(self):
        """10-Q is not in form filter but if it slipped through, should be LOW."""
        from tradingagents.watchdog.classifier import Severity, SignalClassifier
        from tradingagents.watchdog.types import FormType

        classifier = SignalClassifier()
        # Form 4 sale with small value → low (not worth auto-triggering)
        event = _event(
            FormType.FORM_4,
            raw_data={"insider_action": "SELL", "transaction_value_usd": 10_000},
        )
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.LOW)


if __name__ == "__main__":
    unittest.main()
