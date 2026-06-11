"""Tests for the pure 10-K section splitter (#506).

`split_10k_sections` carves the plain-text 10-K into its three Buffett-relevant
items (Item 1 Business, Item 1A Risk Factors, Item 7 MD&A) by regex on the
case-insensitive item headings. It is pure (no I/O, no SEC calls). A heading
that is not found yields ``None`` for that section; each found section is
truncated to a character cap; junk text yields all-None and never crashes.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.buffett.tenk_sections import (
    TenKSections,
    split_10k_sections,
)

# A synthetic 10-K with the three target headings in canonical order plus a
# couple of neighbours that bound them (Item 1B, Item 7A, Item 8).
_SYNTHETIC_10K = (
    "PART I "
    "Item 1. Business "
    "We design and sell widgets to enterprise customers worldwide. "
    "Item 1A. Risk Factors "
    "Our business depends on a small number of large customers. "
    "Item 1B. Unresolved Staff Comments None. "
    "Item 2. Properties We lease offices. "
    "Item 7. Management's Discussion and Analysis "
    "Revenue grew on strong demand for the new product line. "
    "Item 7A. Quantitative and Qualitative Disclosures None. "
    "Item 8. Financial Statements See the notes."
)


class TestSplit10KSections(unittest.TestCase):
    def test_splits_three_sections(self):
        sections = split_10k_sections(_SYNTHETIC_10K)
        self.assertIsInstance(sections, TenKSections)
        assert sections.item_1 is not None
        assert sections.item_1a is not None
        assert sections.item_7 is not None
        self.assertIn("widgets", sections.item_1)
        # Item 1 must stop before Item 1A (bounded by the next heading).
        self.assertNotIn("small number of large customers", sections.item_1)
        self.assertIn("small number of large customers", sections.item_1a)
        # Item 1A stops before Item 1B.
        self.assertNotIn("Unresolved Staff Comments", sections.item_1a)
        self.assertIn("strong demand", sections.item_7)
        # Item 7 stops before Item 7A / Item 8.
        self.assertNotIn("Financial Statements", sections.item_7)

    def test_missing_heading_yields_none(self):
        text = (
            "Item 1. Business We make things. "
            "Item 7. Management's Discussion And Analysis We discuss things."
        )
        sections = split_10k_sections(text)
        self.assertIsNotNone(sections.item_1)
        self.assertIsNone(sections.item_1a)
        self.assertIsNotNone(sections.item_7)

    def test_truncation_cap_respected(self):
        body = "x" * 5000
        text = (
            f"Item 1. Business {body} Item 1A. Risk Factors {body} Item 7. MD&A {body} Item 8. End"
        )
        sections = split_10k_sections(text, max_chars_per_section=100)
        assert sections.item_1 is not None
        assert sections.item_1a is not None
        assert sections.item_7 is not None
        self.assertLessEqual(len(sections.item_1), 100)
        self.assertLessEqual(len(sections.item_1a), 100)
        self.assertLessEqual(len(sections.item_7), 100)

    def test_junk_text_all_none_no_crash(self):
        sections = split_10k_sections("lorem ipsum dolor sit amet, no item headings here")
        self.assertIsNone(sections.item_1)
        self.assertIsNone(sections.item_1a)
        self.assertIsNone(sections.item_7)

    def test_empty_string_all_none(self):
        sections = split_10k_sections("")
        self.assertIsNone(sections.item_1)
        self.assertIsNone(sections.item_1a)
        self.assertIsNone(sections.item_7)

    def test_case_insensitive_headings(self):
        text = (
            "ITEM 1. BUSINESS we sell stuff. "
            "ITEM 1A. RISK FACTORS risky. "
            "ITEM 7. MANAGEMENT'S DISCUSSION we manage. "
            "ITEM 8. FINANCIAL STATEMENTS end."
        )
        sections = split_10k_sections(text)
        assert sections.item_1 is not None
        assert sections.item_1a is not None
        assert sections.item_7 is not None
        self.assertIn("sell stuff", sections.item_1)
        self.assertIn("risky", sections.item_1a)
        self.assertIn("we manage", sections.item_7)


class TestTableOfContentsIsSkipped(unittest.TestCase):
    """Real 10-Ks open with a TABLE OF CONTENTS that lists every item heading
    next to a page number, BEFORE the actual section bodies. Picking the FIRST
    occurrence of "Item 1." grabs the TOC entry ("Business 3") instead of the
    real Business section, starving the qualitative LLM. The splitter must pick
    the body-bearing occurrence (observed live on Macy's 10-K, 2026-06-11).
    """

    _TOC_10K = (
        "PART I "
        # Table of contents: each heading is followed almost immediately by the
        # NEXT heading + a page number (tiny inter-heading spans).
        "Item 1. Business 3 "
        "Item 1A. Risk Factors 7 "
        "Item 1B. Unresolved Staff Comments 21 "
        "Item 7. Management's Discussion and Analysis 22 "
        "Item 8. Financial Statements 40 "
        # The real sections, far down the document, with substantive bodies.
        "Item 1. Business "
        + ("We operate department stores selling apparel and home goods nationwide. " * 8)
        + "Item 1A. Risk Factors "
        + ("Consumer spending is cyclical and competition is intense. " * 8)
        + "Item 1B. Unresolved Staff Comments None. "
        + "Item 7. Management's Discussion and Analysis "
        + ("Comparable sales declined while margins held on cost discipline. " * 8)
        + "Item 8. Financial Statements See the accompanying notes."
    )

    def test_returns_real_body_not_toc_entry(self):
        sections = split_10k_sections(self._TOC_10K)
        assert sections.item_1 is not None
        assert sections.item_1a is not None
        assert sections.item_7 is not None
        # The real bodies are long; the TOC fragments ("Business 3") are tiny.
        self.assertGreater(len(sections.item_1), 100)
        self.assertIn("department stores", sections.item_1)
        self.assertNotEqual(sections.item_1.strip(), "Business 3")
        self.assertIn("cyclical", sections.item_1a)
        self.assertIn("Comparable sales", sections.item_7)


class TestItem8FinancialStatements(unittest.TestCase):
    """Item 8 (Financial Statements and Supplementary Data) is the fourth
    Buffett-relevant section (#505). Unlike Item 1 / 1A / 7, Item 8 frequently
    does NOT carry its statements inline: many filers put a one-line pointer
    ("the financial statements are filed under Item 15") under the Item 8
    heading and place the real statements in a back-of-document block. The
    splitter returns the inline body when it is substantive, and falls back to
    a financial-statements anchor scan when the inline Item 8 is only a stub.
    """

    def test_extracts_inline_item_8_statements(self):
        # Item 8 carries the full statements inline, bounded by Item 9.
        text = (
            "Item 7. Management's Discussion and Analysis Revenue grew. "
            "Item 8. Financial Statements and Supplementary Data "
            + (
                "CONSOLIDATED BALANCE SHEET Cash and equivalents 100 Total assets 500 "
                "Total liabilities 200 Stockholders equity 300. " * 6
            )
            + "Item 9. Changes in and Disagreements with Accountants None."
        )
        sections = split_10k_sections(text)
        assert sections.item_8 is not None
        self.assertIn("CONSOLIDATED BALANCE SHEET", sections.item_8)
        self.assertIn("Total assets 500", sections.item_8)
        # Item 8 stops before Item 9.
        self.assertNotIn("Disagreements with Accountants", sections.item_8)

    def test_incorporated_by_reference_falls_back_to_anchor(self):
        # The inline Item 8 body is a short pointer (below the stub floor); the
        # real statements live in a later block headed by an anchor phrase.
        text = (
            "Item 8. Financial Statements and Supplementary Data "
            "The financial statements required by this item are filed as part "
            "of this report under Item 15. "
            "Item 9. Changes in and Disagreements None. "
            "Item 15. Exhibits and Financial Statement Schedules "
            + (
                "CONSOLIDATED BALANCE SHEET Cash 100 Total assets 900 Total "
                "liabilities 400 Stockholders equity 500 detailed footnotes. " * 6
            )
        )
        sections = split_10k_sections(text)
        assert sections.item_8 is not None
        # Fallback grabbed the real statements block, not the bare pointer.
        self.assertIn("CONSOLIDATED BALANCE SHEET", sections.item_8)
        self.assertIn("Total assets 900", sections.item_8)
        self.assertNotIn("filed as part of this report", sections.item_8)

    def test_item_8_none_when_absent_and_no_anchor(self):
        # No Item 8 heading AND no financial-statements anchor -> None.
        text = (
            "Item 1. Business We make widgets. "
            "Item 7. Management's Discussion and Analysis We grew."
        )
        sections = split_10k_sections(text)
        self.assertIsNone(sections.item_8)

    def test_item_8_truncated_to_max_chars(self):
        text = (
            "Item 8. Financial Statements and Supplementary Data "
            + ("CONSOLIDATED BALANCE SHEET line item values and notes here. " * 50)
            + "Item 9. Other None."
        )
        # Item 8 carries its OWN cap (tighter than the narrative sections by
        # design — it is mostly numeric tables).
        sections = split_10k_sections(text, max_chars_item_8=120)
        assert sections.item_8 is not None
        self.assertLessEqual(len(sections.item_8), 120)

    def test_adding_item_8_leaves_items_1_1a_7_unchanged(self):
        # Regression: the Item 8 addition must not perturb the existing three
        # sections for the canonical synthetic 10-K.
        sections = split_10k_sections(_SYNTHETIC_10K)
        assert sections.item_1 is not None
        self.assertIn("widgets", sections.item_1)
        self.assertNotIn("small number of large customers", sections.item_1)
        assert sections.item_1a is not None
        self.assertIn("small number of large customers", sections.item_1a)
        self.assertNotIn("Unresolved Staff Comments", sections.item_1a)
        assert sections.item_7 is not None
        self.assertIn("strong demand", sections.item_7)


if __name__ == "__main__":
    unittest.main()
