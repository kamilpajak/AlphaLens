import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_research.thematic.extraction import themes


def _event_row(news_id, asof, themes_list, primary=None, confidence=0.8):
    return {
        "news_id": news_id,
        "event_type": "product_launch",
        "primary_entities": primary or [],
        "themes": themes_list,
        "sentiment": "positive",
        "second_order_implications": [],
        "confidence": confidence,
        "model": "gemini-2.5-flash",
        "extracted_at": pd.Timestamp(asof, tz="UTC"),
    }


class TestRollUp(unittest.TestCase):
    def _write(self, events_dir: Path, date: dt.date, rows: list[dict]):
        df = pd.DataFrame(rows)
        df.to_parquet(events_dir / f"{date.isoformat()}.parquet", index=False)

    def test_collects_themes_across_days(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir)
            self._write(
                events_dir,
                dt.date(2026, 5, 10),
                [_event_row("a", "2026-05-10", ["quantum_computing", "AI"])],
            )
            self._write(
                events_dir,
                dt.date(2026, 5, 15),
                [_event_row("b", "2026-05-15", ["quantum_computing", "biotech"])],
            )

            df = themes.roll_up(asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30)

            counts = dict(zip(df["theme"], df["count_window"], strict=True))
            self.assertEqual(counts["quantum_computing"], 2)
            self.assertEqual(counts["AI"], 1)
            self.assertEqual(counts["biotech"], 1)

    def test_novelty_score_uses_7d_over_30d_ratio(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir)
            # Backfill 'cybersecurity' steadily across 30 days, then a spike for 'quantum' in last 7
            for d in range(30):
                date = dt.date(2026, 5, 15) - dt.timedelta(days=d)
                self._write(
                    events_dir,
                    date,
                    [_event_row(f"cs_{d}", date.isoformat(), ["cybersecurity"])],
                )
            for d in range(7):
                date = dt.date(2026, 5, 15) - dt.timedelta(days=d)
                self._write(
                    events_dir,
                    date,
                    [
                        _event_row(f"cs_{d}_b", date.isoformat(), ["cybersecurity"]),
                        _event_row(f"q_{d}", date.isoformat(), ["quantum_computing"]),
                    ],
                )

            df = themes.roll_up(asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30)
            df_indexed = df.set_index("theme")

            # Cybersecurity steady → low novelty
            self.assertLess(df_indexed.loc["cybersecurity", "novelty_score"], 1.5)
            # Quantum spike → high novelty (≥3x baseline)
            self.assertGreaterEqual(df_indexed.loc["quantum_computing", "novelty_score"], 3.0)

    def test_first_seen_and_latest_seen_dates_tracked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir)
            self._write(
                events_dir,
                dt.date(2026, 5, 10),
                [_event_row("a", "2026-05-10", ["AI"])],
            )
            self._write(
                events_dir,
                dt.date(2026, 5, 14),
                [_event_row("b", "2026-05-14", ["AI"])],
            )

            df = themes.roll_up(asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30)
            ai = df[df["theme"] == "AI"].iloc[0]
            self.assertEqual(ai["first_seen"].date(), dt.date(2026, 5, 10))
            self.assertEqual(ai["latest_seen"].date(), dt.date(2026, 5, 14))

    def test_ignores_events_outside_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir)
            # event 60 days ago, well outside default 30d window
            old_date = dt.date(2026, 5, 15) - dt.timedelta(days=60)
            self._write(events_dir, old_date, [_event_row("old", old_date.isoformat(), ["AI"])])

            self._write(
                events_dir,
                dt.date(2026, 5, 14),
                [_event_row("new", "2026-05-14", ["biotech"])],
            )

            df = themes.roll_up(asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30)
            self.assertEqual(set(df["theme"]), {"biotech"})

    def test_empty_events_dir_returns_empty_frame(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            df = themes.roll_up(asof=dt.date(2026, 5, 15), events_dir=Path(tmpdir), window_days=30)
            self.assertEqual(len(df), 0)
            for col in ["theme", "count_window", "novelty_score", "first_seen", "latest_seen"]:
                self.assertIn(col, df.columns)

    def test_flag_novel_uses_threshold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir)
            # Mix one steady + one spiking
            for d in range(30):
                date = dt.date(2026, 5, 15) - dt.timedelta(days=d)
                self._write(events_dir, date, [_event_row(f"s_{d}", date.isoformat(), ["steady"])])
            for d in range(3):
                date = dt.date(2026, 5, 15) - dt.timedelta(days=d)
                self._write(
                    events_dir, date, [_event_row(f"q_{d}", date.isoformat(), ["novel_theme"])]
                )

            df = themes.roll_up(asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30)
            novel = themes.flag_novel(df, threshold=3.0)
            self.assertIn("novel_theme", set(novel["theme"]))
            self.assertNotIn("steady", set(novel["theme"]))


if __name__ == "__main__":
    unittest.main()
