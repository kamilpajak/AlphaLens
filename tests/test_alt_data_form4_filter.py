import unittest
from datetime import date
from decimal import Decimal


def _record(**overrides):
    from alphalens.data.alt_data.form4_records import Form4Record

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
        transaction_shares=Decimal("1000"),
        transaction_price_per_share=Decimal("150.25"),
        acquired_disposed="A",
        is_amendment=False,
        footnotes=(),
    )
    defaults.update(overrides)
    return Form4Record(**defaults)


class TestTransactionCodeFilter(unittest.TestCase):
    def test_keeps_code_p(self):
        from alphalens.data.alt_data.form4_filter import filter_eligible

        kept = filter_eligible([_record(transaction_code="P")])
        self.assertEqual(len(kept), 1)

    def test_drops_code_s(self):
        from alphalens.data.alt_data.form4_filter import filter_eligible

        kept = filter_eligible([_record(transaction_code="S")])
        self.assertEqual(kept, [])

    def test_drops_code_f_tax_withhold(self):
        from alphalens.data.alt_data.form4_filter import filter_eligible

        kept = filter_eligible([_record(transaction_code="F")])
        self.assertEqual(kept, [])

    def test_drops_code_a_award(self):
        from alphalens.data.alt_data.form4_filter import filter_eligible

        kept = filter_eligible([_record(transaction_code="A")])
        self.assertEqual(kept, [])

    def test_drops_code_g_gift(self):
        from alphalens.data.alt_data.form4_filter import filter_eligible

        kept = filter_eligible([_record(transaction_code="G")])
        self.assertEqual(kept, [])


class TestRoleFilter(unittest.TestCase):
    def test_drops_pure_ten_percent_holder(self):
        from alphalens.data.alt_data.form4_filter import filter_eligible

        kept = filter_eligible(
            [
                _record(is_director=False, is_officer=False, is_ten_percent_owner=True),
            ]
        )
        self.assertEqual(kept, [])

    def test_drops_pure_other(self):
        from alphalens.data.alt_data.form4_filter import filter_eligible

        kept = filter_eligible(
            [
                _record(is_director=False, is_officer=False, is_other=True),
            ]
        )
        self.assertEqual(kept, [])

    def test_keeps_pure_officer(self):
        from alphalens.data.alt_data.form4_filter import filter_eligible

        kept = filter_eligible(
            [
                _record(is_director=False, is_officer=True, is_ten_percent_owner=False),
            ]
        )
        self.assertEqual(len(kept), 1)

    def test_keeps_pure_director(self):
        from alphalens.data.alt_data.form4_filter import filter_eligible

        kept = filter_eligible(
            [
                _record(is_director=True, is_officer=False, is_ten_percent_owner=False),
            ]
        )
        self.assertEqual(len(kept), 1)

    def test_keeps_joint_officer_director(self):
        from alphalens.data.alt_data.form4_filter import filter_eligible

        kept = filter_eligible(
            [
                _record(is_director=True, is_officer=True, is_ten_percent_owner=False),
            ]
        )
        self.assertEqual(len(kept), 1)

    def test_keeps_officer_who_also_owns_10_percent(self):
        """Insider wearing both hats — officer role triggers keep."""
        from alphalens.data.alt_data.form4_filter import filter_eligible

        kept = filter_eligible(
            [
                _record(is_director=False, is_officer=True, is_ten_percent_owner=True),
            ]
        )
        self.assertEqual(len(kept), 1)


class TestAmendmentPassThrough(unittest.TestCase):
    def test_amendment_flag_does_not_affect_filter(self):
        from alphalens.data.alt_data.form4_filter import filter_eligible

        kept = filter_eligible([_record(is_amendment=True, transaction_code="P")])
        self.assertEqual(len(kept), 1)


class TestMixedBatch(unittest.TestCase):
    def test_filter_returns_only_qualifying_records(self):
        from alphalens.data.alt_data.form4_filter import filter_eligible

        records = [
            _record(transaction_code="P", is_officer=True),  # keep
            _record(transaction_code="S", is_officer=True),  # drop: code
            _record(
                transaction_code="P",
                is_officer=False,
                is_director=False,
                is_ten_percent_owner=True,
            ),  # drop: role
            _record(transaction_code="P", is_director=True, is_officer=False),  # keep
        ]

        kept = filter_eligible(records)

        self.assertEqual(len(kept), 2)
        self.assertTrue(all(r.transaction_code == "P" for r in kept))


if __name__ == "__main__":
    unittest.main()
