"""Tests for Sloan (1996) total-accruals-to-avg-total-assets ratio.

Locked into v3 pre-reg (event_drift_v3_pead_quality_clean) per
docs/research/preregistration/ledger.json: scorer composes this module to
filter top-quintile-SUE candidates by below-median accrual ratio
(= highest earnings quality per Sloan 1996, market under-reacts more to
high-quality earnings beats).

PIT contract per project convention (mirrors FosterSUEStore semantics):
- For each historical quarter ``q``, the balance-sheet entries used in the
  ratio are FIRST-FILED per period_end (earliest ``filed`` date). At asof
  ``t``, only entries with ``filed <= t`` are visible. Restatements are
  never substituted in.

Sloan total accruals (quarterly extension, Hribar-Collins 2002):

    accruals_q = (delta_AssetsCurrent
                  - delta_CashAndCashEquivalentsAtCarryingValue)
                 - (delta_LiabilitiesCurrent
                    - delta_LongTermDebtCurrent
                    - delta_IncomeTaxesPayable)
                 - DepreciationAndAmortization_q

    ratio_q    = accruals_q / avg(Assets_q, Assets_{q-1})

The ratio is signed: positive ratio = earnings exceed cash flow = lower
quality; negative ratio = cash flow exceeds earnings = higher quality.
Below-median ratio (low / negative) is the "high quality" tail.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
from alphalens_research.data.fundamentals.companyfacts_parquet import (
    CompanyfactsParquetReader,
    companyfacts_json_to_parquet_table,
)


def _write_parquet_for_cf(cf: dict, parquet_dir: Path, cik_padded: str) -> Path:
    """Convert one companyfacts dict to a parquet file inside ``parquet_dir``.

    Returns the parquet path. Replaces the legacy JSON-on-disk fixture pattern
    used before the parquet-backed store refactor.
    """
    parquet_dir.mkdir(parents=True, exist_ok=True)
    table = companyfacts_json_to_parquet_table(cf)
    out = parquet_dir / f"{cik_padded}.parquet"
    pq.write_table(table, out)
    return out


# ---------------------------------------------------------------------------
# Test fixtures: build a minimal SEC companyfacts JSON containing the seven
# concept tags Sloan accruals consumes. Each helper builds one quarterly
# balance-sheet entry (instant for stocks, duration for the depreciation flow).


def _instant(end: str, filed: str, val: float, fp: str = "Q1", form: str = "10-Q") -> dict:
    """Instant (balance-sheet) record. No ``start`` per US-GAAP convention."""
    return {"end": end, "filed": filed, "val": val, "fp": fp, "form": form}


def _duration(end: str, filed: str, val: float, fp: str = "Q1", form: str = "10-Q") -> dict:
    """Duration (income-statement / cash-flow) record. ``start`` 90d before end."""
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


def _make_companyfacts(concepts: dict[str, list[dict]]) -> dict:
    """Build minimal SEC companyfacts JSON with the given concept entries.

    ``concepts`` keys are us-gaap concept names; values are lists of records
    in either USD (most balance-sheet items) or USD/shares (EPS).
    """
    facts: dict[str, dict] = {}
    for name, records in concepts.items():
        facts[name] = {
            "label": name,
            "description": "...",
            "units": {"USD": records},
        }
    return {
        "cik": 320193,
        "entityName": "TESTCO",
        "facts": {"us-gaap": facts},
    }


def _full_balance_sheet(
    end: str,
    filed: str,
    *,
    ca: float,
    cash: float,
    cl: float,
    std: float,
    taxpay: float | None,
    dep: float,
    ta: float,
    fp: str = "Q1",
    form: str = "10-Q",
) -> dict[str, list[dict]]:
    """Helper: produce dict-of-records for ALL seven Sloan concepts at one period."""
    out: dict[str, list[dict]] = {
        "AssetsCurrent": [_instant(end, filed, ca, fp, form)],
        "CashAndCashEquivalentsAtCarryingValue": [_instant(end, filed, cash, fp, form)],
        "LiabilitiesCurrent": [_instant(end, filed, cl, fp, form)],
        "LongTermDebtCurrent": [_instant(end, filed, std, fp, form)],
        "DepreciationAndAmortization": [_duration(end, filed, dep, fp, form)],
        "Assets": [_instant(end, filed, ta, fp, form)],
    }
    if taxpay is not None:
        out["IncomeTaxesPayable"] = [_instant(end, filed, taxpay, fp, form)]
    return out


def _merge_balance_sheets(*period_dicts: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Merge multiple period dicts: concatenate records under each concept key."""
    merged: dict[str, list[dict]] = {}
    for d in period_dicts:
        for name, records in d.items():
            merged.setdefault(name, []).extend(records)
    return merged


