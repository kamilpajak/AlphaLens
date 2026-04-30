import unittest
from datetime import date
from decimal import Decimal


def _build_xml(
    *,
    document_type: str = "4",
    period_of_report: str = "2025-03-15",
    issuer_cik: str = "0000320193",
    issuer_symbol: str = "AAPL",
    reporting_owners: list[dict] | None = None,
    non_derivative: list[dict] | None = None,
    derivative: list[dict] | None = None,
    footnotes: list[tuple[str, str]] | None = None,
) -> bytes:
    """Render a minimal, schema-faithful Form 4 XML body for tests."""
    owners = (
        reporting_owners
        if reporting_owners is not None
        else [
            {
                "cik": "0001111111",
                "name": "Jane Doe",
                "is_director": "1",
                "is_officer": "1",
                "is_ten_percent_owner": "0",
                "is_other": "0",
                "officer_title": "CEO",
            }
        ]
    )
    txs = (
        non_derivative
        if non_derivative is not None
        else [
            {
                "date": "2025-03-15",
                "code": "P",
                "shares": "1000",
                "price": "150.25",
                "acquired_disposed": "A",
            }
        ]
    )
    derivs = derivative or []
    foot = footnotes or []

    owners_xml = "\n".join(
        f"""
<reportingOwner>
  <reportingOwnerId>
    <rptOwnerCik>{o["cik"]}</rptOwnerCik>
    <rptOwnerName>{o["name"]}</rptOwnerName>
  </reportingOwnerId>
  <reportingOwnerRelationship>
    <isDirector>{o.get("is_director", "0")}</isDirector>
    <isOfficer>{o.get("is_officer", "0")}</isOfficer>
    <isTenPercentOwner>{o.get("is_ten_percent_owner", "0")}</isTenPercentOwner>
    <isOther>{o.get("is_other", "0")}</isOther>
    {f"<officerTitle>{o['officer_title']}</officerTitle>" if o.get("officer_title") else ""}
  </reportingOwnerRelationship>
</reportingOwner>"""
        for o in owners
    )

    def _tx_xml(tx: dict) -> str:
        price_xml = (
            f"<transactionPricePerShare><value>{tx['price']}</value></transactionPricePerShare>"
            if tx.get("price") is not None
            else ""
        )
        return f"""
<nonDerivativeTransaction>
  <securityTitle><value>Common Stock</value></securityTitle>
  <transactionDate><value>{tx["date"]}</value></transactionDate>
  <transactionCoding>
    <transactionFormType>4</transactionFormType>
    <transactionCode>{tx["code"]}</transactionCode>
    <equitySwapInvolved>0</equitySwapInvolved>
  </transactionCoding>
  <transactionAmounts>
    <transactionShares><value>{tx["shares"]}</value></transactionShares>
    {price_xml}
    <transactionAcquiredDisposedCode><value>{tx.get("acquired_disposed", "A")}</value></transactionAcquiredDisposedCode>
  </transactionAmounts>
</nonDerivativeTransaction>"""

    tx_body = "\n".join(_tx_xml(tx) for tx in txs)
    non_deriv_xml = f"<nonDerivativeTable>{tx_body}</nonDerivativeTable>" if txs else ""

    def _deriv_xml(tx: dict) -> str:
        return f"""
<derivativeTransaction>
  <securityTitle><value>Stock Option</value></securityTitle>
  <transactionDate><value>{tx["date"]}</value></transactionDate>
  <transactionCoding>
    <transactionFormType>4</transactionFormType>
    <transactionCode>{tx["code"]}</transactionCode>
  </transactionCoding>
  <transactionAmounts>
    <transactionShares><value>{tx["shares"]}</value></transactionShares>
  </transactionAmounts>
</derivativeTransaction>"""

    deriv_body = "\n".join(_deriv_xml(tx) for tx in derivs)
    deriv_xml = f"<derivativeTable>{deriv_body}</derivativeTable>" if derivs else ""

    foot_body = "\n".join(f'<footnote id="{fid}">{text}</footnote>' for fid, text in foot)
    foot_xml = f"<footnotes>{foot_body}</footnotes>" if foot else ""

    body = f"""<?xml version="1.0"?>
<ownershipDocument>
<schemaVersion>X0306</schemaVersion>
<documentType>{document_type}</documentType>
<periodOfReport>{period_of_report}</periodOfReport>
<issuer>
  <issuerCik>{issuer_cik}</issuerCik>
  <issuerName>Issuer Inc.</issuerName>
  <issuerTradingSymbol>{issuer_symbol}</issuerTradingSymbol>
</issuer>
{owners_xml}
{non_deriv_xml}
{deriv_xml}
{foot_xml}
</ownershipDocument>"""
    return body.encode("utf-8")


