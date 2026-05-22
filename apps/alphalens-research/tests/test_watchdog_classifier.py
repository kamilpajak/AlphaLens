import unittest
from datetime import UTC, datetime


def _event(form_type, ticker="AAPL", raw_data=None, accession="ACC-1"):
    from alphalens_research.watchdog.types import Event

    return Event(
        ticker=ticker,
        form_type=form_type,
        accession_number=accession,
        filed_at=datetime(2026, 4, 17, 12, 0, tzinfo=UTC),
        url=f"https://sec.gov/{accession}",
        raw_data=raw_data or {},
    )


def _portfolio(held=None, watchlist=None):
    from alphalens_research.watchdog.portfolio import PortfolioState

    return PortfolioState(held=held or [], watchlist=watchlist or [])


class TestSeverityAndActionEnums(unittest.TestCase):
    def test_severity_has_low_medium_high(self):
        from alphalens_research.watchdog.classifier import Severity

        names = {s.name for s in Severity}
        self.assertEqual(names, {"LOW", "MEDIUM", "HIGH"})

    def test_action_has_required_members(self):
        from alphalens_research.watchdog.classifier import Action

        names = {a.name for a in Action}
        self.assertEqual(names, {"AUTO_TRIGGER", "APPROVAL", "DIGEST", "IGNORE"})


class TestForm4Severity(unittest.TestCase):
    """Evidence: Cohen/Malloy/Pomorski — buys predictive (+2.5% CAR),
    sales flat/noise (diversification, 10b5-1)."""

    def test_form4_default_low_when_action_unknown(self):
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        # No insider_action in raw_data (fetch_form4_details=False or parse failed)
        result = classifier.classify(_event(FormType.FORM_4), _portfolio())
        self.assertEqual(result.severity, Severity.LOW)

    def test_form4_buy_default_medium(self):
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(
            FormType.FORM_4,
            raw_data={"insider_action": "BUY", "transaction_value_usd": 100_000},
        )
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.MEDIUM)

    def test_form4_large_insider_buy_is_high(self):
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(
            FormType.FORM_4,
            raw_data={"insider_action": "BUY", "transaction_value_usd": 1_000_000},
        )
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.HIGH)

    def test_form4_sell_is_low_regardless_of_size(self):
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(
            FormType.FORM_4,
            raw_data={"insider_action": "SELL", "transaction_value_usd": 10_000_000},
        )
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.LOW)

    def test_form4_small_sell_is_suppressed_to_ignore(self):
        """Evidence: sub-$100k SELLs are pure noise. Action.IGNORE regardless of portfolio."""
        from alphalens_research.watchdog.classifier import Action, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(
            FormType.FORM_4,
            raw_data={"insider_action": "SELL", "transaction_value_usd": 50_000},
        )
        result = classifier.classify(event, _portfolio(held=["AAPL"]))
        self.assertEqual(result.action, Action.IGNORE)

    def test_form4_small_buy_is_low(self):
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(
            FormType.FORM_4,
            raw_data={"insider_action": "BUY", "transaction_value_usd": 20_000},
        )
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.LOW)


class TestBeneficialOwnership(unittest.TestCase):
    """Evidence: 13D activist +3-6% CAR; 13G ~65% passive rebalancing."""

    def test_13d_is_high(self):
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        result = classifier.classify(_event(FormType.FORM_13D), _portfolio())
        self.assertEqual(result.severity, Severity.HIGH)

    def test_13g_is_medium_not_high(self):
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        result = classifier.classify(_event(FormType.FORM_13G), _portfolio())
        self.assertEqual(result.severity, Severity.MEDIUM)

    def test_13d_amendment_is_medium(self):
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        result = classifier.classify(_event(FormType.FORM_13D_A), _portfolio())
        self.assertEqual(result.severity, Severity.MEDIUM)

    def test_13g_amendment_is_low(self):
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        result = classifier.classify(_event(FormType.FORM_13G_A), _portfolio())
        self.assertEqual(result.severity, Severity.LOW)


