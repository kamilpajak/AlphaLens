import unittest
from datetime import date, timedelta
from decimal import Decimal


def _record(**overrides):
    from alphalens.alt_data.form4_records import Form4Record

    defaults = dict(
        issuer_cik="0000320193",
        ticker="AAPL",
        accession_number="0000320193-25-000001",
        filing_date=date(2025, 3, 17),
        reporting_owner_cik="0001111111",
        reporting_owner_name="Jane Doe",
        is_director=False,
        is_officer=True,
        is_ten_percent_owner=False,
        is_other=False,
        officer_title="CEO",
        transaction_date=date(2025, 3, 15),
        transaction_code="P",
        transaction_shares=Decimal("100"),
        transaction_price_per_share=Decimal("10.00"),
        acquired_disposed="A",
        is_amendment=False,
        footnotes=(),
    )
    defaults.update(overrides)
    return Form4Record(**defaults)


class TestMinimumInsiderCount(unittest.TestCase):
    def test_exactly_3_distinct_is_cluster(self):
        from alphalens.screeners.insider.cluster import detect_cluster

        records = [
            _record(reporting_owner_cik="0000000001"),
            _record(reporting_owner_cik="0000000002"),
            _record(reporting_owner_cik="0000000003"),
        ]

        cluster = detect_cluster(records, asof=date(2025, 3, 20))

        self.assertIsNotNone(cluster)
        self.assertEqual(cluster.insider_count, 3)

    def test_2_distinct_not_cluster(self):
        from alphalens.screeners.insider.cluster import detect_cluster

        records = [
            _record(reporting_owner_cik="0000000001"),
            _record(reporting_owner_cik="0000000002"),
        ]

        self.assertIsNone(detect_cluster(records, asof=date(2025, 3, 20)))

    def test_same_insider_multiple_trades_counts_once(self):
        from alphalens.screeners.insider.cluster import detect_cluster

        records = [
            _record(reporting_owner_cik="0000000001", transaction_date=date(2025, 3, 10)),
            _record(reporting_owner_cik="0000000001", transaction_date=date(2025, 3, 12)),
            _record(reporting_owner_cik="0000000001", transaction_date=date(2025, 3, 14)),
        ]

        self.assertIsNone(detect_cluster(records, asof=date(2025, 3, 20)))


class TestWindowBoundary(unittest.TestCase):
    def _three_insiders_at(self, *tx_dates: date):
        return [
            _record(
                reporting_owner_cik=f"000000000{i + 1}",
                transaction_date=d,
            )
            for i, d in enumerate(tx_dates)
        ]

    def test_exactly_30d_before_is_inclusive(self):
        from alphalens.screeners.insider.cluster import detect_cluster

        asof = date(2025, 3, 31)
        boundary = asof - timedelta(days=30)  # 2025-03-01

        records = self._three_insiders_at(boundary, boundary, boundary)

        self.assertIsNotNone(detect_cluster(records, asof=asof))

    def test_31d_before_is_excluded(self):
        from alphalens.screeners.insider.cluster import detect_cluster

        asof = date(2025, 3, 31)
        too_old = asof - timedelta(days=31)

        records = self._three_insiders_at(too_old, too_old, too_old)

        self.assertIsNone(detect_cluster(records, asof=asof))

    def test_future_tx_ignored(self):
        from alphalens.screeners.insider.cluster import detect_cluster

        asof = date(2025, 3, 15)
        records = self._three_insiders_at(
            date(2025, 3, 10),
            date(2025, 3, 12),
            date(2025, 3, 20),  # 20th is future
        )

        self.assertIsNone(detect_cluster(records, asof=asof))


class TestPlan10b5_1Exclusion(unittest.TestCase):
    def test_excludes_record_with_old_plan_90d(self):
        """Plan adopted ≥90 days before asof = pre-committed → exclude."""
        from alphalens.screeners.insider.cluster import detect_cluster

        asof = date(2025, 3, 20)
        old_plan_footnote = (("F1", "Rule 10b5-1 trading plan adopted on 2024-10-01."),)

        records = [
            _record(reporting_owner_cik="0000000001", footnotes=old_plan_footnote),
            _record(reporting_owner_cik="0000000002"),
            _record(reporting_owner_cik="0000000003"),
        ]

        # owner #1 excluded → only 2 distinct → no cluster
        self.assertIsNone(detect_cluster(records, asof=asof))

    def test_keeps_record_with_fresh_plan_less_than_90d(self):
        """Plan adopted <90 days = recent conviction → keep."""
        from alphalens.screeners.insider.cluster import detect_cluster

        asof = date(2025, 3, 20)
        fresh_plan_footnote = (("F1", "Rule 10b5-1 plan adopted on 2025-03-01."),)  # 19 days old

        records = [
            _record(reporting_owner_cik="0000000001", footnotes=fresh_plan_footnote),
            _record(reporting_owner_cik="0000000002"),
            _record(reporting_owner_cik="0000000003"),
        ]

        self.assertIsNotNone(detect_cluster(records, asof=asof))

    def test_excludes_record_with_unknown_plan_age(self):
        """Plan mentioned but adoption date not parseable → conservative exclude."""
        from alphalens.screeners.insider.cluster import detect_cluster

        asof = date(2025, 3, 20)
        ambiguous_footnote = (("F1", "Made under a Rule 10b5-1 plan."),)

        records = [
            _record(reporting_owner_cik="0000000001", footnotes=ambiguous_footnote),
            _record(reporting_owner_cik="0000000002"),
            _record(reporting_owner_cik="0000000003"),
        ]

        self.assertIsNone(detect_cluster(records, asof=asof))

    def test_keeps_records_without_plan_mention(self):
        from alphalens.screeners.insider.cluster import detect_cluster

        asof = date(2025, 3, 20)

        records = [
            _record(reporting_owner_cik="0000000001"),  # empty footnotes
            _record(
                reporting_owner_cik="0000000002",
                footnotes=(("F1", "Direct ownership only."),),
            ),
            _record(reporting_owner_cik="0000000003"),
        ]

        self.assertIsNotNone(detect_cluster(records, asof=asof))


