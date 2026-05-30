"""Holdout reason accumulator + Prometheus textfile flush.

The engine doesn't write Prometheus files on every drop — instead it
accumulates counts in a ``TemplateMetrics`` collector for the duration of
one CLI invocation, then flushes once via the existing
``observability.textfile.emit_domain_metrics`` surface (PR-2 / PR #311).

This keeps the "no black-box scoring" doctrine honest (every drop is
counted by reason) without forcing one disk write per article.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from alphalens_pipeline.thematic.extraction.templates.holdout import (
    ALL_HOLDOUT_REASONS,
    HOLDOUT_ALL_PREDICATES_FAILED,
    HOLDOUT_ENTITY_UNRESOLVED,
    HOLDOUT_LOW_CONFIDENCE_NO_TEMPLATE,
    HOLDOUT_NO_TEMPLATE_MATCH,
    TemplateMetrics,
)


class TestReasonEnumeration(unittest.TestCase):
    def test_reason_set_matches_design_memo(self):
        # The 4 reasons in design memo §2.4 are load-bearing for the
        # Grafana panel's by-reason breakdown. Adding/removing one
        # requires updating Prometheus rule + panel JSON in lockstep.
        self.assertEqual(
            ALL_HOLDOUT_REASONS,
            frozenset(
                {
                    HOLDOUT_NO_TEMPLATE_MATCH,
                    HOLDOUT_ENTITY_UNRESOLVED,
                    HOLDOUT_ALL_PREDICATES_FAILED,
                    HOLDOUT_LOW_CONFIDENCE_NO_TEMPLATE,
                }
            ),
        )

    def test_reason_constants_are_strings(self):
        # Plain strings (not Enum) so they slot directly into Prometheus
        # label values without coercion.
        for r in ALL_HOLDOUT_REASONS:
            self.assertIsInstance(r, str)


class TestAccumulation(unittest.TestCase):
    def test_record_drop_increments_counter(self):
        m = TemplateMetrics()
        m.record_drop(HOLDOUT_NO_TEMPLATE_MATCH)
        m.record_drop(HOLDOUT_NO_TEMPLATE_MATCH)
        m.record_drop(HOLDOUT_ENTITY_UNRESOLVED)
        snap = m.snapshot()
        self.assertEqual(snap["holdout"][HOLDOUT_NO_TEMPLATE_MATCH], 2)
        self.assertEqual(snap["holdout"][HOLDOUT_ENTITY_UNRESOLVED], 1)
        self.assertEqual(snap["holdout"][HOLDOUT_ALL_PREDICATES_FAILED], 0)

    def test_record_predicate_outcome(self):
        m = TemplateMetrics()
        m.record_predicate("is_press_release", outcome="pass")
        m.record_predicate("is_press_release", outcome="pass")
        m.record_predicate("is_press_release", outcome="fail")
        m.record_predicate("amount_mentioned", outcome="pass")
        snap = m.snapshot()
        self.assertEqual(snap["predicates"][("is_press_release", "pass")], 2)
        self.assertEqual(snap["predicates"][("is_press_release", "fail")], 1)
        self.assertEqual(snap["predicates"][("amount_mentioned", "pass")], 1)

    def test_record_template_attempt_and_match(self):
        m = TemplateMetrics()
        m.record_attempt("m_and_a_press_release")
        m.record_attempt("m_and_a_press_release")
        m.record_match("m_and_a_press_release")
        snap = m.snapshot()
        self.assertEqual(snap["attempts"]["m_and_a_press_release"], 2)
        self.assertEqual(snap["matches"]["m_and_a_press_release"], 1)

    def test_unknown_reason_raises(self):
        m = TemplateMetrics()
        with self.assertRaises(ValueError):
            m.record_drop("not_a_real_reason")

    def test_invalid_predicate_outcome_raises(self):
        m = TemplateMetrics()
        with self.assertRaises(ValueError):
            m.record_predicate("x", outcome="maybe")


class TestFlush(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._orig_env = os.environ.get("ALPHALENS_TEXTFILE_DIR")
        os.environ["ALPHALENS_TEXTFILE_DIR"] = str(self.tmpdir)

    def tearDown(self):
        if self._orig_env is None:
            os.environ.pop("ALPHALENS_TEXTFILE_DIR", None)
        else:
            os.environ["ALPHALENS_TEXTFILE_DIR"] = self._orig_env

    def test_flush_writes_all_metric_families(self):
        m = TemplateMetrics()
        m.record_attempt("m_and_a_press_release")
        m.record_match("m_and_a_press_release")
        m.record_drop(HOLDOUT_NO_TEMPLATE_MATCH)
        m.record_predicate("is_press_release", outcome="pass")
        m.flush(job="template-engine-evaluate")
        out = self.tmpdir / "alphalens_domain_template-engine-evaluate.prom"
        self.assertTrue(out.exists(), f"expected {out} to exist")
        text = out.read_text()
        # All 4 reasons appear in the flush even if their count is 0 —
        # absence-of-data and zero-count are different in Prometheus.
        for reason in ALL_HOLDOUT_REASONS:
            self.assertIn(
                f'alphalens_template_holdout_total{{reason="{reason}"}}',
                text,
            )
        # Attempt + match counters appear under template_id label.
        self.assertIn(
            'alphalens_template_attempt_total{template_id="m_and_a_press_release"}',
            text,
        )
        self.assertIn(
            'alphalens_template_match_total{template_id="m_and_a_press_release"}',
            text,
        )
        self.assertIn(
            'alphalens_template_predicate_total{name="is_press_release",outcome="pass"}',
            text,
        )

    def test_flush_is_idempotent(self):
        m = TemplateMetrics()
        m.record_drop(HOLDOUT_NO_TEMPLATE_MATCH)
        m.flush(job="t1")
        m.flush(job="t1")
        # The textfile is overwritten on the second call (single file,
        # atomic replace) — the count itself doesn't double inside the
        # accumulator just because flush ran twice.
        snap = m.snapshot()
        self.assertEqual(snap["holdout"][HOLDOUT_NO_TEMPLATE_MATCH], 1)


if __name__ == "__main__":
    unittest.main()