# ---------------------------------------------------------------------------
# TickerCikMap stub: SloanAccrualsStore uses lookup() to get the CIK string.


class _StubTickerCikMap:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def lookup(self, ticker: str) -> str | None:
        return self._mapping.get(ticker.upper())


# ---------------------------------------------------------------------------
# Tests


class TestSloanAccrualsBasic(unittest.TestCase):
    """Sloan formula on simple two-quarter sequence."""

    def _make_store_with_two_quarters(
        self, q_minus_1: dict[str, list[dict]], q: dict[str, list[dict]]
    ):
        from alphalens_research.screeners.event_drift.accruals import SloanAccrualsStore

        merged = _merge_balance_sheets(q_minus_1, q)
        cf = _make_companyfacts(merged)

        tmp = tempfile.mkdtemp()
        cf_dir = Path(tmp)
        _write_parquet_for_cf(cf, cf_dir, "0000320193")

        cik_map = _StubTickerCikMap({"AAPL": "0000320193"})
        return SloanAccrualsStore(CompanyfactsParquetReader(cf_dir), cik_map)

    def test_basic_positive_accruals_ratio(self):
        # Q-1 (2023-12-31): CA=100, Cash=20, CL=80, STD=10, TaxPay=5, Dep=2, TA=500
        # Q   (2024-03-31): CA=120, Cash=22, CL=82, STD=11, TaxPay=4, Dep=3, TA=520
        # delta_CA=+20, delta_Cash=+2 -> (delta_CA - delta_Cash) = 18
        # delta_CL=+2, delta_STD=+1, delta_TaxPay=-1 -> (delta_CL - delta_STD - delta_TaxPay) = 2-1-(-1) = 2
        # accruals = 18 - 2 - Dep_q (=3) = 13
        # avg_TA = (500 + 520) / 2 = 510
        # ratio = 13 / 510 = 0.02549...
        q_minus_1 = _full_balance_sheet(
            "2023-12-31",
            "2024-02-15",
            ca=100,
            cash=20,
            cl=80,
            std=10,
            taxpay=5,
            dep=2,
            ta=500,
            fp="Q4",
            form="10-K",
        )
        q = _full_balance_sheet(
            "2024-03-31",
            "2024-05-01",
            ca=120,
            cash=22,
            cl=82,
            std=11,
            taxpay=4,
            dep=3,
            ta=520,
            fp="Q1",
        )
        store = self._make_store_with_two_quarters(q_minus_1, q)
        ratio = store.accruals_ratio("AAPL", date(2024, 6, 1))
        self.assertIsNotNone(ratio)
        self.assertAlmostEqual(ratio, 13.0 / 510.0, places=6)

    def test_basic_negative_accruals_ratio(self):
        # Cash flow exceeds earnings -> negative accruals -> high quality.
        # Q-1: CA=100, Cash=20, CL=80, STD=10, TaxPay=5, Dep=10, TA=500
        # Q  : CA=95, Cash=30, CL=75, STD=12, TaxPay=8, Dep=12, TA=510
        # delta_CA=-5, delta_Cash=+10 -> (delta_CA - delta_Cash) = -15
        # delta_CL=-5, delta_STD=+2, delta_TaxPay=+3 -> (delta_CL - delta_STD - delta_TaxPay) = -5-2-3 = -10
        # accruals = -15 - (-10) - 12 = -15 + 10 - 12 = -17
        # avg_TA = (500 + 510)/2 = 505 -> ratio = -17/505 = -0.0337
        q_minus_1 = _full_balance_sheet(
            "2023-12-31",
            "2024-02-15",
            ca=100,
            cash=20,
            cl=80,
            std=10,
            taxpay=5,
            dep=10,
            ta=500,
            fp="Q4",
            form="10-K",
        )
        q = _full_balance_sheet(
            "2024-03-31",
            "2024-05-01",
            ca=95,
            cash=30,
            cl=75,
            std=12,
            taxpay=8,
            dep=12,
            ta=510,
            fp="Q1",
        )
        store = self._make_store_with_two_quarters(q_minus_1, q)
        ratio = store.accruals_ratio("AAPL", date(2024, 6, 1))
        self.assertIsNotNone(ratio)
        self.assertAlmostEqual(ratio, -17.0 / 505.0, places=6)
        self.assertLess(ratio, 0.0, "Negative accruals signal high quality.")


