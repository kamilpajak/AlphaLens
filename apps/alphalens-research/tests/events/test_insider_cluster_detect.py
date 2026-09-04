"""Tests for the live insider-cluster detection (event lane, epic #1293).

Everything that would touch the network (EDGAR acceptance time, yfinance market
cap and earnings calendar, the SIC index) is injected, so the tests pin the
selection and exclusion RULES against a tiny Form-4 store on disk.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.events import insider_cluster as ic
from alphalens_pipeline.events import insider_cluster_detect as det

from tests.events.test_insider_cluster import _leg

D = dt.date
ASOF = D(2026, 3, 4)  # Wednesday
PRE_OPEN = dt.datetime(2026, 3, 4, 8, 15)
POST_OPEN_ASOF = dt.datetime(2026, 3, 4, 10, 0)
POST_CLOSE_PREV = dt.datetime(2026, 3, 3, 17, 30)


def _write_store(root: Path, rows_by_year: dict[int, list[dict]]) -> None:
    for year, rows in rows_by_year.items():
        part = root / f"transaction_year={year}"
        part.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(part / "compacted.parquet", index=False)


class _Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "form4_parquet"
        self.acceptance: dict[str, dt.datetime | None] = {}
        self.acceptance_calls: list[str] = []
        self.mcap: dict[str, float | None] = {}
        self.earnings: dict[str, dt.date | None] = {}
        self.sic: dict[str, int | None] = {}

    def tearDown(self):
        self._tmp.cleanup()

    def _acceptance_fn(self, accession, issuer_cik, fallback_ciks):
        self.acceptance_calls.append(accession)
        return self.acceptance.get(accession)

    def build(self, asof=ASOF):
        return det.build_event_candidates(
            asof=asof,
            form4_root=self.root,
            acceptance_fn=self._acceptance_fn,
            mcap_fn=lambda t: self.mcap.get(t, 2_000_000_000.0),
            earnings_fn=self.earnings.get,
            sic_fn=self.sic.get,
            company_names={"AAA": "Alpha Corp"},
        )

    def _cluster(
        self,
        ticker="AAA",
        first=D(2026, 3, 2),
        second=D(2026, 3, 4),
        acc: dt.datetime | None = PRE_OPEN,
    ):
        rows = [_leg(ticker, "1", first), _leg(ticker, "2", second)]
        self.acceptance[f"2-{second.isoformat()}"] = acc
        return rows


class TestLoadLegs(_Fixture):
    def test_reads_both_years_and_clips_to_the_asof_window(self):
        _write_store(
            self.root,
            {
                # December transactions filed in January sit in the EARLIER partition
                2025: [
                    _leg("OLD", "1", D(2026, 1, 5), tx=D(2025, 12, 30)),
                    _leg("OLD", "2", D(2026, 1, 6), tx=D(2025, 12, 31)),
                ],
                2026: [
                    _leg("AAA", "1", D(2026, 3, 2)),
                    _leg("AAA", "2", D(2026, 3, 4)),
                    _leg("AAA", "3", D(2026, 3, 5)),  # after asof: not in the store yet
                    _leg("STALE", "4", D(2025, 12, 1)),  # older than the lookback
                ],
            },
        )
        legs = det.load_legs(form4_root=self.root, asof=ASOF)
        self.assertEqual(sorted(set(legs.ticker)), ["AAA", "OLD"])
        self.assertEqual(legs.filed_date.max(), D(2026, 3, 4))
        self.assertEqual(len(legs[legs.ticker == "AAA"]), 2)

    def test_missing_store_yields_empty_frame(self):
        legs = det.load_legs(form4_root=self.root / "nope", asof=ASOF)
        self.assertTrue(legs.empty)


class TestSelectForBriefDate(_Fixture):
    def test_cluster_completing_pre_open_on_asof_is_selected(self):
        _write_store(self.root, {2026: self._cluster(acc=PRE_OPEN)})
        out = self.build()
        self.assertEqual(list(out.ticker), ["AAA"])
        self.assertEqual(out.iloc[0].event_arrival_session, ASOF)

    def test_cluster_completing_post_close_day_before_asof_is_selected(self):
        _write_store(self.root, {2026: self._cluster(second=D(2026, 3, 3), acc=POST_CLOSE_PREV)})
        out = self.build()
        self.assertEqual(list(out.ticker), ["AAA"])
        self.assertEqual(out.iloc[0].event_arrival_session, ASOF)

    def test_cluster_completing_post_open_on_asof_belongs_to_the_next_brief(self):
        _write_store(self.root, {2026: self._cluster(acc=POST_OPEN_ASOF)})
        self.assertTrue(self.build(asof=ASOF).empty)
        nxt = self.build(asof=D(2026, 3, 5))
        self.assertEqual(list(nxt.ticker), ["AAA"])
        self.assertEqual(nxt.iloc[0].event_arrival_session, D(2026, 3, 5))

    def test_unknown_acceptance_is_treated_as_after_hours(self):
        rows = self._cluster(second=D(2026, 3, 3), acc=None)
        _write_store(self.root, {2026: rows})
        out = self.build()
        self.assertEqual(list(out.ticker), ["AAA"])
        self.assertIsNone(out.iloc[0].event_acceptance_utc)

    def test_acceptance_fetched_only_for_candidate_clusters(self):
        rows = self._cluster()
        rows += [_leg("OLD", "7", D(2026, 2, 9)), _leg("OLD", "8", D(2026, 2, 10))]
        _write_store(self.root, {2026: rows})
        self.build()
        self.assertEqual(self.acceptance_calls, ["2-2026-03-04"])


class TestExclusions(_Fixture):
    def _one(self, **kw):
        _write_store(self.root, {2026: self._cluster(**kw)})
        out = self.build()
        self.assertEqual(len(out), 1)
        return out.iloc[0]

    def test_eligible_when_nothing_trips(self):
        row = self._one()
        self.assertTrue(row.eligible)
        self.assertEqual(row.exclusion_reason, "")

    def test_late_filing_excluded_with_reason(self):
        rows = [_leg("AAA", "1", D(2026, 3, 2)), _leg("AAA", "2", D(2026, 3, 4), tx=D(2026, 2, 2))]
        self.acceptance["2-2026-03-04"] = PRE_OPEN
        _write_store(self.root, {2026: rows})
        row = self.build().iloc[0]
        self.assertFalse(row.eligible)
        self.assertEqual(row.exclusion_reason, "late_filing")
        self.assertGreater(row.event_filing_lag_bdays, 10)

    def test_mcap_out_of_bracket_excluded_and_market_cap_stamped(self):
        self.mcap["AAA"] = 20_000_000_000.0
        row = self._one()
        self.assertEqual(row.exclusion_reason, "mcap_out_of_bracket")
        self.assertAlmostEqual(row.market_cap, 20_000_000_000.0)
        self.mcap["AAA"] = 100_000_000.0
        self.assertEqual(self._one().exclusion_reason, "mcap_out_of_bracket")

    def test_mcap_bounds_are_inclusive(self):
        self.mcap["AAA"] = float(ic.EVENT_MCAP_RANGE[0])
        self.assertTrue(self._one().eligible)
        self.mcap["AAA"] = float(ic.EVENT_MCAP_RANGE[1])
        self.assertTrue(self._one().eligible)

    def test_mcap_unknown_excluded_conservatively(self):
        self.mcap["AAA"] = None
        row = self._one()
        self.assertEqual(row.exclusion_reason, "mcap_unknown")
        self.assertTrue(pd.isna(row.market_cap))

    def test_sic_fund_or_spac_excluded(self):
        for sic in sorted(ic.EXCLUDED_SIC):
            self.sic["AAA"] = sic
            row = self._one()
            self.assertEqual(row.exclusion_reason, "sic_excluded")
            self.assertEqual(row.event_sic, sic)

    def test_sic_unknown_not_excluded(self):
        self.sic["AAA"] = None
        row = self._one()
        self.assertTrue(row.eligible)
        self.assertTrue(pd.isna(row.event_sic))

    def test_earnings_inside_first_ten_sessions_excluded(self):
        self.earnings["AAA"] = D(2026, 3, 17)  # arrival 03-04 + 9 sessions = 03-17
        row = self._one()
        self.assertEqual(row.exclusion_reason, "earnings_window")
        self.assertEqual(row.event_next_earnings_date, D(2026, 3, 17))

    def test_earnings_on_eleventh_session_not_excluded(self):
        self.earnings["AAA"] = D(2026, 3, 18)
        self.assertTrue(self._one().eligible)

    def test_earnings_unknown_not_excluded(self):
        self.earnings["AAA"] = None
        row = self._one()
        self.assertTrue(row.eligible)
        self.assertIsNone(row.event_next_earnings_date)

    def test_first_exclusion_reason_wins_in_declared_order(self):
        self.mcap["AAA"] = None
        self.sic["AAA"] = 6770
        self.earnings["AAA"] = D(2026, 3, 5)
        self.assertEqual(self._one().exclusion_reason, "mcap_unknown")
        self.mcap["AAA"] = 2_000_000_000.0
        self.assertEqual(self._one().exclusion_reason, "sic_excluded")
        self.assertEqual(det.EXCLUSION_ORDER[0], "late_filing")


class TestRowShape(_Fixture):
    def test_eligible_row_shape_and_dtypes(self):
        _write_store(self.root, {2026: self._cluster()})
        out = self.build()
        self.assertEqual(tuple(out.columns), det.EVENT_CANDIDATE_COLUMNS)
        row = out.iloc[0]
        self.assertEqual(row.theme, ic.SOURCE_INSIDER_CLUSTER)
        self.assertEqual(row.source, ic.SOURCE_INSIDER_CLUSTER)
        self.assertIs(bool(row.verified), True)
        self.assertEqual(out["verified"].dtype, bool)
        self.assertEqual(row.company_name, "Alpha Corp")
        self.assertEqual(list(row.gates_passed), [])
        self.assertEqual(row.gates_passed_str, "")
        self.assertEqual(row.n_gates_passed, 0)
        self.assertEqual(row.gate_verdict_json, "{}")
        self.assertEqual(list(row.theme_search_keywords), [])
        self.assertTrue(pd.isna(row.llm_confidence))
        self.assertTrue(
            row.source_event_url.startswith("https://www.sec.gov/Archives/edgar/data/99/")
        )
        self.assertTrue(row.source_event_url.endswith("-index.htm"))
        self.assertIn("Insider purchase cluster", row.source_event_title)
        self.assertEqual(row.rationale, row.source_event_title)
        self.assertEqual(row.source_event_published_at, "2026-03-04T13:15:00Z")
        self.assertEqual(row.event_acceptance_utc, "2026-03-04T13:15:00Z")
        self.assertEqual(row.event_n_insiders, 2)
        self.assertAlmostEqual(row.event_cluster_usd, 120_000.0)
        buyers = json.loads(row.event_buyers_json)
        self.assertEqual([b["cik"] for b in buyers], ["1", "2"])
        self.assertEqual(row.event_first_leg_date, D(2026, 3, 2))
        self.assertEqual(row.event_completing_accession, "2-2026-03-04")
        self.assertEqual(row.event_gate_version, ic.EVENT_GATE_VERSION)
        self.assertNotIn("selection_score", out.columns)  # scorer enrichment names are reserved

    def test_company_name_falls_back_to_ticker(self):
        _write_store(self.root, {2026: self._cluster(ticker="ZZZ")})
        self.acceptance["2-2026-03-04"] = PRE_OPEN
        out = self.build()
        self.assertEqual(out.iloc[0].company_name, "ZZZ")

    def test_empty_store_returns_typed_empty_frame(self):
        out = self.build()
        self.assertTrue(out.empty)
        self.assertEqual(tuple(out.columns), det.EVENT_CANDIDATE_COLUMNS)

    def test_output_is_deterministic_across_two_builds(self):
        rows = self._cluster() + self._cluster(ticker="BBB")
        _write_store(self.root, {2026: rows})
        a, b = self.build(), self.build()
        pd.testing.assert_frame_equal(a, b)
        self.assertEqual(list(a.ticker), ["AAA", "BBB"])

    def test_load_company_names_projects_sec_file(self):
        path = Path(self._tmp.name) / "company_tickers.json"
        path.write_text(
            json.dumps({"0": {"ticker": "aaa", "title": "Alpha Corp"}, "1": {"ticker": ""}})
        )
        self.assertEqual(det.load_company_names(path), {"AAA": "Alpha Corp"})
        self.assertEqual(det.load_company_names(path.with_name("missing.json")), {})


class TestWrite(unittest.TestCase):
    def test_write_event_candidates_uses_atomic_writer(self):
        df = pd.DataFrame(columns=det.EVENT_CANDIDATE_COLUMNS)
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(det, "write_parquet_atomic") as atomic,
        ):
            path = det.write_event_candidates(df, asof=ASOF, output_dir=Path(tmp))
            self.assertEqual(path, Path(tmp) / "2026-03-04.parquet")
            atomic.assert_called_once()
            self.assertEqual(atomic.call_args.args[1], path)
            self.assertFalse(atomic.call_args.kwargs["index"])


if __name__ == "__main__":
    unittest.main()
