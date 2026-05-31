"""Smoke for ``alphalens templates {validate,evaluate}``.

The CLI is the analyst's primary surface for iterating on templates without
running the full pipeline. ``validate`` is fast enough to wire as a
pre-commit hook; ``evaluate`` runs the engine over an existing news
parquet corpus + prints per-template match-rate.
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

import pandas as pd
from alphalens_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def _good_template_file() -> Path:
    tmp = Path(tempfile.mkdtemp()) / "smoke_good.yaml"
    tmp.write_text(
        textwrap.dedent(
            """\
            template_id: smoke_good
            event_type: m_and_a
            description: "smoke ok"
            article_predicates:
              - is_press_release
            entity_requirements:
              acquirer:
                type: company
                required: true
            extraction:
              - field: acquirer_ticker
                source: "entity:acquirer"
            """
        )
    )
    return tmp


def _bad_template_file() -> Path:
    tmp = Path(tempfile.mkdtemp()) / "smoke_bad.yaml"
    tmp.write_text(
        textwrap.dedent(
            """\
            template_id: smoke_bad
            event_type: not_a_real_event
            description: ""
            article_predicates:
              - unknown_predicate
            entity_requirements: {}
            extraction: []
            """
        )
    )
    return tmp


class TestValidateCommand(unittest.TestCase):
    def test_validate_good_template_exits_zero(self):
        path = _good_template_file()
        result = runner.invoke(app, ["templates", "validate", str(path)])
        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("ok", result.stdout.lower())

    def test_validate_bad_template_exits_nonzero(self):
        path = _bad_template_file()
        result = runner.invoke(app, ["templates", "validate", str(path)])
        self.assertNotEqual(result.exit_code, 0)
        # Output must mention the offending key so pre-commit hook output
        # is useful to the analyst.
        out = result.stdout.lower()
        self.assertTrue("event_type" in out or "unknown_predicate" in out or "predicate" in out)

    def test_validate_directory_scans_all_yaml(self):
        # ``validate <dir>`` runs the schema check on every *.yaml file
        # in the directory — the natural pre-commit shape ("validate the
        # whole template library").
        tmpdir = Path(tempfile.mkdtemp())
        good = _good_template_file()
        (tmpdir / good.name).write_text(good.read_text())
        result = runner.invoke(app, ["templates", "validate", str(tmpdir)])
        self.assertEqual(result.exit_code, 0, result.stdout)


class TestEvaluateCommand(unittest.TestCase):
    def setUp(self):
        # Build a tiny parquet corpus that mimics ``~/.alphalens/thematic_news``.
        self.tmpdir = Path(tempfile.mkdtemp())
        df = pd.DataFrame(
            [
                {
                    "id": "bw:1",
                    "source": "businesswire",
                    "timestamp": pd.Timestamp("2026-05-30T10:00:00Z"),
                    "tickers": ["NVDA", "XYZ"],
                    "title": "NVDA announces $5 billion acquisition of XYZ",
                    "body": "NVIDIA today announced a $5 billion all-cash deal.",
                    "url": "https://www.businesswire.com/news/x",
                    "keywords": [],
                    "extra": "{}",
                },
                {
                    "id": "sa:2",
                    "source": "seekingalpha",
                    "timestamp": pd.Timestamp("2026-05-30T11:00:00Z"),
                    "tickers": ["NVDA"],
                    "title": "Top 5 M&A deals — opinion",
                    "body": "Some commentary about deals.",
                    "url": "https://seekingalpha.com/article/x",
                    "keywords": [],
                    "extra": "{}",
                },
            ]
        )
        self.corpus = self.tmpdir / "2026-05-30.parquet"
        df.to_parquet(self.corpus)

    def test_evaluate_prints_per_template_match_rate(self):
        # Point at a tiny template library (not the ship templates) so the
        # test stays hermetic + fast.
        tmpl_dir = Path(tempfile.mkdtemp())
        good = _good_template_file()
        (tmpl_dir / good.name).write_text(good.read_text())
        result = runner.invoke(
            app,
            [
                "templates",
                "evaluate",
                str(self.corpus),
                "--templates-dir",
                str(tmpl_dir),
            ],
        )
        self.assertEqual(result.exit_code, 0, result.stdout)
        # Per-template line printed.
        self.assertIn("smoke_good", result.stdout)
        # The output mentions match counts so the analyst can scan it.
        out = result.stdout.lower()
        self.assertTrue("match" in out or "rate" in out)


if __name__ == "__main__":
    unittest.main()