class TestSloanAccrualsMissingTags(unittest.TestCase):
    """Behavior when some required concept tags are absent from companyfacts."""

    def _make_store_with_concepts(self, concepts: dict[str, list[dict]]):
        from alphalens_research.screeners.event_drift.accruals import SloanAccrualsStore

        cf = _make_companyfacts(concepts)
        tmp = tempfile.mkdtemp()
        cf_dir = Path(tmp)
        _write_parquet_for_cf(cf, cf_dir, "0000320193")

        cik_map = _StubTickerCikMap({"AAPL": "0000320193"})
        return SloanAccrualsStore(CompanyfactsParquetReader(cf_dir), cik_map)

    def test_missing_taxpayables_uses_zero_fallback(self):
        # Hribar-Collins 2002 quarterly: IncomeTaxesPayable is sparsely tagged
        # in EDGAR. Treat absence as zero (no tax-payable adjustment) rather
        # than dropping the firm entirely.
        q_minus_1 = _full_balance_sheet(
            "2023-12-31",
            "2024-02-15",
            ca=100,
            cash=20,
            cl=80,
            std=10,
            taxpay=None,
            dep=2,
            ta=500,
            fp="Q4",
            form="10-K",
        )
        q = _full_balance_sheet(
            "2024-03-31",
            "2024-05-01",
            ca=120,
            cash=22,
            cl=82,
            std=11,
            taxpay=None,
            dep=3,
            ta=520,
            fp="Q1",
        )
        store = self._make_store_with_concepts(_merge_balance_sheets(q_minus_1, q))
        ratio = store.accruals_ratio("AAPL", date(2024, 6, 1))
        # delta_CA - delta_Cash = 20 - 2 = 18
        # delta_CL - delta_STD - delta_TaxPay = 2 - 1 - 0 = 1
        # accruals = 18 - 1 - 3 = 14; avg_TA = 510; ratio = 14/510
        self.assertIsNotNone(ratio)
        self.assertAlmostEqual(ratio, 14.0 / 510.0, places=6)

    def test_missing_required_assets_concept_returns_none(self):
        # Without total Assets we cannot scale. Return None.
        q_minus_1 = _full_balance_sheet(
            "2023-12-31",
            "2024-02-15",
            ca=100,
            cash=20,
            cl=80,
            std=10,
            taxpay=5,
            dep=2,
            ta=500,
            fp="Q4",
            form="10-K",
        )
        q = _full_balance_sheet(
            "2024-03-31",
            "2024-05-01",
            ca=120,
            cash=22,
            cl=82,
            std=11,
            taxpay=4,
            dep=3,
            ta=520,
            fp="Q1",
        )
        merged = _merge_balance_sheets(q_minus_1, q)
        merged.pop("Assets")
        store = self._make_store_with_concepts(merged)
        self.assertIsNone(store.accruals_ratio("AAPL", date(2024, 6, 1)))

    def test_missing_assets_current_returns_none(self):
        q_minus_1 = _full_balance_sheet(
            "2023-12-31",
            "2024-02-15",
            ca=100,
            cash=20,
            cl=80,
            std=10,
            taxpay=5,
            dep=2,
            ta=500,
            fp="Q4",
            form="10-K",
        )
        q = _full_balance_sheet(
            "2024-03-31",
            "2024-05-01",
            ca=120,
            cash=22,
            cl=82,
            std=11,
            taxpay=4,
            dep=3,
            ta=520,
            fp="Q1",
        )
        merged = _merge_balance_sheets(q_minus_1, q)
        merged.pop("AssetsCurrent")
        store = self._make_store_with_concepts(merged)
        self.assertIsNone(store.accruals_ratio("AAPL", date(2024, 6, 1)))

    def test_zero_avg_total_assets_returns_none(self):
        q_minus_1 = _full_balance_sheet(
            "2023-12-31",
            "2024-02-15",
            ca=0,
            cash=0,
            cl=0,
            std=0,
            taxpay=0,
            dep=0,
            ta=0,
            fp="Q4",
            form="10-K",
        )
        q = _full_balance_sheet(
            "2024-03-31",
            "2024-05-01",
            ca=0,
            cash=0,
            cl=0,
            std=0,
            taxpay=0,
            dep=0,
            ta=0,
            fp="Q1",
        )
        store = self._make_store_with_concepts(_merge_balance_sheets(q_minus_1, q))
        self.assertIsNone(store.accruals_ratio("AAPL", date(2024, 6, 1)))


