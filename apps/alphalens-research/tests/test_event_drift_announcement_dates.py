"""Tests for earnings-announcement date provider.

Pre-reg ``event_drift_v3_pead_quality_clean`` extracts announcement dates
from EDGAR companyfacts EPS entries (10-Q/10-K filed dates). 8-K Item 2.02
exact-timestamp ingestion is a future enhancement; v1 conservatively
treats every announcement as after-hours (``hour_et=None``) so the T0
timing rule defaults to ``filed_date + 1 trading day``.

PIT contract: each announcement carries first-filed semantics. An EPS
restatement (10-Q/A) does NOT generate a new announcement event — the
PEAD trade is anchored to the original surprise. Mirrors
``FosterSUEStore`` first-filed-per-period-end logic.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
from alphalens_pipeline.data.fundamentals.companyfacts_parquet import (
    CompanyfactsParquetReader,
    companyfacts_json_to_parquet_table,
)


def _write_parquet_for_cf(cf: dict, parquet_dir: Path, cik_padded: str) -> Path:
    parquet_dir.mkdir(parents=True, exist_ok=True)
    table = companyfacts_json_to_parquet_table(cf)
    out = parquet_dir / f"{cik_padded}.parquet"
    pq.write_table(table, out)
    return out


def _make_companyfacts(eps_records: list[dict]) -> dict:
    return {
        "cik": 320193,
        "entityName": "TESTCO",
        "facts": {
            "us-gaap": {
                "EarningsPerShareBasic": {
                    "label": "Earnings Per Share, Basic",
                    "description": "...",
                    "units": {"USD/shares": eps_records},
                }
            }
        },
    }


def _eps(end: str, filed: str, val: float, fp: str = "Q1", form: str = "10-Q") -> dict:
    end_d = date.fromisoformat(end)
    if end_d.month >= 3:
        start_d = end_d.replace(day=1, month=end_d.month - 2)
    else:
        start_d = end_d.replace(year=end_d.year - 1, day=1, month=end_d.month + 10)
    return {
        "end": end,
        "filed": filed,
        "val": val,
        "fp": fp,
        "start": start_d.isoformat(),
        "form": form,
    }


class _StubTickerCikMap:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def lookup(self, ticker: str) -> str | None:
        return self._mapping.get(ticker.upper())


def _make_provider(eps_records: list[dict]):
    from alphalens_research.screeners.event_drift.announcement_dates import (
        AnnouncementDateProvider,
    )

    cf = _make_companyfacts(eps_records)
    tmp = tempfile.mkdtemp()
    cf_dir = Path(tmp)
    _write_parquet_for_cf(cf, cf_dir, "0000320193")
    cik_map = _StubTickerCikMap({"AAPL": "0000320193"})
    return AnnouncementDateProvider(CompanyfactsParquetReader(cf_dir), cik_map)


class TestAnnouncementBasicEmission(unittest.TestCase):
    def test_single_quarter_emits_one_announcement(self):
        provider = _make_provider([_eps("2024-03-31", "2024-05-01", 1.50, "Q1")])
        events = provider.announcements("AAPL")
        self.assertEqual(len(events), 1)
        ann = events[0]
        self.assertEqual(ann.ticker, "AAPL")
        self.assertEqual(ann.period_end, date(2024, 3, 31))
        self.assertEqual(ann.filed_date, date(2024, 5, 1))
        self.assertIsNone(ann.accepted_hour_et)  # conservative default
        self.assertEqual(ann.source, "10-Q")

    def test_multiple_quarters_emits_in_chronological_order(self):
        provider = _make_provider(
            [
                _eps("2024-03-31", "2024-05-01", 1.50, "Q1"),
                _eps("2024-06-30", "2024-08-01", 1.55, "Q2"),
                _eps("2023-12-31", "2024-02-15", 1.40, "Q4", form="10-K"),
            ]
        )
        events = provider.announcements("AAPL")
        self.assertEqual(len(events), 3)
        # Sorted by period_end ascending (so consumer can pick chronologically)
        self.assertEqual(
            [e.period_end for e in events],
            [date(2023, 12, 31), date(2024, 3, 31), date(2024, 6, 30)],
        )

    def test_10k_form_records_source_label(self):
        provider = _make_provider([_eps("2023-12-31", "2024-02-15", 1.40, "FY", form="10-K")])
        events = provider.announcements("AAPL")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source, "10-K")


class TestAnnouncementFirstFiledPIT(unittest.TestCase):
    def test_amendment_does_not_create_new_announcement(self):
        # Original Q1 filed 2024-05-01 with EPS=1.50.
        # Amendment 10-Q/A filed 2024-08-15 with EPS=1.45.
        # First-filed semantics: only the ORIGINAL announcement (2024-05-01)
        # should appear, NOT the amendment.
        provider = _make_provider(
            [
                _eps("2024-03-31", "2024-05-01", 1.50, "Q1"),
                _eps("2024-03-31", "2024-08-15", 1.45, "Q1", form="10-Q/A"),
            ]
        )
        events = provider.announcements("AAPL")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].filed_date, date(2024, 5, 1))
        self.assertEqual(events[0].source, "10-Q")  # original form, not /A

    def test_announcements_after_filter_drops_old_periods(self):
        provider = _make_provider(
            [
                _eps("2024-03-31", "2024-05-01", 1.50, "Q1"),
                _eps("2024-06-30", "2024-08-01", 1.55, "Q2"),
                _eps("2024-09-30", "2024-11-01", 1.60, "Q3"),
            ]
        )
        events = provider.announcements("AAPL", after=date(2024, 6, 1))
        self.assertEqual(len(events), 2)
        self.assertEqual([e.period_end for e in events], [date(2024, 6, 30), date(2024, 9, 30)])

    def test_announcements_before_filter_drops_future_periods(self):
        provider = _make_provider(
            [
                _eps("2024-03-31", "2024-05-01", 1.50, "Q1"),
                _eps("2024-06-30", "2024-08-01", 1.55, "Q2"),
                _eps("2024-09-30", "2024-11-01", 1.60, "Q3"),
            ]
        )
        events = provider.announcements("AAPL", before=date(2024, 8, 15))
        # filed_date < before. Q3 filed 2024-11-01 should be dropped.
        self.assertEqual([e.period_end for e in events], [date(2024, 3, 31), date(2024, 6, 30)])

    def test_after_and_before_combine(self):
        provider = _make_provider(
            [
                _eps("2024-03-31", "2024-05-01", 1.50, "Q1"),
                _eps("2024-06-30", "2024-08-01", 1.55, "Q2"),
                _eps("2024-09-30", "2024-11-01", 1.60, "Q3"),
                _eps("2024-12-31", "2025-02-15", 1.70, "Q4", form="10-K"),
            ]
        )
        events = provider.announcements("AAPL", after=date(2024, 7, 1), before=date(2024, 11, 30))
        # after=2024-07-01 drops Q1 (filed 2024-05-01) and Q2 (filed 2024-08-01
        # is AFTER 2024-07-01 -> kept). Wait — Q2 filed_date is 2024-08-01 > 2024-07-01,
        # so Q2 IS kept. Q3 filed 2024-11-01 < 2024-11-30, also kept. Q4 filed
        # 2025-02-15 > 2024-11-30, dropped.
        self.assertEqual([e.period_end for e in events], [date(2024, 6, 30), date(2024, 9, 30)])


class TestAnnouncementErrorHandling(unittest.TestCase):
    def test_unmapped_ticker_returns_empty(self):
        from alphalens_research.screeners.event_drift.announcement_dates import (
            AnnouncementDateProvider,
        )

        tmp = tempfile.mkdtemp()
        cik_map = _StubTickerCikMap({})
        provider = AnnouncementDateProvider(CompanyfactsParquetReader(Path(tmp)), cik_map)
        self.assertEqual(provider.announcements("UNKNOWN"), [])

    def test_missing_companyfacts_file_returns_empty(self):
        from alphalens_research.screeners.event_drift.announcement_dates import (
            AnnouncementDateProvider,
        )

        tmp = tempfile.mkdtemp()
        cik_map = _StubTickerCikMap({"GHOST": "9999999999"})
        provider = AnnouncementDateProvider(CompanyfactsParquetReader(Path(tmp)), cik_map)
        self.assertEqual(provider.announcements("GHOST"), [])

    def test_no_eps_block_returns_empty(self):
        from alphalens_research.screeners.event_drift.announcement_dates import (
            AnnouncementDateProvider,
        )

        tmp = tempfile.mkdtemp()
        cf = {"cik": 99, "entityName": "X", "facts": {"us-gaap": {}}}
        cf_dir = Path(tmp)
        _write_parquet_for_cf(cf, cf_dir, "0000000099")
        cik_map = _StubTickerCikMap({"X": "0000000099"})
        provider = AnnouncementDateProvider(CompanyfactsParquetReader(cf_dir), cik_map)
        self.assertEqual(provider.announcements("X"), [])

    def test_skips_non_quarterly_entries(self):
        # FY (10-K) annual entry IS a quarterly event (Q4 announcement) — kept.
        # But entries with no fp tag and no quarterly span -> skipped.
        provider = _make_provider(
            [
                _eps("2024-03-31", "2024-05-01", 1.50, "Q1"),
                # malformed: no fp, span >100 days
                {
                    "end": "2024-12-31",
                    "start": "2024-01-01",
                    "filed": "2025-02-15",
                    "val": 5.50,
                    "form": "10-K",
                },
            ]
        )
        events = provider.announcements("AAPL")
        # The malformed entry has fp absent, span 365d -> not quarterly -> dropped.
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].period_end, date(2024, 3, 31))


class TestAnnouncementDataclass(unittest.TestCase):
    def test_announcement_is_hashable_and_immutable(self):
        from alphalens_research.screeners.event_drift.announcement_dates import (
            EarningsAnnouncement,
        )

        ann = EarningsAnnouncement(
            ticker="AAPL",
            period_end=date(2024, 3, 31),
            filed_date=date(2024, 5, 1),
            accepted_hour_et=None,
            source="10-Q",
        )
        # Frozen dataclass: hashable, raises on mutation
        self.assertEqual(hash(ann), hash(ann))
        with self.assertRaises((AttributeError, Exception)):
            ann.ticker = "MSFT"  # type: ignore[misc]


class TestAnnouncementProviderOnRepresentativeFixtures(unittest.TestCase):
    """Round-trip regression on the synthetic Apple / IPO fixtures."""

    def setUp(self):
        from alphalens_research.screeners.event_drift.announcement_dates import (
            AnnouncementDateProvider,
        )

        from tests.fixtures.companyfacts_fixtures import (
            APPLE_CIK,
            IPO_CIK,
            write_all_fixtures_as_parquet,
        )

        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.parquet_dir = tmp / "companyfacts_parquet"
        write_all_fixtures_as_parquet(self.parquet_dir)
        cik_map = _StubTickerCikMap({"AAPL": APPLE_CIK, "IPO_CO": IPO_CIK})
        self.provider = AnnouncementDateProvider(
            CompanyfactsParquetReader(self.parquet_dir), cik_map
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_apple_fixture_emits_eight_first_filed_announcements(self):
        # 8 quarters + 1 restatement -> first-filed semantics collapse to 8.
        events = self.provider.announcements("AAPL")
        self.assertEqual(len(events), 8)
        # All have ticker uppercase + accepted_hour_et None.
        self.assertTrue(all(e.ticker == "AAPL" for e in events))
        self.assertTrue(all(e.accepted_hour_et is None for e in events))
        # First-filed for the restated quarter (2023-04-01) must be the
        # ORIGINAL filing (2023-05-15), not the late amendment (2024-01-15).
        restated = next(e for e in events if e.period_end == date(2023, 4, 1))
        self.assertEqual(restated.filed_date, date(2023, 5, 15))

    def test_apple_fixture_after_filter_drops_pre_2023(self):
        events = self.provider.announcements("AAPL", after=date(2023, 1, 1))
        self.assertTrue(all(e.filed_date >= date(2023, 1, 1) for e in events))

    def test_ipo_fixture_emits_two_announcements(self):
        events = self.provider.announcements("IPO_CO")
        self.assertEqual(len(events), 2)


if __name__ == "__main__":
    unittest.main()
