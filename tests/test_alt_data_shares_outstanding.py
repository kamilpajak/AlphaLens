import unittest
from datetime import date
from unittest.mock import MagicMock


def _payload(entries_by_taxonomy: dict) -> dict:
    facts = {}
    for tax, entries in entries_by_taxonomy.items():
        facts[tax] = {
            "EntityCommonStockSharesOutstanding": {
                "units": {"shares": entries}
            }
        }
    return {"cik": 320193, "facts": facts}


class TestParseCompanyFacts(unittest.TestCase):
    def test_happy_path_us_gaap(self):
        from alphalens.alt_data.shares_outstanding import parse_company_facts

        payload = _payload({
            "us-gaap": [
                {"val": 1000000, "end": "2024-03-31", "filed": "2024-05-02",
                 "accn": "acc-1", "form": "10-Q"},
                {"val": 1010000, "end": "2024-06-30", "filed": "2024-08-05",
                 "accn": "acc-2", "form": "10-Q"},
            ]
        })

        facts = parse_company_facts(payload, cik="0000320193")

        self.assertEqual(len(facts), 2)
        self.assertEqual(facts[0].shares, 1000000)
        self.assertEqual(facts[0].end_date, date(2024, 3, 31))
        self.assertEqual(facts[0].filed_date, date(2024, 5, 2))
        self.assertEqual(facts[0].form_type, "10-Q")
        self.assertEqual(facts[0].accession, "acc-1")
        self.assertEqual(facts[0].cik, "0000320193")

    def test_falls_back_to_dei_taxonomy(self):
        """Some filers report EntityCommonStockSharesOutstanding under dei,
        not us-gaap. Parser must try both."""
        from alphalens.alt_data.shares_outstanding import parse_company_facts

        payload = _payload({
            "dei": [
                {"val": 5000, "end": "2024-03-31", "filed": "2024-05-01",
                 "accn": "a", "form": "10-Q"},
            ]
        })

        facts = parse_company_facts(payload, cik="0000000001")

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].shares, 5000)

    def test_us_gaap_preferred_over_dei_if_both_present(self):
        from alphalens.alt_data.shares_outstanding import parse_company_facts

        payload = _payload({
            "us-gaap": [
                {"val": 999, "end": "2024-03-31", "filed": "2024-05-01",
                 "accn": "a", "form": "10-Q"},
            ],
            "dei": [
                {"val": 555, "end": "2024-03-31", "filed": "2024-05-01",
                 "accn": "a", "form": "10-Q"},
            ],
        })

        facts = parse_company_facts(payload, cik="0000000001")

        self.assertEqual(facts[0].shares, 999)

    def test_missing_concept_returns_empty(self):
        from alphalens.alt_data.shares_outstanding import parse_company_facts

        self.assertEqual(
            parse_company_facts({"facts": {}}, cik="0000000001"),
            [],
        )

    def test_empty_payload_returns_empty(self):
        from alphalens.alt_data.shares_outstanding import parse_company_facts

        self.assertEqual(parse_company_facts({}, cik="0000000001"), [])

    def test_malformed_entries_skipped(self):
        from alphalens.alt_data.shares_outstanding import parse_company_facts

        payload = _payload({
            "us-gaap": [
                {"val": 100, "end": "2024-03-31", "filed": "2024-05-01",
                 "accn": "a", "form": "10-Q"},
                {"val": "not-int", "end": "2024-06-30", "filed": "2024-08-01",
                 "accn": "b", "form": "10-Q"},
                {"val": 200, "end": "bad-date", "filed": "2024-11-01",
                 "accn": "c", "form": "10-Q"},
                {"val": 300, "end": "2024-12-31", "filed": "2025-02-01",
                 "accn": "d", "form": "10-K"},
            ]
        })

        facts = parse_company_facts(payload, cik="0000000001")

        # Only the 2 well-formed entries survive.
        self.assertEqual(len(facts), 2)
        self.assertEqual({f.shares for f in facts}, {100, 300})


class TestLatestSharesAsOf(unittest.TestCase):
    def _fact(self, shares: int, filed: str) -> object:
        from alphalens.alt_data.shares_outstanding import SharesFact

        return SharesFact(
            cik="0000000001",
            end_date=date.fromisoformat(filed) - __import__("datetime").timedelta(days=45),
            filed_date=date.fromisoformat(filed),
            shares=shares,
            form_type="10-Q",
            accession="a",
        )

    def test_returns_latest_filed_before_asof(self):
        from alphalens.alt_data.shares_outstanding import latest_shares_as_of

        facts = [
            self._fact(100, "2024-05-01"),
            self._fact(200, "2024-08-05"),
            self._fact(300, "2024-11-04"),
        ]

        self.assertEqual(latest_shares_as_of(facts, date(2024, 9, 1)), 200)

    def test_exactly_on_filed_date_included(self):
        from alphalens.alt_data.shares_outstanding import latest_shares_as_of

        facts = [self._fact(100, "2024-05-01")]

        self.assertEqual(latest_shares_as_of(facts, date(2024, 5, 1)), 100)

    def test_filed_after_asof_excluded(self):
        from alphalens.alt_data.shares_outstanding import latest_shares_as_of

        facts = [self._fact(100, "2024-05-01"), self._fact(200, "2024-08-05")]

        self.assertEqual(latest_shares_as_of(facts, date(2024, 6, 1)), 100)

    def test_no_facts_before_asof_returns_none(self):
        from alphalens.alt_data.shares_outstanding import latest_shares_as_of

        facts = [self._fact(100, "2025-01-01")]

        self.assertIsNone(latest_shares_as_of(facts, date(2024, 5, 1)))

    def test_empty_list_returns_none(self):
        from alphalens.alt_data.shares_outstanding import latest_shares_as_of

        self.assertIsNone(latest_shares_as_of([], date(2024, 5, 1)))


class TestEdgarClientCompanyFacts(unittest.TestCase):
    def test_hits_correct_url(self):
        from alphalens.alt_data.sec_edgar_client import SecEdgarClient

        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"cik": 320193, "facts": {}}
        session.get.return_value = resp
        sleep = MagicMock()

        client = SecEdgarClient(
            user_agent="AlphaLens test@example.com",
            rate_limit_per_sec=10,
            session=session,
            sleep=sleep,
        )

        client.fetch_company_facts("0000320193")

        url = session.get.call_args[0][0]
        self.assertIn("api/xbrl/companyfacts/CIK0000320193.json", url)


if __name__ == "__main__":
    unittest.main()