class TestSloanAccrualsPIT(unittest.TestCase):
    """First-filed PIT semantics + asof visibility."""

    def _make_store_with_concepts(self, concepts: dict[str, list[dict]]):
        from alphalens_research.screeners.event_drift.accruals import SloanAccrualsStore

        cf = _make_companyfacts(concepts)
        tmp = tempfile.mkdtemp()
        cf_dir = Path(tmp)
        _write_parquet_for_cf(cf, cf_dir, "0000320193")
        cik_map = _StubTickerCikMap({"AAPL": "0000320193"})
        return SloanAccrualsStore(CompanyfactsParquetReader(cf_dir), cik_map)

    def test_amendments_invariance_picks_first_filed(self):
        # Q has 2 entries: original 10-Q filed 2024-05-01 with CA=120,
        # then amendment 10-Q/A filed 2024-08-15 with CA=125.
        # First-filed must pick original (CA=120).
        q_minus_1 = _full_balance_sheet(
            "2023-12-31",
            "2024-02-15",
            ca=100,
            cash=20,
            cl=80,
            std=10,
            taxpay=5,
            dep=2,
            ta=500,
            fp="Q4",
            form="10-K",
        )
        q_original = _full_balance_sheet(
            "2024-03-31",
            "2024-05-01",
            ca=120,
            cash=22,
            cl=82,
            std=11,
            taxpay=4,
            dep=3,
            ta=520,
            fp="Q1",
        )
        # Amendment filed later with different CA
        q_amendment = _full_balance_sheet(
            "2024-03-31",
            "2024-08-15",
            ca=125,
            cash=22,
            cl=82,
            std=11,
            taxpay=4,
            dep=3,
            ta=520,
            fp="Q1",
            form="10-Q/A",
        )
        merged = _merge_balance_sheets(q_minus_1, q_original, q_amendment)
        store = self._make_store_with_concepts(merged)
        # asof after both filings -> should still see ORIGINAL CA=120
        ratio = store.accruals_ratio("AAPL", date(2024, 12, 1))
        # accruals = (120-100 - (22-20)) - (82-80 - (11-10) - (4-5)) - 3 = 18 - 2 - 3 = 13
        # avg_TA = 510 -> 13/510
        self.assertIsNotNone(ratio)
        self.assertAlmostEqual(ratio, 13.0 / 510.0, places=6)

    def test_pit_filter_hides_post_asof_filings(self):
        # asof BEFORE Q's filed date -> no Q visible -> ratio should be None
        q_minus_1 = _full_balance_sheet(
            "2023-12-31",
            "2024-02-15",
            ca=100,
            cash=20,
            cl=80,
            std=10,
            taxpay=5,
            dep=2,
            ta=500,
            fp="Q4",
            form="10-K",
        )
        q = _full_balance_sheet(
            "2024-03-31",
            "2024-05-01",
            ca=120,
            cash=22,
            cl=82,
            std=11,
            taxpay=4,
            dep=3,
            ta=520,
            fp="Q1",
        )
        store = self._make_store_with_concepts(_merge_balance_sheets(q_minus_1, q))
        # asof = 2024-04-15 (before Q filed 2024-05-01) -> only Q-1 visible
        # No Q visible -> need >=2 quarters for delta -> None
        self.assertIsNone(store.accruals_ratio("AAPL", date(2024, 4, 15)))

    def test_no_prior_quarter_returns_none(self):
        # Only one quarter filed. No prior quarter for delta computation -> None.
        q = _full_balance_sheet(
            "2024-03-31",
            "2024-05-01",
            ca=120,
            cash=22,
            cl=82,
            std=11,
            taxpay=4,
            dep=3,
            ta=520,
            fp="Q1",
        )
        store = self._make_store_with_concepts(q)
        self.assertIsNone(store.accruals_ratio("AAPL", date(2024, 6, 1)))

    def test_no_quarterly_entries_returns_none(self):
        # All entries are FY (10-K annual) -> no quarterly accruals possible.
        # Sloan accruals are quarterly per Hribar-Collins; FY-only is rejected.
        fy = _full_balance_sheet(
            "2023-12-31",
            "2024-02-15",
            ca=100,
            cash=20,
            cl=80,
            std=10,
            taxpay=5,
            dep=2,
            ta=500,
            fp="FY",
            form="10-K",
        )
        store = self._make_store_with_concepts(fy)
        self.assertIsNone(store.accruals_ratio("AAPL", date(2024, 6, 1)))