class TestParseHappyPath(unittest.TestCase):
    def test_parses_single_reporting_owner_single_tx(self):
        from alphalens.data.alt_data.form4_records import parse_form4_xml

        xml = _build_xml()

        records = parse_form4_xml(
            xml,
            accession_number="0000320193-25-000001",
            filing_date=date(2025, 3, 17),
        )

        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r.issuer_cik, "0000320193")
        self.assertEqual(r.ticker, "AAPL")
        self.assertEqual(r.accession_number, "0000320193-25-000001")
        self.assertEqual(r.filing_date, date(2025, 3, 17))
        self.assertEqual(r.reporting_owner_cik, "0001111111")
        self.assertEqual(r.reporting_owner_name, "Jane Doe")
        self.assertTrue(r.is_director)
        self.assertTrue(r.is_officer)
        self.assertFalse(r.is_ten_percent_owner)
        self.assertFalse(r.is_other)
        self.assertEqual(r.officer_title, "CEO")
        self.assertEqual(r.transaction_date, date(2025, 3, 15))
        self.assertEqual(r.transaction_code, "P")
        self.assertEqual(r.transaction_shares, Decimal("1000"))
        self.assertEqual(r.transaction_price_per_share, Decimal("150.25"))
        self.assertEqual(r.acquired_disposed, "A")
        self.assertFalse(r.is_amendment)
        self.assertEqual(r.footnotes, ())


class TestMultipleReportingOwners(unittest.TestCase):
    def test_joint_ceo_cfo_emits_two_records(self):
        from alphalens.data.alt_data.form4_records import parse_form4_xml

        xml = _build_xml(
            reporting_owners=[
                {
                    "cik": "1111",
                    "name": "CEO Person",
                    "is_director": "0",
                    "is_officer": "1",
                    "is_ten_percent_owner": "0",
                    "is_other": "0",
                    "officer_title": "CEO",
                },
                {
                    "cik": "2222",
                    "name": "CFO Person",
                    "is_director": "0",
                    "is_officer": "1",
                    "is_ten_percent_owner": "0",
                    "is_other": "0",
                    "officer_title": "CFO",
                },
            ],
        )

        records = parse_form4_xml(
            xml,
            accession_number="A",
            filing_date=date(2025, 3, 17),
        )

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].reporting_owner_cik, "0000001111")
        self.assertEqual(records[0].officer_title, "CEO")
        self.assertEqual(records[1].reporting_owner_cik, "0000002222")
        self.assertEqual(records[1].officer_title, "CFO")


class TestMultipleTransactionsSingleFiling(unittest.TestCase):
    def test_each_tx_becomes_record(self):
        from alphalens.data.alt_data.form4_records import parse_form4_xml

        xml = _build_xml(
            non_derivative=[
                {"date": "2025-03-15", "code": "P", "shares": "100", "price": "10.00"},
                {"date": "2025-03-15", "code": "P", "shares": "200", "price": "10.50"},
            ],
        )

        records = parse_form4_xml(
            xml,
            accession_number="A",
            filing_date=date(2025, 3, 17),
        )

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].transaction_shares, Decimal("100"))
        self.assertEqual(records[1].transaction_shares, Decimal("200"))


class TestDerivativeIgnored(unittest.TestCase):
    def test_only_non_derivative_records_returned(self):
        from alphalens.data.alt_data.form4_records import parse_form4_xml

        xml = _build_xml(
            non_derivative=[
                {"date": "2025-03-15", "code": "P", "shares": "100", "price": "10.00"},
            ],
            derivative=[
                {"date": "2025-03-15", "code": "M", "shares": "500"},
            ],
        )

        records = parse_form4_xml(
            xml,
            accession_number="A",
            filing_date=date(2025, 3, 17),
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].transaction_code, "P")


class TestAmendedForm4A(unittest.TestCase):
    def test_documenttype_4a_marks_is_amendment(self):
        from alphalens.data.alt_data.form4_records import parse_form4_xml

        xml = _build_xml(document_type="4/A")

        records = parse_form4_xml(
            xml,
            accession_number="A",
            filing_date=date(2025, 3, 17),
        )

        self.assertTrue(records[0].is_amendment)


class TestForm5Rejected(unittest.TestCase):
    def test_form_5_raises(self):
        from alphalens.data.alt_data.form4_records import Form4ParseError, parse_form4_xml

        xml = _build_xml(document_type="5")

        with self.assertRaises(Form4ParseError):
            parse_form4_xml(xml, accession_number="A", filing_date=date(2025, 3, 17))