class TestClusterMetrics(unittest.TestCase):
    def test_aggregate_dollar_sum(self):
        from alphalens.screeners.insider.cluster import detect_cluster

        records = [
            _record(
                reporting_owner_cik="0000000001",
                transaction_shares=Decimal("100"),
                transaction_price_per_share=Decimal("10.00"),
            ),
            _record(
                reporting_owner_cik="0000000002",
                transaction_shares=Decimal("200"),
                transaction_price_per_share=Decimal("20.00"),
            ),
            _record(
                reporting_owner_cik="0000000003",
                transaction_shares=Decimal("50"),
                transaction_price_per_share=Decimal("30.00"),
            ),
        ]

        cluster = detect_cluster(records, asof=date(2025, 3, 20))

        self.assertEqual(cluster.aggregate_dollar, Decimal("6500.00"))

    def test_aggregate_dollar_skips_none_price(self):
        from alphalens.screeners.insider.cluster import detect_cluster

        records = [
            _record(
                reporting_owner_cik="0000000001",
                transaction_shares=Decimal("100"),
                transaction_price_per_share=None,
            ),
            _record(
                reporting_owner_cik="0000000002",
                transaction_shares=Decimal("200"),
                transaction_price_per_share=Decimal("20.00"),
            ),
            _record(
                reporting_owner_cik="0000000003",
                transaction_shares=Decimal("50"),
                transaction_price_per_share=Decimal("30.00"),
            ),
        ]

        cluster = detect_cluster(records, asof=date(2025, 3, 20))

        # 0 + 4000 + 1500 = 5500
        self.assertEqual(cluster.aggregate_dollar, Decimal("5500.00"))

    def test_records_tuple_preserves_eligible_only(self):
        from alphalens.screeners.insider.cluster import detect_cluster

        asof = date(2025, 3, 20)
        old_plan = (("F1", "10b5-1 plan adopted on 2024-01-01."),)

        records = [
            _record(reporting_owner_cik="0000000001", footnotes=old_plan),  # excluded
            _record(reporting_owner_cik="0000000002"),
            _record(reporting_owner_cik="0000000003"),
            _record(reporting_owner_cik="0000000004"),
        ]

        cluster = detect_cluster(records, asof=asof)

        self.assertEqual(cluster.insider_count, 3)
        self.assertEqual(len(cluster.records), 3)
        self.assertNotIn("0000000001", {r.reporting_owner_cik for r in cluster.records})


class TestParameters(unittest.TestCase):
    def test_custom_min_distinct_insiders(self):
        from alphalens.screeners.insider.cluster import detect_cluster

        records = [
            _record(reporting_owner_cik="0000000001"),
            _record(reporting_owner_cik="0000000002"),
        ]

        self.assertIsNotNone(
            detect_cluster(records, asof=date(2025, 3, 20), min_distinct_insiders=2)
        )

    def test_custom_window_days(self):
        from alphalens.screeners.insider.cluster import detect_cluster

        asof = date(2025, 3, 20)
        records = [
            _record(reporting_owner_cik="0000000001", transaction_date=date(2025, 2, 1)),
            _record(reporting_owner_cik="0000000002", transaction_date=date(2025, 2, 1)),
            _record(reporting_owner_cik="0000000003", transaction_date=date(2025, 2, 1)),
        ]

        self.assertIsNone(detect_cluster(records, asof=asof))  # 47 days > 30
        self.assertIsNotNone(detect_cluster(records, asof=asof, window_days=60))


class TestEmptyInput(unittest.TestCase):
    def test_empty_records_returns_none(self):
        from alphalens.screeners.insider.cluster import detect_cluster

        self.assertIsNone(detect_cluster([], asof=date(2025, 3, 20)))


if __name__ == "__main__":
    unittest.main()
