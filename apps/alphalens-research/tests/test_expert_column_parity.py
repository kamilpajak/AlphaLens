"""Cross-boundary parity guard for the expert-blob column sets.

The Django ingest (``apps/alphalens-django/briefs/ingest/parquet.py``)
keeps a HAND-COPIED mirror ``_EXPERT_COLUMNS`` of the pipeline's per-expert
column tuples, because the slim Django image must NOT import
``alphalens_pipeline``. The existing Django-side tests
(``test_expert_columns_match_frozen_{oneil,panel,buffett}_tuple``) only
compare that mirror against ANOTHER Django-side literal — so both copies can
drift together away from the real pipeline tuples without any test failing.
A pipeline column addition would then silently drop from the assembled
``expert_assessments`` blob at ingest.

This test closes that gap from the research side, where ``alphalens_pipeline``
IS importable: it imports the REAL pipeline tuples and reads the Django mirror
as DATA (ast-parsed from source, never imported — the dependency DAG forbids
research importing Django, and Django needs settings to import anyway). If the
pipeline registry grows a column and the Django mirror is not updated in
lockstep, this fails loud in CI.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from alphalens_pipeline.experts.buffett.expert import BuffettExpert
from alphalens_pipeline.experts.disagreement import PANEL_COLUMNS
from alphalens_pipeline.experts.oneil.quant_enrichment import ONEIL_COLUMNS

REPO_ROOT = Path(__file__).resolve().parents[3]
DJANGO_PARQUET = REPO_ROOT / "apps" / "alphalens-django" / "briefs" / "ingest" / "parquet.py"


def _parse_django_expert_columns() -> dict[str, tuple[str, ...]]:
    """Return the Django ``_EXPERT_COLUMNS`` dict, parsed from source as a
    literal (no import — Django is not importable from the research tree)."""
    tree = ast.parse(DJANGO_PARQUET.read_text(), filename=str(DJANGO_PARQUET))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AnnAssign, ast.Assign)):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        if (
            len(targets) == 1
            and isinstance(targets[0], ast.Name)
            and targets[0].id == "_EXPERT_COLUMNS"
            and node.value is not None
        ):
            value = ast.literal_eval(node.value)
            return {k: tuple(v) for k, v in value.items()}
    raise AssertionError(f"_EXPERT_COLUMNS not found in {DJANGO_PARQUET}")


class TestExpertColumnParity(unittest.TestCase):
    """The Django mirror must equal the pipeline's authoritative column tuples."""

    @classmethod
    def setUpClass(cls):
        cls.django = _parse_django_expert_columns()

    def test_django_file_exists(self):
        # Positive control: the parse target must exist, so the parity asserts
        # below cannot silently no-op if the path drifts.
        self.assertTrue(DJANGO_PARQUET.exists(), msg=f"missing {DJANGO_PARQUET}")
        self.assertIn("oneil", self.django)

    def test_oneil_mirror_matches_pipeline(self):
        self.assertEqual(self.django["oneil"], tuple(ONEIL_COLUMNS))

    def test_panel_mirror_matches_pipeline(self):
        self.assertEqual(self.django["panel"], tuple(PANEL_COLUMNS))

    def test_buffett_mirror_matches_pipeline(self):
        # BuffettExpert.column_names = BUFFETT_COLUMNS + qual content + qual
        # provenance — the authoritative buffett blob column set.
        self.assertEqual(self.django["buffett"], tuple(BuffettExpert.column_names))


if __name__ == "__main__":
    unittest.main()