class TestEightKItems(unittest.TestCase):
    """Evidence: Beneish (4.02 -1.5%), Salzman (5.02 CEO -1.5-2%, director -0.3%),
    Aharony (2.04 -1.5-3%); earnings 8-K (2.02) priced in via press release."""

    def test_8k_402_non_reliance_is_high(self):
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"items": ["4.02"]})
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.HIGH)

    def test_8k_204_default_is_high(self):
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"items": ["2.04"]})
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.HIGH)

    def test_8k_502b_principal_officer_termination_is_high(self):
        """SEC Form 8-K Item 5.02(b): termination of principal executive officer
        (CEO), CFO, COO, or principal accounting officer. Salzman: ~-1.5 to -2%
        CAR, deserves HIGH severity → AUTO_TRIGGER on held tickers."""
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"items": ["5.02(b)"]})
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.HIGH)

    def test_8k_502c_principal_officer_appointment_is_high(self):
        """Item 5.02(c): appointment of new principal officer. Materially informative
        (succession signal) — classify as HIGH."""
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"items": ["5.02(c)"]})
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.HIGH)

    def test_8k_502a_director_resignation_is_medium(self):
        """Item 5.02(a): director resignation/removal (not in dispute). Salzman
        director ~-0.3% CAR — MEDIUM, approval gate rather than auto-trigger."""
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"items": ["5.02(a)"]})
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.MEDIUM)

    def test_8k_502d_director_election_is_medium(self):
        """Item 5.02(d): election of director (non-annual). Routine governance,
        MEDIUM."""
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"items": ["5.02(d)"]})
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.MEDIUM)

    def test_8k_502e_compensation_change_is_low(self):
        """Item 5.02(e)-(f): compensatory arrangements / salary changes. Procedural,
        rarely market-moving — LOW."""
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"items": ["5.02(e)"]})
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.LOW)

    def test_8k_502_without_subsection_defaults_to_medium(self):
        """Defensive fallback: if the primary HTML parser fails to capture the
        subsection and we only see plain '5.02', we still know it's about
        directors/officers — default to MEDIUM (not auto-trigger, but not noise)."""
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"items": ["5.02"]})
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.MEDIUM)

    def test_8k_202_earnings_is_low(self):
        """Earnings 8-K is priced in via press release (Dellavigna & Pollet 2009)."""
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"items": ["2.02"]})
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.LOW)

    def test_8k_701_regfd_is_low(self):
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"items": ["7.01"]})
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.LOW)

    def test_8k_101_material_agreement_is_medium(self):
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"items": ["1.01"]})
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.MEDIUM)

    def test_8k_without_items_is_low(self):
        """Title-only 8-Ks without parseable items are usually routine (8.01 Other)."""
        from alphalens_research.watchdog.classifier import Severity, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"title": "8-K - Other Events"})
        result = classifier.classify(event, _portfolio())
        self.assertEqual(result.severity, Severity.LOW)


class TestActionMatrix(unittest.TestCase):
    def test_classify_returns_severity_relevance_action(self):
        from alphalens_research.watchdog.classifier import ClassifiedEvent, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        result = classifier.classify(_event(FormType.FORM_4), _portfolio(held=["AAPL"]))
        self.assertIsInstance(result, ClassifiedEvent)

    def test_action_matrix_high_held_is_auto_trigger(self):
        from alphalens_research.watchdog.classifier import Action, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(FormType.FORM_8K, raw_data={"items": ["4.02"]})
        result = classifier.classify(event, _portfolio(held=["AAPL"]))
        self.assertEqual(result.action, Action.AUTO_TRIGGER)

    def test_action_matrix_covers_all_cells(self):
        """Base 3x3 matrix — before form-type-specific overrides."""
        from alphalens_research.watchdog.classifier import Action, Severity, SignalClassifier
        from alphalens_research.watchdog.portfolio import Relevance

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
                self.assertEqual(classifier._action_for(sev, rel), expected_action)

    def test_form4_medium_on_watchlist_is_digested_not_approved(self):
        """Spam-reduction override: Form 4 MEDIUM on watchlist is too noisy for approval."""
        from alphalens_research.watchdog.classifier import Action, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(
            FormType.FORM_4,
            raw_data={"insider_action": "BUY", "transaction_value_usd": 100_000},
        )
        result = classifier.classify(event, _portfolio(watchlist=["AAPL"]))
        self.assertEqual(result.action, Action.DIGEST)

    def test_form4_medium_on_held_stays_approval(self):
        """HELD is capital at risk — keep approval for MEDIUM buys."""
        from alphalens_research.watchdog.classifier import Action, SignalClassifier
        from alphalens_research.watchdog.types import FormType

        classifier = SignalClassifier()
        event = _event(
            FormType.FORM_4,
            raw_data={"insider_action": "BUY", "transaction_value_usd": 100_000},
        )
        result = classifier.classify(event, _portfolio(held=["AAPL"]))
        self.assertEqual(result.action, Action.APPROVAL)


if __name__ == "__main__":
    unittest.main()