class TestSloanAccrualsErrorHandling(unittest.TestCase):
    """Defensive handling of missing data sources."""

    def test_unmapped_ticker_returns_none(self):
        from alphalens_research.screeners.event_drift.accruals import SloanAccrualsStore

        tmp = tempfile.mkdtemp()
        cf_dir = Path(tmp)
        cik_map = _StubTickerCikMap({})  # nothing mapped
        store = SloanAccrualsStore(CompanyfactsParquetReader(cf_dir), cik_map)
        self.assertIsNone(store.accruals_ratio("UNKNOWN", date(2024, 6, 1)))

    def test_missing_companyfacts_file_returns_none(self):
        from alphalens_research.screeners.event_drift.accruals import SloanAccrualsStore

        tmp = tempfile.mkdtemp()
        cf_dir = Path(tmp)
        # Mapping says CIK 9999999999 but no JSON file at that path
        cik_map = _StubTickerCikMap({"GHOST": "9999999999"})
        store = SloanAccrualsStore(CompanyfactsParquetReader(cf_dir), cik_map)
        self.assertIsNone(store.accruals_ratio("GHOST", date(2024, 6, 1)))


class TestSloanAccrualsQuarterPair(unittest.TestCase):
    """Quarter pair selection picks two MOST RECENT quarters visible at asof."""

    def _make_store_with_concepts(self, concepts: dict[str, list[dict]]):
        from alphalens_research.screeners.event_drift.accruals import SloanAccrualsStore

        cf = _make_companyfacts(concepts)
        tmp = tempfile.mkdtemp()
        cf_dir = Path(tmp)
        _write_parquet_for_cf(cf, cf_dir, "0000320193")
        cik_map = _StubTickerCikMap({"AAPL": "0000320193"})
        return SloanAccrualsStore(CompanyfactsParquetReader(cf_dir), cik_map)

    def test_quarterly_pair_picks_most_recent_two(self):
        # 4 quarters present. Should use Q4 vs Q3 (the last two), NOT older pairs.
        q1 = _full_balance_sheet(
            "2023-03-31",
            "2023-05-01",
            ca=100,
            cash=20,
            cl=80,
            std=10,
            taxpay=5,
            dep=2,
            ta=500,
            fp="Q1",
        )
        q2 = _full_balance_sheet(
            "2023-06-30",
            "2023-08-01",
            ca=110,
            cash=21,
            cl=81,
            std=11,
            taxpay=5,
            dep=2,
            ta=510,
            fp="Q2",
        )
        q3 = _full_balance_sheet(
            "2023-09-30",
            "2023-11-01",
            ca=115,
            cash=22,
            cl=82,
            std=12,
            taxpay=4,
            dep=3,
            ta=515,
            fp="Q3",
        )
        q4 = _full_balance_sheet(
            "2023-12-31",
            "2024-02-15",
            ca=120,
            cash=23,
            cl=85,
            std=13,
            taxpay=4,
            dep=3,
            ta=520,
            fp="Q4",
            form="10-K",
        )
        merged = _merge_balance_sheets(q1, q2, q3, q4)
        store = self._make_store_with_concepts(merged)
        # Pair = Q4 (2023-12-31) vs Q3 (2023-09-30):
        # delta_CA = 120-115=5; delta_Cash = 23-22=1; delta_CL = 85-82=3
        # delta_STD = 13-12=1; delta_TaxPay = 4-4=0
        # accruals = (5-1) - (3 - 1 - 0) - 3 = 4 - 2 - 3 = -1
        # avg_TA = (515 + 520)/2 = 517.5 -> ratio = -1/517.5
        ratio = store.accruals_ratio("AAPL", date(2024, 4, 1))
        self.assertIsNotNone(ratio)
        self.assertAlmostEqual(ratio, -1.0 / 517.5, places=6)

    def test_uses_pair_visible_at_asof_only(self):
        # Q3 visible (filed before asof), Q4 NOT yet visible (filed after asof).
        # Should fall back to Q2/Q3 pair (Q3 most recent visible).
        q1 = _full_balance_sheet(
            "2023-03-31",
            "2023-05-01",
            ca=100,
            cash=20,
            cl=80,
            std=10,
            taxpay=5,
            dep=2,
            ta=500,
            fp="Q1",
        )
        q2 = _full_balance_sheet(
            "2023-06-30",
            "2023-08-01",
            ca=110,
            cash=21,
            cl=81,
            std=11,
            taxpay=5,
            dep=2,
            ta=510,
            fp="Q2",
        )
        q3 = _full_balance_sheet(
            "2023-09-30",
            "2023-11-01",
            ca=115,
            cash=22,
            cl=82,
            std=12,
            taxpay=4,
            dep=3,
            ta=515,
            fp="Q3",
        )
        q4 = _full_balance_sheet(
            "2023-12-31",
            "2024-02-15",
            ca=120,
            cash=23,
            cl=85,
            std=13,
            taxpay=4,
            dep=3,
            ta=520,
            fp="Q4",
            form="10-K",
        )
        merged = _merge_balance_sheets(q1, q2, q3, q4)
        store = self._make_store_with_concepts(merged)
        # asof 2024-01-15 -> Q4 filing (2024-02-15) NOT visible.
        # Pair = Q3 vs Q2:
        # delta_CA = 115-110=5; delta_Cash = 22-21=1
        # delta_CL = 82-81=1; delta_STD = 12-11=1; delta_TaxPay = 4-5=-1
        # accruals = (5-1) - (1 - 1 - (-1)) - 3 = 4 - 1 - 3 = 0
        # avg_TA = (510+515)/2 = 512.5 -> ratio = 0
        ratio = store.accruals_ratio("AAPL", date(2024, 1, 15))
        self.assertIsNotNone(ratio)
        self.assertAlmostEqual(ratio, 0.0, places=6)


