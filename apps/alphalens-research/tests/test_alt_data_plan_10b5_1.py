import unittest
from datetime import date


class TestDetectPlanReference(unittest.TestCase):
    def test_no_10b5_1_mention_returns_false(self):
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import extract_10b5_1_adoption

        has, adopted = extract_10b5_1_adoption("Direct ownership only.")
        self.assertFalse(has)
        self.assertIsNone(adopted)

    def test_plain_10b5_1_mention_detected(self):
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import extract_10b5_1_adoption

        has, _ = extract_10b5_1_adoption("Trade made under a 10b5-1 plan.")
        self.assertTrue(has)

    def test_rule_prefix_detected(self):
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import extract_10b5_1_adoption

        has, _ = extract_10b5_1_adoption("Pursuant to Rule 10b5-1.")
        self.assertTrue(has)

    def test_case_insensitive(self):
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import extract_10b5_1_adoption

        has, _ = extract_10b5_1_adoption("RULE 10B5-1 PLAN")
        self.assertTrue(has)

    def test_dash_variant_10b5_1(self):
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import extract_10b5_1_adoption

        has, _ = extract_10b5_1_adoption("Rule 10b5 - 1 plan")
        self.assertTrue(has)


class TestIsoDatePattern(unittest.TestCase):
    def test_iso_date_in_structured_adoption(self):
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import extract_10b5_1_adoption

        has, adopted = extract_10b5_1_adoption(
            "Made pursuant to a Rule 10b5-1 trading plan adopted on 2025-10-15."
        )
        self.assertTrue(has)
        self.assertEqual(adopted, date(2025, 10, 15))


class TestSpelledMonthPattern(unittest.TestCase):
    def test_full_month_name(self):
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import extract_10b5_1_adoption

        _, adopted = extract_10b5_1_adoption(
            "Transaction was made pursuant to a Rule 10b5-1 trading plan "
            "adopted by the Reporting Person on October 15, 2025."
        )
        self.assertEqual(adopted, date(2025, 10, 15))

    def test_abbreviated_month_name(self):
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import extract_10b5_1_adoption

        _, adopted = extract_10b5_1_adoption("Rule 10b5-1 plan adopted on Oct 3, 2023.")
        self.assertEqual(adopted, date(2023, 10, 3))


class TestUsNumericPattern(unittest.TestCase):
    def test_slash_dates_us_convention(self):
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import extract_10b5_1_adoption

        _, adopted = extract_10b5_1_adoption("Trade per Rule 10b5-1 plan dated 10/15/2025.")
        self.assertEqual(adopted, date(2025, 10, 15))

    def test_slash_dates_two_digit_year(self):
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import extract_10b5_1_adoption

        _, adopted = extract_10b5_1_adoption("10b5-1 plan entered on 10/15/25.")
        self.assertEqual(adopted, date(2025, 10, 15))


class TestPre2023FreeTextVariants(unittest.TestCase):
    def test_adopted_a_10b5_1_plan_on(self):
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import extract_10b5_1_adoption

        _, adopted = extract_10b5_1_adoption(
            "The reporting person adopted a 10b5-1 plan on January 12, 2022."
        )
        self.assertEqual(adopted, date(2022, 1, 12))

    def test_entered_into_variant(self):
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import extract_10b5_1_adoption

        _, adopted = extract_10b5_1_adoption(
            "Mr. Smith entered into a Rule 10b5-1 plan on 3/5/2021."
        )
        self.assertEqual(adopted, date(2021, 3, 5))


class TestUnparseable(unittest.TestCase):
    def test_plan_mentioned_no_date_returns_true_none(self):
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import extract_10b5_1_adoption

        has, adopted = extract_10b5_1_adoption("Transaction made pursuant to a Rule 10b5-1 plan.")
        self.assertTrue(has)
        self.assertIsNone(adopted)

    def test_invalid_date_returns_none(self):
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import extract_10b5_1_adoption

        has, adopted = extract_10b5_1_adoption(
            "10b5-1 plan adopted on 2025-13-45."  # invalid month/day
        )
        self.assertTrue(has)
        self.assertIsNone(adopted)


class TestAdoptionAgeHelper(unittest.TestCase):
    def test_plan_age_days_computes_delta(self):
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import plan_age_days

        age = plan_age_days(adoption_date=date(2025, 1, 1), asof=date(2025, 4, 1))
        self.assertEqual(age, 90)

    def test_plan_age_days_unknown_adoption_returns_none(self):
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import plan_age_days

        self.assertIsNone(plan_age_days(adoption_date=None, asof=date(2025, 4, 1)))


class TestMultipleDatesInFootnote(unittest.TestCase):
    def test_prefers_date_nearest_adoption_keyword(self):
        """Footnote may reference multiple dates (e.g. plan adopted + amended)."""
        from alphalens_pipeline.data.alt_data.plan_10b5_1 import extract_10b5_1_adoption

        _, adopted = extract_10b5_1_adoption(
            "10b5-1 plan originally adopted on March 1, 2024 and amended May 10, 2024."
        )
        self.assertEqual(adopted, date(2024, 3, 1))


if __name__ == "__main__":
    unittest.main()
