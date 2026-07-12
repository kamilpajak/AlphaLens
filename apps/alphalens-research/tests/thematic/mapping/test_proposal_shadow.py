"""Tests for the V-forward proposal-shadow logger (D4).

Shadow-logs, per (theme, brief_date), BOTH candidate sources ungated:
- the LLM's pre-gate proposals (source="llm"), and
- the mechanical salience-membership set (source="mechanical") — equal-weight
  tickers named in the theme's own news (`thematic_events.primary_entities`)
  over the production 30-day catalyst window.

Uses ``unittest.TestCase`` (CI runs ``unittest discover`` — pytest-style classes
with ``tmp_path`` fixtures are silently NOT collected there).

Design: docs/research/theme_mapper_mechanical_rule_headtohead_design_2026_07_12.md §8.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.thematic.mapping import proposal_shadow as ps


def _write_events(events_dir: Path, date: str, rows: list[dict]) -> None:
    """Seed one per-date thematic_events parquet (themes + primary_entities cols)."""
    events_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        [
            {
                "news_id": f"{date}-{i}",
                "primary_entities": r["primary_entities"],
                "themes": r["themes"],
            }
            for i, r in enumerate(rows)
        ]
    )
    frame.to_parquet(events_dir / f"{date}.parquet", index=False)


class TestMechanicalSalienceCandidates(unittest.TestCase):
    def test_equal_weight_membership_theme_filtered_with_counts(self):
        # Two articles name AAPL under the 'ai' theme; one names NVDA under 'ai';
        # MSFT appears only under a DIFFERENT theme -> must be excluded.
        with tempfile.TemporaryDirectory() as tmp:
            ev = Path(tmp) / "events"
            _write_events(
                ev,
                "2026-06-10",
                [
                    {"primary_entities": ["AAPL", "NVDA"], "themes": ["ai", "chips"]},
                    {"primary_entities": ["AAPL"], "themes": ["ai"]},
                    {"primary_entities": ["MSFT"], "themes": ["cloud"]},
                ],
            )
            out = ps.mechanical_salience_candidates(
                "ai", dt.date(2026, 6, 10), events_dir=ev, lookback_days=30
            )
        by_ticker = {c["ticker"]: c["mech_article_count"] for c in out}
        self.assertEqual(by_ticker, {"AAPL": 2, "NVDA": 1})
        self.assertNotIn("MSFT", by_ticker)

    def test_respects_lookback_window(self):
        # An article 40 days before asof is OUTSIDE the 30-day window -> excluded.
        with tempfile.TemporaryDirectory() as tmp:
            ev = Path(tmp) / "events"
            _write_events(ev, "2026-05-01", [{"primary_entities": ["OLD"], "themes": ["ai"]}])
            _write_events(ev, "2026-06-09", [{"primary_entities": ["FRESH"], "themes": ["ai"]}])
            out = ps.mechanical_salience_candidates(
                "ai", dt.date(2026, 6, 10), events_dir=ev, lookback_days=30
            )
        self.assertEqual({c["ticker"] for c in out}, {"FRESH"})

    def test_tickers_uppercased(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev = Path(tmp) / "events"
            _write_events(ev, "2026-06-10", [{"primary_entities": ["aapl"], "themes": ["ai"]}])
            out = ps.mechanical_salience_candidates(
                "ai", dt.date(2026, 6, 10), events_dir=ev, lookback_days=30
            )
        self.assertEqual(out[0]["ticker"], "AAPL")

    def test_empty_when_no_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev = Path(tmp) / "events"
            ev.mkdir()
            out = ps.mechanical_salience_candidates(
                "ai", dt.date(2026, 6, 10), events_dir=ev, lookback_days=30
            )
        self.assertEqual(out, [])

    def test_scalar_string_cell_not_split_into_chars(self):
        # A malformed events parquet where a cell is a BARE string (not a list)
        # must not explode into per-character tickers ("AAPL" -> A/A/P/L).
        with tempfile.TemporaryDirectory() as tmp:
            ev = Path(tmp) / "events"
            ev.mkdir()
            pd.DataFrame([{"news_id": "x", "primary_entities": "AAPL", "themes": "ai"}]).to_parquet(
                ev / "2026-06-10.parquet", index=False
            )
            out = ps.mechanical_salience_candidates(
                "ai", dt.date(2026, 6, 10), events_dir=ev, lookback_days=30
            )
        self.assertEqual({c["ticker"] for c in out}, {"AAPL"})


class TestIterListScalarGuard(unittest.TestCase):
    def test_bare_string_returned_whole(self):
        self.assertEqual(ps._iter_list("AAPL"), ["AAPL"])

    def test_list_passthrough(self):
        self.assertEqual(ps._iter_list(["A", "B"]), ["A", "B"])

    def test_none_is_empty(self):
        self.assertEqual(ps._iter_list(None), [])


class TestBuildShadowFrame(unittest.TestCase):
    def test_both_sources_present_with_correct_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev = Path(tmp) / "events"
            _write_events(
                ev, "2026-06-10", [{"primary_entities": ["AAPL", "NVDA"], "themes": ["ai"]}]
            )
            llm = [
                {"theme": "ai", "ticker": "TSLA", "llm_confidence": 0.8},
                {"theme": "ai", "ticker": "AAPL", "llm_confidence": 0.6},
            ]
            frame = ps.build_shadow_frame(
                dt.date(2026, 6, 10),
                llm,
                events_dir=ev,
                lookback_days=30,
                mapper_config_version="cfg-x",
            )
        self.assertEqual(set(frame["source"]), {"llm", "mechanical"})
        # LLM rows carry confidence; mechanical rows carry article_count.
        llm_rows = frame[frame["source"] == "llm"]
        mech_rows = frame[frame["source"] == "mechanical"]
        self.assertEqual(set(llm_rows["ticker"]), {"TSLA", "AAPL"})
        self.assertEqual(set(mech_rows["ticker"]), {"AAPL", "NVDA"})
        self.assertTrue(llm_rows["llm_confidence"].notna().all())
        self.assertTrue(llm_rows["mech_article_count"].isna().all())
        self.assertTrue(mech_rows["mech_article_count"].notna().all())
        self.assertTrue(mech_rows["llm_confidence"].isna().all())
        # Poolability keys stamped on every row.
        self.assertTrue((frame["mapper_config_version"] == "cfg-x").all())
        self.assertTrue((frame["mech_rule_version"] == ps.MECH_RULE_VERSION).all())
        self.assertTrue((frame["proposal_shadow_version"] == ps.PROPOSAL_SHADOW_VERSION).all())
        self.assertTrue((frame["brief_date"] == dt.date(2026, 6, 10)).all())

    def test_llm_only_theme_still_logs_llm_rows_when_no_news(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev = Path(tmp) / "events"
            ev.mkdir()
            llm = [{"theme": "ai", "ticker": "TSLA", "llm_confidence": 0.8}]
            frame = ps.build_shadow_frame(
                dt.date(2026, 6, 10),
                llm,
                events_dir=ev,
                lookback_days=30,
                mapper_config_version="cfg-x",
            )
        self.assertEqual(list(frame["source"]), ["llm"])
        self.assertEqual(list(frame["ticker"]), ["TSLA"])


class TestWriteProposalShadow(unittest.TestCase):
    def test_writes_parquet_at_dated_path_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev = Path(tmp) / "events"
            _write_events(ev, "2026-06-10", [{"primary_entities": ["AAPL"], "themes": ["ai"]}])
            out_dir = Path(tmp) / "shadow"
            llm = [{"theme": "ai", "ticker": "TSLA", "llm_confidence": 0.8}]
            path = ps.write_proposal_shadow(
                dt.date(2026, 6, 10),
                llm,
                events_dir=ev,
                out_dir=out_dir,
                lookback_days=30,
                mapper_config_version="cfg-x",
            )
            self.assertEqual(path, out_dir / "2026-06-10.parquet")
            back = pd.read_parquet(path)
            self.assertEqual(set(back["source"]), {"llm", "mechanical"})

    def test_empty_llm_and_no_news_writes_nothing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev = Path(tmp) / "events"
            ev.mkdir()
            out_dir = Path(tmp) / "shadow"
            path = ps.write_proposal_shadow(
                dt.date(2026, 6, 10),
                [],
                events_dir=ev,
                out_dir=out_dir,
                lookback_days=30,
                mapper_config_version="cfg-x",
            )
            self.assertIsNone(path)
            self.assertFalse((out_dir / "2026-06-10.parquet").exists())


if __name__ == "__main__":
    unittest.main()
