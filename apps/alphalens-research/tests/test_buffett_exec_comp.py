"""Tests for the DEF 14A exec-comp reader (#507 PR-7b).

`exec_comp_as_of` reads SEC pay-versus-performance (ecd) data from the XBRL
*frames* API (the tags are absent from companyfacts but present in frames),
applies an EXACT-accn point-in-time filter (frame rows carry no `filed` field —
the accession is resolved to its acceptance date via the issuer submissions JSON),
and reports a coverage enum so missing data is never a fabricated zero. All
numbers come from XBRL; the ratio is computed in Python. Hermetic — a fake client
returns canned frames + submissions, no network.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from alphalens_pipeline.buffett.exec_comp import (
    ExecCompCoverage,
    ExecCompFacts,
    exec_comp_as_of,
)

_CIK = "0001321655"

# Submissions JSON: one DEF 14A accepted 2026-04-24, calendar fiscal-year-end.
_SUBS = {
    "fiscalYearEnd": "1231",
    "filings": {
        "recent": {
            "accessionNumber": ["0001321655-26-000019", "0001321655-25-000010"],
            "acceptanceDateTime": ["2026-04-24T10:01:15.000Z", "2025-04-20T09:00:00.000Z"],
            "filingDate": ["2026-04-24", "2025-04-20"],
            "form": ["DEF 14A", "DEF 14A"],
        },
        "files": [],
    },
}


def _frame(rows: list[dict]) -> dict:
    return {"data": rows}


class _FakeClient:
    """Minimal stand-in for SecEdgarClient: canned submissions + frames."""

    def __init__(self, frames: dict[tuple[str, int], list[dict]], subs: dict | None = None):
        # frames keyed by (concept, year) -> list of rows
        self._frames = frames
        self._subs = subs if subs is not None else _SUBS
        self.frame_calls: list[tuple[str, int]] = []

    def fetch_submissions(self, cik: str) -> dict:
        return self._subs

    def fetch_submissions_overflow(self, name: str) -> dict:
        return {"filings": {"recent": {}}}

    def fetch_xbrl_frame(self, taxonomy: str, concept: str, unit: str, period: str) -> dict:
        year = int(period.replace("CY", ""))
        self.frame_calls.append((concept, year))
        return _frame(self._frames.get((concept, year), []))


def _full_year_frames(
    year: int,
    cik: str = _CIK,
    *,
    peo_total=20_000_000.0,
    peo_cap=18_000_000.0,
    neo_total=4_000_000.0,
    neo_cap=3_500_000.0,
    accn="0001321655-26-000019",
):
    c = int(cik)
    return {
        ("PeoTotalCompAmt", year): [{"cik": c, "val": peo_total, "accn": accn}],
        ("PeoActuallyPaidCompAmt", year): [{"cik": c, "val": peo_cap, "accn": accn}],
        ("NonPeoNeoAvgTotalCompAmt", year): [{"cik": c, "val": neo_total, "accn": accn}],
        ("NonPeoNeoAvgCompActuallyPaidAmt", year): [{"cik": c, "val": neo_cap, "accn": accn}],
    }


class TestExecCompAsOf(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, client, asof) -> ExecCompFacts:
        return exec_comp_as_of(_CIK, asof, client=client, frame_cache_dir=self.cache)

    def test_present_with_values_and_ratio(self):
        client = _FakeClient(_full_year_frames(2025))
        facts = self._run(client, dt.date(2026, 6, 1))
        self.assertEqual(facts.coverage, ExecCompCoverage.PRESENT)
        self.assertEqual(facts.fiscal_year, 2025)
        self.assertEqual(facts.peo_total_comp, 20_000_000.0)
        self.assertEqual(facts.neo_avg_total_comp, 4_000_000.0)
        # ratio computed in Python: 20M / 4M = 5.0
        self.assertEqual(facts.peo_to_neo_ratio, 5.0)
        self.assertEqual(facts.accn, "0001321655-26-000019")

    def test_pit_excludes_filing_accepted_after_asof(self):
        # The 2025 frame's accn was accepted 2026-04-24; asof just before → excluded.
        client = _FakeClient(_full_year_frames(2025))
        facts = self._run(client, dt.date(2026, 4, 1))
        self.assertEqual(facts.coverage, ExecCompCoverage.NOT_DISCLOSED)
        self.assertIsNone(facts.peo_total_comp)

    def test_pit_includes_filing_accepted_on_or_before_asof(self):
        client = _FakeClient(_full_year_frames(2025))
        facts = self._run(client, dt.date(2026, 4, 24))  # exactly acceptance date
        self.assertEqual(facts.coverage, ExecCompCoverage.PRESENT)

    def test_pre_2023_short_circuits_without_fetch(self):
        client = _FakeClient({})
        # First PvP data year is CY2022; asof in 2022 → window CY2020..CY2021, all pre-rule.
        facts = self._run(client, dt.date(2022, 6, 1))
        self.assertEqual(facts.coverage, ExecCompCoverage.PRE_2023_NOT_REQUIRED)
        self.assertEqual(client.frame_calls, [])

    def test_not_disclosed_when_cik_absent(self):
        client = _FakeClient(_full_year_frames(2025, cik="0000999999"))  # different filer
        facts = self._run(client, dt.date(2026, 6, 1))
        self.assertEqual(facts.coverage, ExecCompCoverage.NOT_DISCLOSED)
        self.assertIsNone(facts.peo_total_comp)

    def test_non_calendar_fy_not_fetched(self):
        subs = {**_SUBS, "fiscalYearEnd": "0926"}  # Apple-style late-Sept FYE
        client = _FakeClient(_full_year_frames(2025), subs=subs)
        facts = self._run(client, dt.date(2026, 6, 1))
        self.assertEqual(facts.coverage, ExecCompCoverage.UNKNOWN_NON_CALENDAR_FY)
        self.assertEqual(client.frame_calls, [])  # guard runs before any frame read

    def test_non_calendar_not_mislabeled_even_with_frame_hit(self):
        # A Sept-FY filer that DOES appear in a CY frame stays UNKNOWN (guard wins).
        subs = {**_SUBS, "fiscalYearEnd": "0630"}
        client = _FakeClient(_full_year_frames(2025), subs=subs)
        facts = self._run(client, dt.date(2026, 6, 1))
        self.assertEqual(facts.coverage, ExecCompCoverage.UNKNOWN_NON_CALENDAR_FY)
        self.assertIsNone(facts.peo_to_neo_ratio)

    def test_ratio_none_when_neo_zero(self):
        client = _FakeClient(_full_year_frames(2025, neo_total=0.0))
        facts = self._run(client, dt.date(2026, 6, 1))
        self.assertEqual(facts.coverage, ExecCompCoverage.PRESENT)
        self.assertIsNone(facts.peo_to_neo_ratio)

    def test_multi_peo_row_present_but_ratio_none(self):
        # Two PEO total rows (mid-year CEO change) → don't silently pick one.
        frames = _full_year_frames(2025)
        frames[("PeoTotalCompAmt", 2025)] = [
            {"cik": int(_CIK), "val": 10_000_000.0, "accn": "0001321655-26-000019"},
            {"cik": int(_CIK), "val": 12_000_000.0, "accn": "0001321655-26-000019"},
        ]
        client = _FakeClient(frames)
        facts = self._run(client, dt.date(2026, 6, 1))
        self.assertEqual(facts.coverage, ExecCompCoverage.PRESENT)
        self.assertIsNone(facts.peo_to_neo_ratio)

    def test_zero_value_preserved_distinct_from_missing(self):
        # A genuine 0 val is kept as 0.0; a missing concept is None — never conflated.
        frames = _full_year_frames(2025, peo_cap=0.0)
        client = _FakeClient(frames)
        facts = self._run(client, dt.date(2026, 6, 1))
        self.assertEqual(facts.peo_actually_paid, 0.0)

    def test_newest_eligible_year_wins(self):
        frames = {
            **_full_year_frames(2024, peo_total=9_000_000.0),
            **_full_year_frames(2025, peo_total=20_000_000.0),
        }
        # both years' accn accepted on/before asof
        subs = {
            "fiscalYearEnd": "1231",
            "filings": {
                "recent": {
                    "accessionNumber": ["0001321655-26-000019"],
                    "acceptanceDateTime": ["2026-04-24T10:01:15.000Z"],
                    "filingDate": ["2026-04-24"],
                    "form": ["DEF 14A"],
                },
                "files": [],
            },
        }
        client = _FakeClient(frames, subs=subs)
        facts = self._run(client, dt.date(2026, 6, 1))
        self.assertEqual(facts.fiscal_year, 2025)
        self.assertEqual(facts.peo_total_comp, 20_000_000.0)

    def test_frame_disk_cached_once(self):
        client = _FakeClient(_full_year_frames(2025))
        self._run(client, dt.date(2026, 6, 1))
        n_first = len(client.frame_calls)
        # second call (fresh client to prove the DISK cache, not in-memory) → 0 fetches
        client2 = _FakeClient(_full_year_frames(2025))
        exec_comp_as_of(_CIK, dt.date(2026, 6, 1), client=client2, frame_cache_dir=self.cache)
        self.assertGreater(n_first, 0)
        self.assertEqual(client2.frame_calls, [])  # served entirely from disk


if __name__ == "__main__":
    unittest.main()
