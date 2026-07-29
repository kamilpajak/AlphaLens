"""Pin equality of the duplicate ``_INGEST_WATERMARK_NAME`` constant + the shared
``completed_at`` JSON key across the pipeline sentinel writer and the Django ingest
reader.

``alphalens_cli.commands.feedback._INGEST_WATERMARK_NAME`` (pipeline, writer side)
and ``edge.ingest.parquet._INGEST_WATERMARK_NAME`` (Django, reader side) must name
the exact same sentinel file — the two sides cannot share an import (the workspace
DAG forbids Django importing the pipeline CLI, and the pipeline stays Django-free).
If the filename or the ``completed_at`` key drifts between the two copies, the
Django reader silently falls back to the pure mtime gate (the sentinel it looks for
never exists) and the settled-watermark race the completion-stamp was built to
close (design memo `docs/research/edge_enrichment_completion_stamp_design_2026_07_28.md`
§1) reopens with no error anywhere — a stale-vs-in-progress parquet read can slip
back in unnoticed.

This test reads the Django reader source as TEXT rather than importing it, so it
does not need a Django settings module (research CI runs `unittest discover`
without Django configured).
"""

from __future__ import annotations

import unittest
from pathlib import Path

from alphalens_cli.commands.feedback import _INGEST_WATERMARK_NAME

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DJANGO_READER_PATH = _REPO_ROOT / "apps" / "alphalens-django" / "edge" / "ingest" / "parquet.py"


class TestIngestWatermarkParity(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(
            _DJANGO_READER_PATH.is_file(),
            f"Django ingest reader not found at {_DJANGO_READER_PATH} — repo-root "
            "derivation from this test file's __file__ may have drifted.",
        )
        self.reader_source = _DJANGO_READER_PATH.read_text()

    def test_django_reader_uses_same_sentinel_filename(self) -> None:
        expected = f'_INGEST_WATERMARK_NAME = "{_INGEST_WATERMARK_NAME}"'
        self.assertIn(
            expected,
            self.reader_source,
            "edge/ingest/parquet.py's _INGEST_WATERMARK_NAME literal must match "
            f"alphalens_cli.commands.feedback._INGEST_WATERMARK_NAME ({_INGEST_WATERMARK_NAME!r}) "
            "exactly, or the Django reader silently falls back to the pure mtime gate.",
        )

    def test_django_reader_reads_same_completed_at_key(self) -> None:
        self.assertIn(
            '"completed_at"',
            self.reader_source,
            "edge/ingest/parquet.py must read the same 'completed_at' JSON key the "
            "pipeline writer stamps.",
        )

    def test_pipeline_writer_writes_completed_at_key(self) -> None:
        writer_path = (
            _REPO_ROOT
            / "apps"
            / "alphalens-pipeline"
            / "alphalens_cli"
            / "commands"
            / "feedback.py"
        )
        self.assertTrue(writer_path.is_file(), f"pipeline writer not found at {writer_path}")
        writer_source = writer_path.read_text()
        self.assertIn(
            '"completed_at"',
            writer_source,
            "alphalens_cli/commands/feedback.py must write the 'completed_at' JSON "
            "key the Django reader expects.",
        )


if __name__ == "__main__":
    unittest.main()
