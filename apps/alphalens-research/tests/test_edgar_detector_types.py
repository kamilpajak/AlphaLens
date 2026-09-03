import unittest
from datetime import UTC, datetime


class TestEventDataclass(unittest.TestCase):
    def test_event_holds_required_fields(self):
        from alphalens_pipeline.edgar_detector.types import Event, FormType

        filed_at = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)
        event = Event(
            ticker="AAPL",
            form_type=FormType.FORM_8K,
            accession_number="0000320193-26-000001",
            filed_at=filed_at,
            url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/0000320193-26-000001-index.htm",
            raw_data={"filer_cik": "0000320193", "items": ["2.02"]},
        )

        self.assertEqual(event.ticker, "AAPL")
        self.assertEqual(event.form_type, FormType.FORM_8K)
        self.assertEqual(event.accession_number, "0000320193-26-000001")
        self.assertEqual(event.filed_at, filed_at)
        self.assertIn("sec.gov", event.url)
        self.assertEqual(event.raw_data["items"], ["2.02"])

    def test_event_equality_by_accession_number(self):
        from alphalens_pipeline.edgar_detector.types import Event, FormType

        filed_at = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)
        event_a = Event(
            ticker="AAPL",
            form_type=FormType.FORM_4,
            accession_number="SAME-ACC-123",
            filed_at=filed_at,
            url="https://x",
            raw_data={},
        )
        event_b = Event(
            ticker="AAPL",
            form_type=FormType.FORM_4,
            accession_number="SAME-ACC-123",
            filed_at=filed_at,
            url="https://y",
            raw_data={"different": "data"},
        )
        event_c = Event(
            ticker="AAPL",
            form_type=FormType.FORM_4,
            accession_number="DIFFERENT-ACC",
            filed_at=filed_at,
            url="https://x",
            raw_data={},
        )

        self.assertEqual(event_a, event_b)
        self.assertNotEqual(event_a, event_c)
        self.assertEqual(hash(event_a), hash(event_b))

    def test_form_type_enum_covers_mvp_forms(self):
        from alphalens_pipeline.edgar_detector.types import FormType

        required = {
            "FORM_8K",
            "FORM_4",
            "FORM_13D",
            "FORM_13G",
            "FORM_13D_A",
            "FORM_13G_A",
        }
        actual = {member.name for member in FormType}
        missing = required - actual
        self.assertFalse(missing, f"Missing FormType members: {missing}")

    def test_form_type_values_map_to_sec_form_strings(self):
        from alphalens_pipeline.edgar_detector.types import FormType

        self.assertEqual(FormType.FORM_8K.value, "8-K")
        self.assertEqual(FormType.FORM_4.value, "4")
        self.assertEqual(FormType.FORM_13D.value, "SC 13D")
        self.assertEqual(FormType.FORM_13G.value, "SC 13G")

    def test_form_type_from_sec_string(self):
        from alphalens_pipeline.edgar_detector.types import FormType

        self.assertEqual(FormType.from_sec_string("8-K"), FormType.FORM_8K)
        self.assertEqual(FormType.from_sec_string("4"), FormType.FORM_4)
        self.assertEqual(FormType.from_sec_string("SC 13D/A"), FormType.FORM_13D_A)
        self.assertIsNone(FormType.from_sec_string("10-K"))

    def test_form_type_from_sec_string_accepts_2024_schedule_names(self):
        """SEC renamed beneficial-ownership forms in the Dec-2024 structured-data
        rule: EDGAR now emits ``SCHEDULE 13D`` / ``SCHEDULE 13G`` (+ ``/A``) instead
        of the legacy ``SC 13D`` / ``SC 13G``. Both spellings must map to the same
        members, otherwise the detector silently drops every activist filing
        (issue #1263)."""
        from alphalens_pipeline.edgar_detector.types import FormType

        self.assertEqual(FormType.from_sec_string("SCHEDULE 13D"), FormType.FORM_13D)
        self.assertEqual(FormType.from_sec_string("SCHEDULE 13D/A"), FormType.FORM_13D_A)
        self.assertEqual(FormType.from_sec_string("SCHEDULE 13G"), FormType.FORM_13G)
        self.assertEqual(FormType.from_sec_string("SCHEDULE 13G/A"), FormType.FORM_13G_A)
        # legacy spellings keep working (pre-2025 index rows, tests, fixtures)
        self.assertEqual(FormType.from_sec_string("SC 13D"), FormType.FORM_13D)
        self.assertEqual(FormType.from_sec_string("SC 13G/A"), FormType.FORM_13G_A)

    def test_form_type_from_sec_string_rejects_unknown_schedule_names(self):
        """Positive control: the alias table must not turn into a prefix match."""
        from alphalens_pipeline.edgar_detector.types import FormType

        self.assertIsNone(FormType.from_sec_string("SCHEDULE 13F"))
        self.assertIsNone(FormType.from_sec_string("SCHEDULE 13D-A"))
        self.assertIsNone(FormType.from_sec_string("schedule 13d"))
        # exact match only: the Atom ``category/@term`` is machine-generated,
        # so surrounding whitespace is a defect upstream, not something to absorb
        self.assertIsNone(FormType.from_sec_string("SCHEDULE 13D "))
        self.assertIsNone(FormType.from_sec_string(" SCHEDULE 13D"))


if __name__ == "__main__":
    unittest.main()
