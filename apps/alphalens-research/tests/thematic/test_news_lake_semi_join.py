"""The lake read is restricted to the news ids some event actually points at.

Measured on the production lake, 30-day window, asof 2026-11-08: of 12,754 ids
the lake holds, **6,579 (51.6%) are referenced by no event at all**, and of the
7,440 ids only the lake has, just 861 (11.6%) have an event. The lake stores the
raw per-source union from BEFORE dedup, clustering, the single-day filter and
the cap, while events are created only from VIEW rows — so most of what a full
lake read costs cannot join by construction.

That read reached 10.01 GB RSS on a 30-day window and was killed. Restricting it
to the ids in hand attacks the cause rather than the bytes per row, and unlike
pruning columns it leaves the longest-body rule untouched.

Both read sites already hold the events frame BEFORE they read the news
(`catalyst_resolver.py:645` then `:661`, and `:840` then `:847`), and the first
one holds it already narrowed to the theme — so the restriction is per theme,
not per window.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.thematic.mapping import catalyst_resolver, news_source

_ASOF = dt.date(2026, 11, 5)
_THEME = "quantum_computing"
_OTHER_THEME = "steel"
_TICKER = "IONQ"
_BODY = "The press release text."


def _news_row(news_id: str, *, url: str | None = None) -> dict:
    return {
        "id": news_id,
        "source": "edgar_press_release",
        "timestamp": pd.Timestamp(f"{_ASOF.isoformat()}T12:00:00Z"),
        "tickers": [_TICKER],
        "title": f"Headline for {news_id}",
        "body": _BODY,
        "url": url or f"https://example.test/{news_id}",
        "keywords": [],
        "extra": "{}",
        "ingested_at": pd.Timestamp(f"{_ASOF.isoformat()}T12:05:00Z"),
    }


def _events_row(news_id: str, theme: str) -> dict:
    return {
        "news_id": news_id,
        "event_type": "contract_award",
        "primary_entities": [_TICKER],
        "themes": [theme],
        "sentiment": "positive",
        "second_order_implications": ["Quantum hardware suppliers may benefit."],
        "confidence": 0.9,
        "extraction_method": "flash",
        "template_id": None,
        "template_fields_json": None,
        "model": "test",
        "extracted_at": pd.Timestamp(f"{_ASOF.isoformat()}T13:00:00Z"),
    }


class _Stores:
    def __init__(self, root: Path) -> None:
        self.view = root / "thematic_news"
        self.lake = root / "thematic_news_lake"
        self.events = root / "thematic_events"
        for d in (self.view, self.lake, self.events):
            d.mkdir(parents=True, exist_ok=True)

    def write_view(self, rows: list[dict]) -> None:
        pd.DataFrame(rows).to_parquet(self.view / f"{_ASOF.isoformat()}.parquet", index=False)

    def write_lake_run(self, stamp: str, rows: list[dict]) -> None:
        session = self.lake / f"session_date={_ASOF.isoformat()}"
        session.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(session / f"run={stamp}.parquet", index=False)

    def write_events(self, rows: list[dict]) -> None:
        pd.DataFrame(rows).to_parquet(self.events / f"{_ASOF.isoformat()}.parquet", index=False)


class TestTheLakeReadIsRestricted(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stores = _Stores(Path(tmp.name))
        self.stores.write_lake_run("20261105T010000000Z", [_news_row("wanted"), _news_row("junk")])

    def test_only_the_ids_asked_for_come_back(self) -> None:
        got = news_source.load_lake_window(
            self.stores.lake, _ASOF, lookback_days=30, needed_ids=frozenset({"wanted"})
        )
        self.assertEqual(list(got["id"]), ["wanted"])

    def test_no_restriction_reads_everything(self) -> None:
        """Back-compatible: the parameter is optional and absent means today."""
        got = news_source.load_lake_window(self.stores.lake, _ASOF, lookback_days=30)
        self.assertEqual(sorted(got["id"]), ["junk", "wanted"])

    def test_an_empty_id_set_reads_nothing(self) -> None:
        """A theme with no events must not pay for a lake read at all."""
        with mock.patch.object(news_source.pd, "read_parquet") as spy:
            got = news_source.load_lake_window(
                self.stores.lake, _ASOF, lookback_days=30, needed_ids=frozenset()
            )
        self.assertTrue(got.empty)
        spy.assert_not_called()


class TestTheResolverAsksOnlyForWhatItNeeds(unittest.TestCase):
    """The behavioural guard: the restriction must be THE THEME'S ids."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stores = _Stores(Path(tmp.name))
        # Two orphans, one per theme. Neither is in the view.
        self.stores.write_view([_news_row("in_view")])
        self.stores.write_lake_run(
            "20261105T010000000Z", [_news_row("mine"), _news_row("other_theme")]
        )
        self.stores.write_events(
            [_events_row("mine", _THEME), _events_row("other_theme", _OTHER_THEME)]
        )

    def test_the_other_theme_s_news_row_is_never_read(self) -> None:
        seen: list[frozenset[str]] = []
        real = news_source.load_lake_window

        def spy(lake_dir, asof, lookback_days, *, needed_ids=None):
            seen.append(frozenset(needed_ids) if needed_ids is not None else None)
            return real(lake_dir, asof, lookback_days, needed_ids=needed_ids)

        with mock.patch.object(news_source, "READ_NEWS_LAKE", True):
            with mock.patch.object(news_source, "load_lake_window", spy):
                payload = catalyst_resolver.find_trigger_event(
                    theme=_THEME,
                    asof=_ASOF,
                    events_dir=self.stores.events,
                    news_dir=self.stores.view,
                )
        self.assertIsNotNone(payload)
        self.assertEqual(payload.url, "https://example.test/mine")
        self.assertTrue(seen, "the lake was never read")
        for asked in seen:
            self.assertIsNotNone(asked, "the resolver read the lake unrestricted")
            self.assertNotIn(
                "other_theme",
                asked,
                "the resolver asked for a news id belonging to a different theme",
            )

    def test_a_view_only_id_still_survives_the_restriction(self) -> None:
        """The union is view plus RESTRICTED lake, so the view is untouched."""
        with mock.patch.object(news_source, "READ_NEWS_LAKE", True):
            got = catalyst_resolver._load_news_window(
                self.stores.view, _ASOF, lookback_days=30, needed_ids=frozenset({"mine"})
            )
        self.assertEqual(sorted(got["id"]), ["in_view", "mine"])


if __name__ == "__main__":
    unittest.main()
