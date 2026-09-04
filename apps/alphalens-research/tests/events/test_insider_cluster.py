"""Unit tests for the pipeline-tier insider-cluster helpers (event lane, epic #1293).

The pure functions were promoted from ``alphalens_research.diagnostics.insider_cluster_retro``
so the VPS pipeline image (which carries no research code) can run the live
detection. The frozen forward spec is
``docs/research/preregistration/params_insider_cluster_forward_2026_09.json``.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from alphalens_pipeline.events import insider_cluster as ic
from alphalens_pipeline.paper.calendar import session_on_or_after

D = dt.date
REPO_ROOT = Path(__file__).resolve().parents[4]


def _leg(
    ticker,
    cik,
    filed,
    usd=60_000.0,
    tx=None,
    code="P",
    officer=True,
    amend=False,
    accession=None,
    name=None,
    director=False,
):
    return {
        "issuer_cik": "0000000099",
        "ticker": ticker,
        "reporting_owner_cik": cik,
        "reporting_owner_name": name or f"Insider {cik}",
        "filed_date": filed,
        "transaction_date": tx or filed,
        "transaction_code": code,
        "acquired_disposed": "A",
        "is_amendment": amend,
        "is_officer": officer,
        "is_director": director,
        "is_ten_percent_owner": False,
        "transaction_shares": usd / 10.0,
        "transaction_price_per_share": 10.0,
        "accession_number": accession or f"{cik}-{filed.isoformat()}",
    }


class TestQualifyingLegs(unittest.TestCase):
    def test_keeps_open_market_officer_purchases_above_floor(self):
        df = pd.DataFrame(
            [
                _leg("AAA", "1", D(2020, 3, 2)),
                _leg("AAA", "2", D(2020, 3, 2), code="S"),  # sale
                _leg("AAA", "3", D(2020, 3, 2), officer=False),  # 10% owner only
                _leg("AAA", "4", D(2020, 3, 2), amend=True),  # amendment
                _leg("AAA", "5", D(2020, 3, 2), usd=5_000.0),  # below 10k leg floor
            ]
        )
        df.loc[2, "is_ten_percent_owner"] = True
        out = ic.qualifying_legs(df, leg_min_usd=10_000.0)
        self.assertEqual(list(out.reporting_owner_cik), ["1"])
        self.assertAlmostEqual(float(out.usd.iloc[0]), 60_000.0)

    def test_drops_legs_without_price(self):
        df = pd.DataFrame([_leg("AAA", "1", D(2020, 3, 2))])
        df.loc[0, "transaction_price_per_share"] = np.nan
        self.assertTrue(ic.qualifying_legs(df, leg_min_usd=10_000.0).empty)


class TestDetectClusters(unittest.TestCase):
    def _legs(self, rows):
        return ic.qualifying_legs(pd.DataFrame(rows), leg_min_usd=10_000.0)

    def _detect(self, legs, **kw):
        base = {"window_sessions": 2, "min_insiders": 2, "min_usd": 100_000.0, "dedup_sessions": 20}
        base.update(kw)
        return ic.detect_clusters(legs, **base)

    def test_two_distinct_insiders_within_two_sessions_form_one_event(self):
        legs = self._legs(
            [
                _leg("AAA", "1", D(2020, 3, 2)),  # Monday
                _leg("AAA", "2", D(2020, 3, 4)),  # Wednesday = +2 sessions
            ]
        )
        ev = self._detect(legs)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev.iloc[0].event_date, D(2020, 3, 4))  # completion = 2nd insider's filing
        self.assertEqual(ev.iloc[0].first_leg_date, D(2020, 3, 2))
        self.assertEqual(ev.iloc[0].n_insiders, 2)
        self.assertAlmostEqual(ev.iloc[0].cluster_usd, 120_000.0)
        self.assertEqual(ev.iloc[0].completing_accession, "2-2020-03-04")
        self.assertEqual(ev.iloc[0].completing_transaction_date, D(2020, 3, 4))

    def test_same_insider_twice_is_not_a_cluster(self):
        legs = self._legs([_leg("AAA", "1", D(2020, 3, 2)), _leg("AAA", "1", D(2020, 3, 3))])
        self.assertTrue(self._detect(legs).empty)

    def test_legs_three_sessions_apart_do_not_cluster(self):
        legs = self._legs([_leg("AAA", "1", D(2020, 3, 2)), _leg("AAA", "2", D(2020, 3, 5))])
        self.assertTrue(self._detect(legs).empty)  # Mon, Thu = +3

    def test_usd_floor_applies_to_the_cluster_sum(self):
        legs = self._legs(
            [
                _leg("AAA", "1", D(2020, 3, 2), usd=40_000.0),
                _leg("AAA", "2", D(2020, 3, 3), usd=40_000.0),
            ]
        )
        self.assertTrue(self._detect(legs).empty)

    def test_three_in_five_definition_detects_three_distinct_insiders(self):
        legs = self._legs(
            [
                _leg("AAA", "1", D(2020, 3, 2)),
                _leg("AAA", "2", D(2020, 3, 5)),  # +3 sessions from leg 1
                _leg("AAA", "3", D(2020, 3, 9)),  # +5 sessions from leg 1, +2 from leg 2
            ]
        )
        two_in_two = self._detect(legs)
        three_in_five = self._detect(legs, window_sessions=5, min_insiders=3)
        self.assertEqual(len(two_in_two), 1)
        self.assertEqual(two_in_two.iloc[0].n_insiders, 2)
        self.assertEqual(len(three_in_five), 1)
        self.assertEqual(three_in_five.iloc[0].event_date, D(2020, 3, 9))
        self.assertEqual(three_in_five.iloc[0].n_insiders, 3)

    def test_dedup_keeps_first_event_per_ticker_per_window(self):
        legs = self._legs(
            [
                _leg("AAA", "1", D(2020, 3, 2)),
                _leg("AAA", "2", D(2020, 3, 3)),
                _leg("AAA", "3", D(2020, 3, 10)),
                _leg("AAA", "4", D(2020, 3, 11)),  # inside 20 sessions -> dropped
                _leg("AAA", "5", D(2020, 6, 1)),
                _leg("AAA", "6", D(2020, 6, 2)),  # far -> new event
            ]
        )
        ev = self._detect(legs)
        self.assertEqual(list(ev.event_date), [D(2020, 3, 3), D(2020, 6, 2)])

    def test_empty_legs_frame_carries_cluster_columns(self):
        empty = self._legs([_leg("AAA", "1", D(2020, 3, 2), usd=1_000.0)])
        self.assertTrue(empty.empty)
        ev = self._detect(empty)
        self.assertTrue(ev.empty)
        self.assertEqual(tuple(ev.columns), ic.CLUSTER_COLUMNS)
        self.assertIn("completing_transaction_date", ev.columns)


class TestArrivalSession(unittest.TestCase):
    def test_pre_open_acceptance_maps_to_same_session(self):
        acc = dt.datetime(2020, 3, 4, 8, 15)  # ET, before 09:00
        self.assertEqual(ic.arrival_session(D(2020, 3, 4), acc), D(2020, 3, 4))

    def test_intraday_or_post_close_acceptance_maps_to_next_session(self):
        self.assertEqual(
            ic.arrival_session(D(2020, 3, 4), dt.datetime(2020, 3, 4, 10, 0)), D(2020, 3, 5)
        )
        self.assertEqual(
            ic.arrival_session(D(2020, 3, 4), dt.datetime(2020, 3, 4, 17, 30)), D(2020, 3, 5)
        )

    def test_unknown_acceptance_is_conservative_next_session(self):
        self.assertEqual(ic.arrival_session(D(2020, 3, 6), None), D(2020, 3, 9))  # Fri -> Mon

    def test_filing_on_a_non_session_weekday_arrives_at_the_next_session(self):
        # Good Friday 2026-04-03: EDGAR is open, XNYS is closed. The filing is public
        # before Monday's open whatever its acceptance time, so Monday IS obtainable
        # (the retro helper skipped to Tuesday for after-hours acceptance).
        self.assertEqual(
            ic.arrival_session(D(2026, 4, 3), dt.datetime(2026, 4, 3, 15, 0)), D(2026, 4, 6)
        )
        self.assertEqual(
            ic.arrival_session(D(2026, 4, 3), dt.datetime(2026, 4, 3, 8, 0)), D(2026, 4, 6)
        )


class TestEventBriefDate(unittest.TestCase):
    """Brief date D such that session_on_or_after(D) == arrival_session(F, A)."""

    def test_pre_open_acceptance_maps_to_filing_date(self):
        self.assertEqual(
            ic.event_brief_date(D(2020, 3, 4), dt.datetime(2020, 3, 4, 8, 15)), D(2020, 3, 4)
        )

    def test_post_open_acceptance_maps_to_next_calendar_day(self):
        self.assertEqual(
            ic.event_brief_date(D(2020, 3, 4), dt.datetime(2020, 3, 4, 10, 0)), D(2020, 3, 5)
        )

    def test_unknown_acceptance_maps_to_next_calendar_day(self):
        self.assertEqual(ic.event_brief_date(D(2020, 3, 4), None), D(2020, 3, 5))

    def test_friday_post_open_lands_on_saturday_brief_only(self):
        d = ic.event_brief_date(D(2020, 3, 6), dt.datetime(2020, 3, 6, 17, 0))
        self.assertEqual(d, D(2020, 3, 7))  # Saturday brief; Sunday/Monday never claim it
        self.assertEqual(session_on_or_after(d), D(2020, 3, 9))

    def test_brief_date_arrival_parity_with_arrival_session(self):
        cases = [
            (D(2026, 11, 23), dt.datetime(2026, 11, 23, 8, 0)),  # Monday pre-open
            (D(2026, 11, 23), dt.datetime(2026, 11, 23, 12, 0)),  # Monday intraday
            (D(2026, 11, 20), dt.datetime(2026, 11, 20, 18, 0)),  # Friday after close
            (D(2026, 11, 25), dt.datetime(2026, 11, 25, 17, 0)),  # Wed before Thanksgiving
            (D(2026, 4, 3), dt.datetime(2026, 4, 3, 15, 0)),  # Good Friday (exchange holiday)
            (D(2026, 4, 3), dt.datetime(2026, 4, 3, 8, 0)),  # Good Friday, pre-open
            (D(2026, 11, 24), None),  # unknown acceptance
        ]
        for filed, acc in cases:
            with self.subTest(filed=filed, acc=acc):
                d = ic.event_brief_date(filed, acc)
                self.assertEqual(session_on_or_after(d), ic.arrival_session(filed, acc))
        # spot-check the holiday: Wed 11-25 after close -> Fri 11-27 (Thu is Thanksgiving)
        self.assertEqual(
            ic.arrival_session(D(2026, 11, 25), dt.datetime(2026, 11, 25, 17, 0)),
            D(2026, 11, 27),
        )


class TestFilingLag(unittest.TestCase):
    def test_busday_count_skips_weekend(self):
        self.assertEqual(ic.filing_lag_bdays(D(2020, 3, 6), D(2020, 3, 9)), 1)  # Fri -> Mon
        self.assertEqual(ic.filing_lag_bdays(D(2020, 3, 2), D(2020, 3, 2)), 0)

    def test_late_threshold_is_ten(self):
        self.assertEqual(ic.LATE_FILING_BDAYS, 10)
        self.assertLessEqual(ic.filing_lag_bdays(D(2020, 3, 2), D(2020, 3, 16)), 10)
        self.assertGreater(ic.filing_lag_bdays(D(2020, 3, 2), D(2020, 3, 17)), 10)


class TestClusterBuyers(unittest.TestCase):
    def test_lists_each_distinct_insider_in_window_with_usd(self):
        legs = ic.qualifying_legs(
            pd.DataFrame(
                [
                    _leg("AAA", "2", D(2020, 3, 3), usd=70_000.0, name="Bob CFO"),
                    _leg("AAA", "1", D(2020, 3, 2), usd=30_000.0, name="Ann CEO"),
                    _leg("AAA", "1", D(2020, 3, 3), usd=30_000.0, name="Ann CEO"),
                    _leg("AAA", "3", D(2020, 3, 20), name="Late Larry"),  # outside the window
                    _leg("BBB", "9", D(2020, 3, 2), name="Other issuer"),
                ]
            ),
            leg_min_usd=10_000.0,
        )
        buyers = ic.cluster_buyers(
            legs, ticker="AAA", first_leg_date=D(2020, 3, 2), event_date=D(2020, 3, 3)
        )
        self.assertEqual([b["cik"] for b in buyers], ["1", "2"])
        self.assertEqual(buyers[0]["name"], "Ann CEO")
        self.assertAlmostEqual(buyers[0]["usd"], 60_000.0)
        self.assertEqual(buyers[0]["filed_date"], "2020-03-02")
        self.assertEqual(buyers[0]["role"], "officer")
        self.assertAlmostEqual(buyers[1]["usd"], 70_000.0)
        self.assertEqual(json.loads(json.dumps(buyers)), buyers)  # JSON-serialisable


class TestUrlsAndTitle(unittest.TestCase):
    def test_filing_index_url_shape(self):
        url = ic.filing_index_url("0000320193", "0000320193-20-000010")
        self.assertEqual(
            url,
            "https://www.sec.gov/Archives/edgar/data/320193/000032019320000010/"
            "0000320193-20-000010-index.htm",
        )

    def test_filing_index_url_empty_cik_returns_empty(self):
        self.assertEqual(ic.filing_index_url("", "0000320193-20-000010"), "")
        self.assertEqual(ic.filing_index_url("  ", "0000320193-20-000010"), "")

    def test_title_mentions_count_usd_and_dates(self):
        title = ic.cluster_title(
            n_insiders=2,
            cluster_usd=120_000.0,
            first_leg_date=D(2020, 3, 2),
            event_date=D(2020, 3, 4),
        )
        self.assertIn("2 officers/directors", title)
        self.assertIn("$120k", title)
        self.assertIn("2020-03-02", title)
        self.assertIn("2020-03-04", title)
        self.assertIn(
            "$1.2M",
            ic.cluster_title(
                n_insiders=3,
                cluster_usd=1_234_000.0,
                first_leg_date=D(2020, 3, 2),
                event_date=D(2020, 3, 2),
            ),
        )

    def test_acceptance_to_utc_iso_converts_et(self):
        self.assertEqual(
            ic.acceptance_to_utc_iso(dt.datetime(2020, 3, 4, 8, 15)), "2020-03-04T13:15:00Z"
        )
        self.assertEqual(
            ic.acceptance_to_utc_iso(dt.datetime(2020, 7, 1, 17, 0)), "2020-07-01T21:00:00Z"
        )
        self.assertIsNone(ic.acceptance_to_utc_iso(None))


class TestAcceptanceFetchFallback(unittest.TestCase):
    """The acceptance fetch tries the issuer CIK path, then the reporter CIK path, and caches the URL."""

    class FakeClient:
        def __init__(self):
            self.urls = []

        def get_text(self, url):
            self.urls.append(url)
            if "/data/1/" in url:
                raise RuntimeError("NoSuchKey")
            return "<SEC-HEADER>\n<ACCEPTANCE-DATETIME>20200304081500\nFILER:"

    def test_falls_back_to_reporter_cik_and_caches_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self.FakeClient()
            got = ic.fetch_acceptance(
                "0000000002-20-000001", "1", client, cache_dir=Path(tmp), fallback_ciks=["2"]
            )
            self.assertEqual(got, dt.datetime(2020, 3, 4, 8, 15))
            self.assertEqual(len(client.urls), 2)
            self.assertIn("/data/2/", client.urls[1])
            cached = json.loads((Path(tmp) / "0000000002-20-000001.json").read_text())
            self.assertEqual(cached["acceptance"], "20200304081500")
            self.assertIn("/data/2/", cached["url"])
            ic.fetch_acceptance(
                "0000000002-20-000001", "1", client, cache_dir=Path(tmp), fallback_ciks=["2"]
            )
            self.assertEqual(len(client.urls), 2)  # served from the cache

    def test_all_paths_missing_caches_the_error_and_returns_none(self):
        class Dead:
            def get_text(self, url):
                raise RuntimeError("NoSuchKey")

        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                ic.fetch_acceptance(
                    "0000000002-20-000002", "1", Dead(), cache_dir=Path(tmp), fallback_ciks=["2"]
                )
            )
            cached = json.loads((Path(tmp) / "0000000002-20-000002.json").read_text())
            self.assertIsNone(cached["acceptance"])
            self.assertIn("NoSuchKey", cached["error"])
            self.assertTrue(cached["fallback_tried"])

    def test_accession_urls_dedups_and_skips_empty(self):
        urls = ic.accession_urls("0000000002-20-000001", ["1", None, "1", "2"])
        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].endswith("/data/1/000000000220000001/0000000002-20-000001.txt"))


class TestConstantsParity(unittest.TestCase):
    """The frozen forward params and the module constants must agree (spec drift guard)."""

    PARAMS = REPO_ROOT / "docs/research/preregistration/params_insider_cluster_forward_2026_09.json"

    def test_event_mcap_range_matches_thematic_bracket(self):
        from alphalens_pipeline.thematic.mapping.orchestrator import DEFAULT_MCAP_RANGE

        self.assertEqual(ic.EVENT_MCAP_RANGE, tuple(DEFAULT_MCAP_RANGE))

    def test_constants_match_frozen_forward_params(self):
        constants = json.loads(self.PARAMS.read_text())["constants"]
        for key, frozen in constants.items():
            if key == "EVENT_CAR_VERSION":
                continue  # owned by the outcome pass (feedback/event_car), pinned there
            with self.subTest(key=key):
                value = getattr(ic, key)
                if key == "PRE_OPEN_CUTOFF_ET":
                    value = value.strftime("%H:%M")
                elif isinstance(value, (tuple, frozenset)):
                    value = sorted(value) if isinstance(value, frozenset) else list(value)
                self.assertEqual(value, frozen)
        self.assertEqual(ic.EVENT_GATE_VERSION, constants["EVENT_GATE_VERSION"])


if __name__ == "__main__":
    unittest.main()