class TestSloanOnRepresentativeFixtures(unittest.TestCase):
    """Round-trip regression on the synthetic Apple / sparse / IPO fixtures."""

    def setUp(self):
        from alphalens_research.screeners.event_drift.accruals import SloanAccrualsStore
        from tests.fixtures.companyfacts_fixtures import (
            APPLE_CIK,
            IPO_CIK,
            SPARSE_CIK,
            write_all_fixtures_as_parquet,
        )

        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.parquet_dir = tmp / "companyfacts_parquet"
        write_all_fixtures_as_parquet(self.parquet_dir)
        cik_map = _StubTickerCikMap(
            {
                "AAPL": APPLE_CIK,
                "SPARSE_CO": SPARSE_CIK,
                "IPO_CO": IPO_CIK,
            }
        )
        self.store = SloanAccrualsStore(CompanyfactsParquetReader(self.parquet_dir), cik_map)

    def tearDown(self):
        self._tmp.cleanup()

    def test_apple_fixture_returns_finite_ratio(self):
        # Apple fixture provides all 7 Sloan concepts; ratio must be a real number.
        ratio = self.store.accruals_ratio("AAPL", date(2024, 6, 30))
        self.assertIsNotNone(ratio)
        # Sanity: Apple baselines drift +1.2% per quarter -> small ratio,
        # well within the [-1.0, 1.0] range typical for healthy S&P 500 firms.
        self.assertGreater(ratio, -1.0)
        self.assertLess(ratio, 1.0)

    def test_sparse_fixture_returns_none_due_to_missing_depreciation(self):
        # SPARSE_CO is missing DepreciationAndAmortization -> Sloan must fail.
        ratio = self.store.accruals_ratio("SPARSE_CO", date(2024, 6, 30))
        self.assertIsNone(ratio)

    def test_ipo_fixture_returns_none_due_to_missing_balance_sheet(self):
        # IPO_CO has no balance sheet at all -> Sloan must fail.
        ratio = self.store.accruals_ratio("IPO_CO", date(2024, 6, 30))
        self.assertIsNone(ratio)


if __name__ == "__main__":
    unittest.main()
