"""Tests for EdgarCompanyfactsROEStore.

Covers:
  - PIT correctness (filed-date filter vs restatement amendments)
  - TTM formula (current YTD + prior FY - prior YTD)
  - Matched-pair fallback parent (NetIncomeLoss / StockholdersEquity) ->
    consolidated (ProfitLoss / ...IncludingNonControllingInterest)
  - Common-equity adjustment (subtract PreferredStockValue, prefer
    NetIncomeLossAvailableToCommonStockholdersBasic when present)
  - End-date matching between numerator (NI period end) and denominator
    (Equity instant) -- prevents 8-K preliminary NI being paired with
    stale prior-quarter Equity
  - Negative-equity dropout
  - Unknown ticker / missing companyfacts JSON returns None gracefully
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from alphalens_pipeline.data.alt_data.ticker_cik_map import TickerCikMap
from alphalens_pipeline.data.fundamentals.edgar_companyfacts import EdgarCompanyfactsROEStore

# --- Synthetic-fixture helpers ----------------------------------------------


def _entry(end, val, *, start=None, fp="FY", form="10-K", filed=None, accn=None):
    e = {
        "end": end,
        "val": val,
        "accn": accn or "0000000000-00-000000",
        "fy": 2020,
        "fp": fp,
        "form": form,
        "filed": filed or end,
    }
    if start is not None:
        e["start"] = start
    return e


def _block(entries):
    return {"label": "", "description": "", "units": {"USD": list(entries)}}


def _write_companyfacts(directory: Path, cik: int, **concept_entries) -> Path:
    """Write a minimal companyfacts JSON file for tests.

    concept_entries keys are us-gaap concept names; values are lists of entry
    dicts (use _entry to build them).
    """
    facts = {name: _block(entries) for name, entries in concept_entries.items()}
    payload = {
        "cik": cik,
        "entityName": f"TEST_{cik}",
        "facts": {"us-gaap": facts},
    }
    cik_str = f"{cik:010d}"
    path = directory / f"{cik_str}.json"
    path.write_text(json.dumps(payload))
    return path


def _write_ticker_cik_map(path: Path, mapping: dict[str, int]) -> None:
    """Mapping keys are tickers, values are int CIKs (the loader normalises)."""
    import yaml

    path.write_text(yaml.safe_dump(dict(mapping)))


# --- Synthetic-fixture tests -------------------------------------------------


class EdgarROESyntheticTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cf_dir = self.tmp / "companyfacts"
        self.cf_dir.mkdir()
        self.map_path = self.tmp / "ticker_cik_map.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def _store(self, mapping: dict[str, int]) -> EdgarCompanyfactsROEStore:
        _write_ticker_cik_map(self.map_path, mapping)
        return EdgarCompanyfactsROEStore(
            companyfacts_dir=self.cf_dir,
            ticker_cik_map=TickerCikMap.load(self.map_path),
        )

    def test_unknown_ticker_returns_none(self):
        store = self._store({"AAPL": 320193})
        self.assertIsNone(store.roe_ttm("DOESNOTEXIST", date(2024, 1, 31)))

    def test_companyfacts_file_missing_returns_none(self):
        # Ticker maps to a CIK but no JSON written for that CIK.
        store = self._store({"GHOST": 9999999})
        self.assertIsNone(store.roe_ttm("GHOST", date(2024, 1, 31)))

    def test_basic_fy_roe(self):
        # FY ending 2023-12-31 filed 2024-02-15: NI = 100, Equity = 1000.
        # asof = 2024-03-01: TTM = FY = 100; ROE = 0.10.
        _write_companyfacts(
            self.cf_dir,
            cik=1,
            NetIncomeLoss=[
                _entry(
                    end="2023-12-31",
                    val=100,
                    start="2023-01-01",
                    fp="FY",
                    form="10-K",
                    filed="2024-02-15",
                ),
            ],
            StockholdersEquity=[
                _entry(end="2023-12-31", val=1000, fp="FY", form="10-K", filed="2024-02-15"),
            ],
        )
        store = self._store({"FOO": 1})
        roe = store.roe_ttm("FOO", date(2024, 3, 1))
        self.assertAlmostEqual(roe, 0.10, places=4)

    def test_negative_equity_returns_none(self):
        _write_companyfacts(
            self.cf_dir,
            cik=2,
            NetIncomeLoss=[
                _entry(
                    end="2023-12-31",
                    val=500,
                    start="2023-01-01",
                    fp="FY",
                    form="10-K",
                    filed="2024-02-15",
                ),
            ],
            StockholdersEquity=[
                _entry(end="2023-12-31", val=-1000, fp="FY", form="10-K", filed="2024-02-15"),
            ],
        )
        store = self._store({"NEG": 2})
        self.assertIsNone(store.roe_ttm("NEG", date(2024, 3, 1)))

    def test_pit_filter_excludes_filings_after_asof(self):
        # NI restated upward 6 months after original 10-K. asof BEFORE
        # the 10-K/A filed date must see the original value (=>ROE 0.10),
        # asof AFTER must see the restated value (=>ROE 0.20).
        _write_companyfacts(
            self.cf_dir,
            cik=3,
            NetIncomeLoss=[
                _entry(
                    end="2023-12-31",
                    val=100,
                    start="2023-01-01",
                    fp="FY",
                    form="10-K",
                    filed="2024-02-15",
                ),
                _entry(
                    end="2023-12-31",
                    val=200,
                    start="2023-01-01",
                    fp="FY",
                    form="10-K/A",
                    filed="2024-08-01",
                ),
            ],
            StockholdersEquity=[
                _entry(end="2023-12-31", val=1000, fp="FY", form="10-K", filed="2024-02-15"),
            ],
        )
        store = self._store({"PIT": 3})
        # Before amendment: original value visible
        self.assertAlmostEqual(store.roe_ttm("PIT", date(2024, 3, 1)), 0.10, places=4)
        # After amendment: restated value visible
        self.assertAlmostEqual(store.roe_ttm("PIT", date(2024, 9, 1)), 0.20, places=4)

    def test_ttm_formula_at_q3_asof_uses_ytd_plus_prior_fy_minus_prior_ytd(self):
        # AAPL-style FY ending Sep 30. Current YTD = Q3 cumulative (9m FY24).
        # Q3 FY24 9m ending 2024-06-30: 90
        # FY23 12m ending 2023-09-30: 100
        # Q3 FY23 9m ending 2023-06-30: 75
        # Expected TTM = 90 + 100 - 75 = 115
        # Equity must match latest YTD end = 2024-06-30 (Q3 balance sheet)
        # Equity 2024-06-30 = 1000 -> ROE = 115 / 1000 = 0.115
        _write_companyfacts(
            self.cf_dir,
            cik=4,
            NetIncomeLoss=[
                _entry(
                    end="2023-06-30",
                    val=75,
                    start="2022-10-01",
                    fp="Q3",
                    form="10-Q",
                    filed="2023-08-04",
                ),
                _entry(
                    end="2023-09-30",
                    val=100,
                    start="2022-10-01",
                    fp="FY",
                    form="10-K",
                    filed="2023-11-03",
                ),
                _entry(
                    end="2024-06-30",
                    val=90,
                    start="2023-10-01",
                    fp="Q3",
                    form="10-Q",
                    filed="2024-08-04",
                ),
            ],
            StockholdersEquity=[
                _entry(end="2023-09-30", val=900, fp="FY", form="10-K", filed="2023-11-03"),
                _entry(end="2024-06-30", val=1000, fp="Q3", form="10-Q", filed="2024-08-04"),
            ],
        )
        store = self._store({"TTM": 4})
        roe = store.roe_ttm("TTM", date(2024, 9, 1))
        self.assertAlmostEqual(roe, 0.115, places=4)

    def test_matched_pair_consolidated_fallback_when_parent_missing(self):
        # Filer reports only consolidated (ProfitLoss + ...IncludingNCI), no parent
        # variants. Store must fall back without mixing parent/consolidated.
        _write_companyfacts(
            self.cf_dir,
            cik=5,
            ProfitLoss=[
                _entry(
                    end="2023-12-31",
                    val=300,
                    start="2023-01-01",
                    fp="FY",
                    form="10-K",
                    filed="2024-02-15",
                ),
            ],
            StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest=[
                _entry(end="2023-12-31", val=2000, fp="FY", form="10-K", filed="2024-02-15"),
            ],
        )
        store = self._store({"CONS": 5})
        roe = store.roe_ttm("CONS", date(2024, 3, 1))
        self.assertAlmostEqual(roe, 0.15, places=4)

    def test_does_not_mix_parent_ni_with_consolidated_equity(self):
        # If only NetIncomeLoss (parent) + only StockholdersEquityIncludingNCI
        # are present, store must NOT mix-and-match. Either parent pair fully
        # available, or consolidated pair fully available, otherwise None.
        _write_companyfacts(
            self.cf_dir,
            cik=6,
            NetIncomeLoss=[
                _entry(
                    end="2023-12-31",
                    val=100,
                    start="2023-01-01",
                    fp="FY",
                    form="10-K",
                    filed="2024-02-15",
                ),
            ],
            StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest=[
                _entry(end="2023-12-31", val=1000, fp="FY", form="10-K", filed="2024-02-15"),
            ],
        )
        store = self._store({"MIX": 6})
        self.assertIsNone(store.roe_ttm("MIX", date(2024, 3, 1)))

    def test_common_equity_subtracts_preferred_when_present(self):
        # NI common = 80 (NI - preferred dividends 20),
        # Equity 1000, PreferredStockValue 200.
        # common ROE = 80 / (1000 - 200) = 0.10
        _write_companyfacts(
            self.cf_dir,
            cik=7,
            NetIncomeLoss=[
                _entry(
                    end="2023-12-31",
                    val=100,
                    start="2023-01-01",
                    fp="FY",
                    form="10-K",
                    filed="2024-02-15",
                ),
            ],
            NetIncomeLossAvailableToCommonStockholdersBasic=[
                _entry(
                    end="2023-12-31",
                    val=80,
                    start="2023-01-01",
                    fp="FY",
                    form="10-K",
                    filed="2024-02-15",
                ),
            ],
            StockholdersEquity=[
                _entry(end="2023-12-31", val=1000, fp="FY", form="10-K", filed="2024-02-15"),
            ],
            PreferredStockValue=[
                _entry(end="2023-12-31", val=200, fp="FY", form="10-K", filed="2024-02-15"),
            ],
        )
        store = self._store({"PRF": 7})
        roe = store.roe_ttm("PRF", date(2024, 3, 1))
        self.assertAlmostEqual(roe, 0.10, places=4)

    def test_partial_coverage_common_ni_does_not_subtract_preferred(self):
        # Matched-pair invariant: if NetIncomeLossAvailableToCommonStockholdersBasic
        # is reported but lacks the period entries needed to compute TTM at the
        # selected target_end, we must NOT silently fall back to (parent_NI /
        # common_equity) — that mixes incompatible numerator and denominator.
        # Setup: parent NI/Equity fully available at FY end. Common-NI block is
        # present but only has a Q1 entry (no FY → common_ttm returns None at
        # FY target_end). Preferred is reported at the same FY end.
        # Expected: ROE = parent_NI / parent_Equity (preferred NOT subtracted),
        # because the common-NI fallback failed and we must keep numerator and
        # denominator on the same paradigm.
        _write_companyfacts(
            self.cf_dir,
            cik=70,
            NetIncomeLoss=[
                _entry(
                    end="2023-12-31",
                    val=100,
                    start="2023-01-01",
                    fp="FY",
                    form="10-K",
                    filed="2024-02-15",
                ),
            ],
            NetIncomeLossAvailableToCommonStockholdersBasic=[
                # Only a Q1 entry — no FY entry, so _ttm_net_income at the FY
                # target_end returns None and the common-NI substitution silently
                # fails. The preferred subtraction must not happen.
                _entry(
                    end="2023-03-31",
                    val=20,
                    start="2023-01-01",
                    fp="Q1",
                    form="10-Q",
                    filed="2023-05-01",
                ),
            ],
            StockholdersEquity=[
                _entry(end="2023-12-31", val=1000, fp="FY", form="10-K", filed="2024-02-15"),
            ],
            PreferredStockValue=[
                _entry(end="2023-12-31", val=200, fp="FY", form="10-K", filed="2024-02-15"),
            ],
        )
        store = self._store({"PARTIAL": 70})
        roe = store.roe_ttm("PARTIAL", date(2024, 3, 1))
        # Must be parent_NI / parent_Equity = 100/1000 = 0.10.
        # The buggy path would yield 100/(1000-200) = 0.125 (parent NI over
        # common-adjusted equity — the matched-pair violation).
        self.assertAlmostEqual(roe, 0.10, places=4)

    def test_8k_preliminary_falls_back_to_matched_period(self):
        # 8-K issues a preliminary FY24 NI before the 10-K is filed; the
        # balance sheet for FY24 is not yet public. Latest matched (NI end
        # == Equity end) period is Q3 FY24. ROE should reflect Q3-based TTM,
        # not pair the 8-K NI with stale Q3 equity.
        # Setup: same shape as TTM test above, plus a stray 8-K NI at end=
        # 2024-09-30 (FY) that has no matching equity entry yet.
        _write_companyfacts(
            self.cf_dir,
            cik=8,
            NetIncomeLoss=[
                _entry(
                    end="2023-06-30",
                    val=75,
                    start="2022-10-01",
                    fp="Q3",
                    form="10-Q",
                    filed="2023-08-04",
                ),
                _entry(
                    end="2023-09-30",
                    val=100,
                    start="2022-10-01",
                    fp="FY",
                    form="10-K",
                    filed="2023-11-03",
                ),
                _entry(
                    end="2024-06-30",
                    val=90,
                    start="2023-10-01",
                    fp="Q3",
                    form="10-Q",
                    filed="2024-08-04",
                ),
                # 8-K preliminary FY24 NI: filed before 10-K, no balance sheet
                _entry(
                    end="2024-09-30",
                    val=130,
                    start="2023-10-01",
                    fp=None,
                    form="8-K",
                    filed="2024-10-30",
                ),
            ],
            StockholdersEquity=[
                _entry(end="2023-09-30", val=900, fp="FY", form="10-K", filed="2023-11-03"),
                _entry(end="2024-06-30", val=1000, fp="Q3", form="10-Q", filed="2024-08-04"),
            ],
        )
        store = self._store({"PRELIM": 8})
        # asof AFTER the 8-K but BEFORE 10-K: must use Q3-matched period
        # TTM = 90 + 100 - 75 = 115; equity = 1000; ROE = 0.115
        roe = store.roe_ttm("PRELIM", date(2024, 11, 15))
        self.assertAlmostEqual(roe, 0.115, places=4)


# --- AAPL acceptance tests against real cached companyfacts ------------------

_CACHED_AAPL_PATH = Path.home() / ".alphalens" / "companyfacts" / "0000320193.json"


@unittest.skipUnless(
    _CACHED_AAPL_PATH.exists(),
    f"AAPL companyfacts cache absent at {_CACHED_AAPL_PATH}; skipping acceptance tests",
)
class EdgarROEAppleAcceptanceTests(unittest.TestCase):
    """Acceptance tests: parse the real AAPL companyfacts JSON and verify the
    PIT-correct values align with figures pulled directly from the underlying
    10-K / 10-K/A filings.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cf_dir = cls.tmp / "companyfacts"
        cf_dir.mkdir()
        # Hard-link the cached AAPL file into the test cf dir so the store sees
        # only what the test expects (no other tickers in scope).
        (cf_dir / "0000320193.json").symlink_to(_CACHED_AAPL_PATH)
        map_path = cls.tmp / "ticker_cik_map.yaml"
        _write_ticker_cik_map(map_path, {"AAPL": 320193})
        cls.store = EdgarCompanyfactsROEStore(
            companyfacts_dir=cf_dir,
            ticker_cik_map=TickerCikMap.load(map_path),
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_fy2009_roe_uses_original_value_at_year_end(self):
        # asof = 2009-12-31: only the original 10-K (filed 2009-10-27) is
        # visible; the 10-K/A restating to $8.235B is filed 2010-01-25.
        # NI(orig) = 5,704,000,000; Equity(orig) = 27,832,000,000.
        # ROE = 5704 / 27832 ~= 0.2050
        roe = self.store.roe_ttm("AAPL", date(2009, 12, 31))
        self.assertIsNotNone(roe)
        self.assertAlmostEqual(roe, 5704 / 27832, places=3)

    def test_fy2009_roe_uses_restated_value_after_amendment(self):
        # asof = 2010-04-01: 10-K/A is visible (filed 2010-01-25) plus
        # Q1 FY10 10-Q. Picker should now reflect restatement.
        # The exact ROE depends on TTM construction at Q1 FY10. We assert
        # that the post-amendment ROE differs from the pre-amendment ROE
        # by more than measurement noise -- the PIT filter must change
        # which entries are visible.
        pre = self.store.roe_ttm("AAPL", date(2009, 12, 31))
        post = self.store.roe_ttm("AAPL", date(2010, 4, 1))
        self.assertIsNotNone(pre)
        self.assertIsNotNone(post)
        self.assertNotAlmostEqual(pre, post, places=2)

    def test_aapl_2024_roe_in_expected_range(self):
        # AAPL FY2023 (end 2023-09-30) NI = 96,995M; Equity = 62,146M
        # asof = 2024-01-31 (FY10-K filed 2023-11-03 visible; Q1 FY24 not yet).
        # ROE ~= 96995 / 62146 ~= 1.561 (high due to buyback-driven equity)
        roe = self.store.roe_ttm("AAPL", date(2024, 1, 31))
        self.assertIsNotNone(roe)
        # Loose bracket: AAPL TTM ROE in this period is well above 1.0
        # but below 2.0; tight equality would lock the test to one
        # specific TTM-formula edge case.
        self.assertGreater(roe, 1.2)
        self.assertLess(roe, 2.0)


class EdgarROECacheBoundTests(unittest.TestCase):
    """Issue #38 item 1: `_facts_cache` must FIFO-evict above capacity.

    Working set on a typical R2000 backtest is ~1200 unique CIKs × ~3MB
    JSON each ≈ 3.6GB peak. Default capacity 1500 (25% margin) caps RAM
    around 4.5GB on dev hardware while leaving room for legitimate
    cross-period universe expansion.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cf_dir = self.tmp / "companyfacts"
        self.cf_dir.mkdir()
        self.map_path = self.tmp / "ticker_cik_map.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def _store_with_n_tickers(self, n: int) -> tuple[EdgarCompanyfactsROEStore, list[str]]:
        """Write n distinct CIK fixtures and return (store, ticker_list)."""
        mapping: dict[str, int] = {}
        for i in range(n):
            cik = i + 1  # CIKs 1..n
            ticker = f"T{i:04d}"
            mapping[ticker] = cik
            _write_companyfacts(
                self.cf_dir,
                cik=cik,
                NetIncomeLoss=[
                    _entry(
                        end="2023-12-31",
                        val=100,
                        start="2023-01-01",
                        fp="FY",
                        form="10-K",
                        filed="2024-02-15",
                    ),
                ],
                StockholdersEquity=[
                    _entry(end="2023-12-31", val=1000, fp="FY", form="10-K", filed="2024-02-15"),
                ],
            )
        _write_ticker_cik_map(self.map_path, mapping)
        store = EdgarCompanyfactsROEStore(
            companyfacts_dir=self.cf_dir,
            ticker_cik_map=TickerCikMap.load(self.map_path),
        )
        return store, list(mapping.keys())

    def test_cache_evicts_oldest_when_capacity_exceeded(self):
        store, tickers = self._store_with_n_tickers(3)
        store._facts_cache_capacity = 2  # force eviction after the second insert

        # Load CIK 0000000001, then 2, then 3 — oldest (CIK 1) should be gone.
        for ticker in tickers:
            store.roe_ttm(ticker, date(2024, 3, 1))

        self.assertEqual(len(store._facts_cache), 2)
        # CIK strings are 10-digit zero-padded.
        self.assertNotIn("0000000001", store._facts_cache)
        self.assertIn("0000000002", store._facts_cache)
        self.assertIn("0000000003", store._facts_cache)

    def test_default_capacity_does_not_evict_typical_universe(self):
        store, tickers = self._store_with_n_tickers(50)
        # Default capacity 1500; 50 entries shouldn't trigger eviction.
        self.assertEqual(store._facts_cache_capacity, 1500)

        for ticker in tickers:
            store.roe_ttm(ticker, date(2024, 3, 1))

        self.assertEqual(len(store._facts_cache), 50)

    def test_eviction_event_emits_warning(self):
        """Warning fires only when an eviction actually happened — eviction
        means the working-set assumption was violated, which is the anomaly
        a reviewer needs to know about. Size-threshold warnings are dead
        code when working set is bounded."""
        store, tickers = self._store_with_n_tickers(3)
        store._facts_cache_capacity = 2

        with self.assertLogs(
            "alphalens_pipeline.data.fundamentals.edgar_companyfacts", level="WARNING"
        ) as cm:
            for ticker in tickers:
                store.roe_ttm(ticker, date(2024, 3, 1))

        eviction_logs = [m for m in cm.output if "evicted" in m.lower()]
        self.assertTrue(eviction_logs, f"expected eviction warning; got {cm.output}")


if __name__ == "__main__":
    unittest.main()