class TestMissingRelationshipFields(unittest.TestCase):
    def test_missing_flags_default_false(self):
        from alphalens.data.alt_data.form4_records import parse_form4_xml

        xml = _build_xml(
            reporting_owners=[
                {
                    "cik": "1111",
                    "name": "Director Person",
                    "is_director": "1",
                    "is_officer": "0",
                    "is_ten_percent_owner": "0",
                    "is_other": "0",
                },
            ],
        )

        records = parse_form4_xml(
            xml,
            accession_number="A",
            filing_date=date(2025, 3, 17),
        )

        r = records[0]
        self.assertTrue(r.is_director)
        self.assertFalse(r.is_officer)
        self.assertIsNone(r.officer_title)


class TestMalformedXml(unittest.TestCase):
    def test_broken_xml_raises(self):
        from alphalens.data.alt_data.form4_records import Form4ParseError, parse_form4_xml

        with self.assertRaises(Form4ParseError):
            parse_form4_xml(
                b"<ownershipDocument><unclosed>",
                accession_number="A",
                filing_date=date(2025, 3, 17),
            )


class TestFootnotesExtracted(unittest.TestCase):
    def test_footnotes_preserved_as_id_text_tuples(self):
        from alphalens.data.alt_data.form4_records import parse_form4_xml

        xml = _build_xml(
            footnotes=[
                (
                    "F1",
                    "Transaction made pursuant to a 10b5-1 plan adopted on October 15, 2024.",
                ),
                ("F2", "Direct ownership only."),
            ],
        )

        records = parse_form4_xml(
            xml,
            accession_number="A",
            filing_date=date(2025, 3, 17),
        )

        self.assertEqual(len(records), 1)
        fids = [fid for fid, _ in records[0].footnotes]
        self.assertIn("F1", fids)
        self.assertIn("F2", fids)


class TestEmptyNonDerivativeTable(unittest.TestCase):
    def test_no_non_derivative_txs_returns_empty_list(self):
        from alphalens.data.alt_data.form4_records import parse_form4_xml

        xml = _build_xml(
            non_derivative=[],
            derivative=[{"date": "2025-03-15", "code": "M", "shares": "500"}],
        )

        records = parse_form4_xml(
            xml,
            accession_number="A",
            filing_date=date(2025, 3, 17),
        )

        self.assertEqual(records, [])


class TestTransactionCodePreserved(unittest.TestCase):
    def test_parser_does_not_filter_by_code(self):
        """Parser returns all non-derivative tx codes; filtering is M2b's job."""
        from alphalens.data.alt_data.form4_records import parse_form4_xml

        xml = _build_xml(
            non_derivative=[
                {"date": "2025-03-15", "code": "F", "shares": "100"},
            ],
        )

        records = parse_form4_xml(
            xml,
            accession_number="A",
            filing_date=date(2025, 3, 17),
        )

        self.assertEqual(records[0].transaction_code, "F")


class TestTolerantDateParse(unittest.TestCase):
    def test_iso_date_with_timezone_offset_parses_to_date(self):
        """Real AEHR Form 4 filing had tx_date '2026-04-09-05:00'.
        Strip after YYYY-MM-DD rather than raise uncaught ValueError."""
        from alphalens.data.alt_data.form4_records import parse_form4_xml

        xml = _build_xml(
            non_derivative=[
                {
                    "date": "2026-04-09-05:00",
                    "code": "S",
                    "shares": "5000",
                    "price": "68",
                },
            ],
        )

        records = parse_form4_xml(
            xml,
            accession_number="A",
            filing_date=date(2026, 4, 10),
        )

        self.assertEqual(records[0].transaction_date, date(2026, 4, 9))

    def test_malformed_date_raises_form4_parse_error(self):
        """Genuinely malformed dates must raise Form4ParseError so scorer
        can skip the filing — NOT uncaught ValueError crashing the scan."""
        from alphalens.data.alt_data.form4_records import Form4ParseError, parse_form4_xml

        xml = _build_xml(
            non_derivative=[
                {"date": "not-a-date", "code": "P", "shares": "100"},
            ],
        )

        with self.assertRaises(Form4ParseError):
            parse_form4_xml(xml, accession_number="A", filing_date=date(2026, 4, 10))


class TestMissingPriceNonePreserved(unittest.TestCase):
    def test_gift_tx_no_price_parses_as_none(self):
        from alphalens.data.alt_data.form4_records import parse_form4_xml

        xml = _build_xml(
            non_derivative=[
                {"date": "2025-03-15", "code": "G", "shares": "500", "price": None},
            ],
        )

        records = parse_form4_xml(
            xml,
            accession_number="A",
            filing_date=date(2025, 3, 17),
        )

        self.assertIsNone(records[0].transaction_price_per_share)


if __name__ == "__main__":
    unittest.main()
