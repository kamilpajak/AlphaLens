"""Unit tests for the Buffett Mode-A comparison lens (ticket #511).

Drives :func:`compute_panel` and :func:`build_comparison` against a hand-built
fake store + fake market-cap / dividends callables — NO network. The fake store
exposes the four real accessor methods (``ev_fcff_features_as_of``,
``annual_series_as_of``, ``owner_earnings_as_of``, ``capital_allocation_as_of``)
and the panel assembler is otherwise pure.

Covered:

- happy path: a complete ticker resolves every Buffett-delta field with
  hand-verified numbers and ``data_coverage == 1.0``;
- patchy: a ticker whose store returns ``None`` / empty lists yields all-None
  fields, a low ``data_coverage``, and NEVER raises;
- margin-of-safety guard: a non-positive per-share intrinsic value or a missing
  price yields ``margin_of_safety_pct = None`` (no raise);
- dividend yield only sums ex-dates inside the trailing-365-day window;
- ``build_comparison`` preserves brief order and length.
"""

from __future__ import annotations

import unittest
from datetime import date

import pandas as pd
from alphalens_pipeline.buffett.comparison import (
    DEFAULT_GROWTH,
    DEFAULT_HURDLE_RATE,
    DEFAULT_TERMINAL_GROWTH,
    BuffettPanel,
    build_comparison,
    compute_panel,
)
from alphalens_pipeline.data.fundamentals.annual_aggregator import AnnualStatement
from alphalens_pipeline.data.fundamentals.capital_allocation import CapitalAllocation
from alphalens_pipeline.data.fundamentals.owner_earnings import OwnerEarnings
from alphalens_pipeline.data.fundamentals.valuation_dcf import (
    discount_owner_earnings,
    margin_of_safety,
)

ASOF = date(2024, 6, 30)


def _annual(
    year: int,
    *,
    revenue: float | None = None,
    operating_income: float | None = None,
    long_term_debt: float | None = None,
    short_term_debt: float | None = None,
    total_equity: float | None = None,
    cash_and_equivalents: float | None = None,
    shares_outstanding: float | None = None,
) -> AnnualStatement:
    """Hand-built AnnualStatement; only the trend-relevant fields are set."""
    return AnnualStatement(
        fiscal_year_end=date(year, 12, 31),
        fy=year,
        filed_date=date(year + 1, 2, 15),
        revenue=revenue,
        operating_income=operating_income,
        net_income=None,
        ocf=None,
        capex=None,
        da=None,
        total_equity=total_equity,
        long_term_debt=long_term_debt,
        short_term_debt=short_term_debt,
        cash_and_equivalents=cash_and_equivalents,
        shares_outstanding=shares_outstanding,
        accounts_receivable=None,
        inventory=None,
        accounts_payable=None,
    )


def _owner_earnings(year: int, owner_earnings: float | None) -> OwnerEarnings:
    return OwnerEarnings(
        fiscal_year_end=date(year, 12, 31),
        fy=year,
        owner_earnings=owner_earnings,
        maintenance_capex=None,
        working_capital=None,
        working_capital_change=None,
    )


def _capital_allocation(
    year: int, *, shares_change_pct: float | None, net_buyback: bool | None
) -> CapitalAllocation:
    return CapitalAllocation(
        fiscal_year_end=date(year, 12, 31),
        fy=year,
        shares_outstanding=None,
        shares_change=None,
        shares_change_pct=shares_change_pct,
        net_buyback=net_buyback,
    )


class _FakeStore:
    """Stand-in for ``EdgarFundamentalsStore`` exposing the four accessors.

    Construct with the exact per-ticker payloads the test needs; an unknown
    ticker returns the empty / None default for every method (the patchy path).
    """

    def __init__(
        self,
        *,
        features: dict[str, dict | None] | None = None,
        annual: dict[str, list[AnnualStatement]] | None = None,
        owner: dict[str, list[OwnerEarnings]] | None = None,
        capital: dict[str, list[CapitalAllocation]] | None = None,
    ) -> None:
        self._features = features or {}
        self._annual = annual or {}
        self._owner = owner or {}
        self._capital = capital or {}

    def ev_fcff_features_as_of(self, ticker: str, asof: date) -> dict | None:
        return self._features.get(ticker)

    def annual_series_as_of(
        self, ticker: str, asof: date, *, max_years: int = 10
    ) -> list[AnnualStatement]:
        return self._annual.get(ticker, [])

    def owner_earnings_as_of(
        self, ticker: str, asof: date, *, max_years: int = 10
    ) -> list[OwnerEarnings]:
        return self._owner.get(ticker, [])

    def capital_allocation_as_of(
        self, ticker: str, asof: date, *, max_years: int = 10
    ) -> list[CapitalAllocation]:
        return self._capital.get(ticker, [])


def _no_dividends(ticker: str, *, asof: date | None = None) -> pd.Series:
    return pd.Series(dtype=float)


def _zero_mcap(ticker: str, *, asof: date | None = None) -> float | None:
    return None


class TestComputePanelHappyPath(unittest.TestCase):
    def test_complete_ticker_resolves_every_field(self):
        # Latest year (2023) drives every delta. Price = 50, shares = 100.
        # operating_income 200, revenue 1000 -> op margin 20%.
        # invested = ltd 100 + std 50 + equity 600 - cash 100 = 650
        #   -> roic = 100 * 200 / 650 = 30.769...%
        features = {
            "operating_income_ttm": 200.0,
            "long_term_debt": 100.0,
            "short_term_debt": 50.0,
            "total_equity": 600.0,
            "cash_and_equivalents": 100.0,
            "price": 50.0,
            "shares_outstanding": 100.0,
        }
        # Three fiscal years for 3y averages.
        annual = [
            _annual(
                2023,
                revenue=1000.0,
                operating_income=200.0,
                long_term_debt=100.0,
                short_term_debt=50.0,
                total_equity=600.0,
                cash_and_equivalents=100.0,
                shares_outstanding=100.0,
            ),
            _annual(
                2022,
                revenue=900.0,
                operating_income=180.0,
                long_term_debt=100.0,
                short_term_debt=50.0,
                total_equity=550.0,
                cash_and_equivalents=100.0,
            ),
            _annual(
                2021,
                revenue=800.0,
                operating_income=160.0,
                long_term_debt=100.0,
                short_term_debt=50.0,
                total_equity=500.0,
                cash_and_equivalents=100.0,
            ),
        ]
        owner = [_owner_earnings(2023, 120.0), _owner_earnings(2022, 110.0)]
        capital = [_capital_allocation(2023, shares_change_pct=-0.02, net_buyback=True)]

        store = _FakeStore(
            features={"AAA": features},
            annual={"AAA": annual},
            owner={"AAA": owner},
            capital={"AAA": capital},
        )

        def _mcap(ticker: str, *, asof: date | None = None) -> float | None:
            return 5000.0  # 50 * 100

        # Two dividends inside the trailing 365d window: 0.5 + 0.5 = 1.0/share.
        def _dividends(ticker: str, *, asof: date | None = None) -> pd.Series:
            return pd.Series(
                [0.5, 0.5],
                index=pd.to_datetime([date(2023, 9, 15), date(2024, 3, 15)]),
            )

        panel = compute_panel(
            "AAA",
            "AI infrastructure",
            ASOF,
            store=store,
            mcap_fn=_mcap,
            dividends_fn=_dividends,
        )

        self.assertIsInstance(panel, BuffettPanel)
        self.assertEqual(panel.ticker, "AAA")
        self.assertEqual(panel.theme, "AI infrastructure")
        self.assertEqual(panel.market_cap, 5000.0)
        self.assertEqual(panel.owner_earnings_latest, 120.0)

        # owner_earnings_yield_pct = 100 * 120 / 5000 = 2.4%.
        self.assertAlmostEqual(panel.owner_earnings_yield_pct, 2.4)

        # roic_latest = 100 * 200 / 650.
        self.assertAlmostEqual(panel.roic_latest, 100.0 * 200.0 / 650.0)
        # roic per year (invested = ltd + std + equity - cash):
        #   2023 -> 200 / (100+50+600-100)=650
        #   2022 -> 180 / (100+50+550-100)=600
        #   2021 -> 160 / (100+50+500-100)=550
        roic_2023 = 100.0 * 200.0 / 650.0
        roic_2022 = 100.0 * 180.0 / 600.0
        roic_2021 = 100.0 * 160.0 / 550.0
        self.assertAlmostEqual(panel.roic_3y_avg, (roic_2023 + roic_2022 + roic_2021) / 3)

        # op margin: 2023 20%, 2022 20%, 2021 20% -> latest 20, avg 20.
        self.assertAlmostEqual(panel.op_margin_latest, 20.0)
        self.assertAlmostEqual(panel.op_margin_3y_avg, 20.0)

        # DCF: enterprise value of no-growth owner earnings 120 at 10% hurdle.
        ev = discount_owner_earnings(
            120.0,
            growth_rate=DEFAULT_GROWTH,
            terminal_growth=DEFAULT_TERMINAL_GROWTH,
            discount_rate=DEFAULT_HURDLE_RATE,
            years=10,
        )
        net_cash = 100.0 - (100.0 + 50.0)  # cash - total debt = -50
        equity_value = ev + net_cash
        per_share = equity_value / 100.0
        self.assertAlmostEqual(panel.intrinsic_value_per_share, per_share)
        self.assertAlmostEqual(
            panel.margin_of_safety_pct, 100.0 * margin_of_safety(per_share, 50.0)
        )

        # buyback: shares_change_pct -0.02 -> -2.0%.
        self.assertAlmostEqual(panel.buyback_pct, -2.0)

        # dividend yield = 100 * 1.0 / 50 = 2.0%.
        self.assertAlmostEqual(panel.dividend_yield_pct, 2.0)

        self.assertEqual(panel.data_coverage, 1.0)


class TestComputePanelPatchy(unittest.TestCase):
    def test_empty_store_yields_all_none_no_raise(self):
        store = _FakeStore()  # every accessor returns None / empty
        panel = compute_panel(
            "ZZZ",
            "obscure micro-cap",
            ASOF,
            store=store,
            mcap_fn=_zero_mcap,
            dividends_fn=_no_dividends,
        )
        self.assertIsNone(panel.owner_earnings_yield_pct)
        self.assertIsNone(panel.roic_latest)
        self.assertIsNone(panel.roic_3y_avg)
        self.assertIsNone(panel.op_margin_latest)
        self.assertIsNone(panel.op_margin_3y_avg)
        self.assertIsNone(panel.intrinsic_value_per_share)
        self.assertIsNone(panel.margin_of_safety_pct)
        self.assertIsNone(panel.buyback_pct)
        self.assertIsNone(panel.net_buyback)
        self.assertIsNone(panel.dividend_yield_pct)
        # data_coverage counts the 6 resolved-or-not Buffett fields; none here.
        self.assertEqual(panel.data_coverage, 0.0)


class TestMarginOfSafetyGuard(unittest.TestCase):
    def test_negative_per_share_yields_none(self):
        # Tiny owner earnings + huge net debt -> negative equity value -> per
        # share <= 0 -> margin_of_safety_pct must be None, never a raise.
        features = {
            "long_term_debt": 1_000_000.0,
            "short_term_debt": 0.0,
            "cash_and_equivalents": 0.0,
            "price": 10.0,
            "shares_outstanding": 100.0,
        }
        annual = [
            _annual(
                2023,
                long_term_debt=1_000_000.0,
                short_term_debt=0.0,
                cash_and_equivalents=0.0,
                shares_outstanding=100.0,
            )
        ]
        owner = [_owner_earnings(2023, 1.0)]
        store = _FakeStore(
            features={"DBT": features},
            annual={"DBT": annual},
            owner={"DBT": owner},
        )

        def _mcap(ticker: str, *, asof: date | None = None) -> float | None:
            return 1000.0

        panel = compute_panel(
            "DBT",
            "leveraged",
            ASOF,
            store=store,
            mcap_fn=_mcap,
            dividends_fn=_no_dividends,
        )
        self.assertIsNone(panel.margin_of_safety_pct)

    def test_missing_price_yields_none_mos(self):
        features = {
            "long_term_debt": 0.0,
            "short_term_debt": 0.0,
            "cash_and_equivalents": 0.0,
            "price": None,
            "shares_outstanding": 100.0,
        }
        annual = [
            _annual(
                2023,
                long_term_debt=0.0,
                short_term_debt=0.0,
                cash_and_equivalents=0.0,
                shares_outstanding=100.0,
            )
        ]
        owner = [_owner_earnings(2023, 50.0)]
        store = _FakeStore(
            features={"NOP": features},
            annual={"NOP": annual},
            owner={"NOP": owner},
        )

        def _mcap(ticker: str, *, asof: date | None = None) -> float | None:
            return 1000.0

        panel = compute_panel(
            "NOP",
            "no-price",
            ASOF,
            store=store,
            mcap_fn=_mcap,
            dividends_fn=_no_dividends,
        )
        # Per-share intrinsic value is positive, but no price -> MoS None.
        self.assertIsNotNone(panel.intrinsic_value_per_share)
        self.assertIsNone(panel.margin_of_safety_pct)


class TestDividendYieldWindow(unittest.TestCase):
    def test_only_in_window_dividends_summed(self):
        features = {"price": 100.0}
        store = _FakeStore(features={"DIV": features})

        # asof = 2024-06-30. Trailing 365d window is (2023-07-01, 2024-06-30].
        # 0.4 on 2023-08-01 (in), 0.6 on 2024-05-01 (in), 0.9 on 2023-01-01
        # (OUT, > 365d before asof). In-window sum = 1.0/share.
        def _dividends(ticker: str, *, asof: date | None = None) -> pd.Series:
            return pd.Series(
                [0.9, 0.4, 0.6],
                index=pd.to_datetime([date(2023, 1, 1), date(2023, 8, 1), date(2024, 5, 1)]),
            )

        def _mcap(ticker: str, *, asof: date | None = None) -> float | None:
            return 1000.0

        panel = compute_panel(
            "DIV",
            "dividend payer",
            ASOF,
            store=store,
            mcap_fn=_mcap,
            dividends_fn=_dividends,
        )
        # 100 * 1.0 / 100 = 1.0%.
        self.assertAlmostEqual(panel.dividend_yield_pct, 1.0)


class TestBuildComparison(unittest.TestCase):
    def test_preserves_brief_order_and_length(self):
        from unittest import mock

        from alphalens_pipeline.buffett import comparison as comp_mod

        candidates = [
            mock.Mock(ticker="CCC", theme="theme-c"),
            mock.Mock(ticker="AAA", theme="theme-a"),
            mock.Mock(ticker="BBB", theme="theme-b"),
        ]
        store = _FakeStore()

        with mock.patch.object(comp_mod, "load_brief", return_value=candidates):
            panels = build_comparison(
                ASOF,
                store=store,
                mcap_fn=_zero_mcap,
                dividends_fn=_no_dividends,
            )

        self.assertEqual([p.ticker for p in panels], ["CCC", "AAA", "BBB"])
        self.assertEqual([p.theme for p in panels], ["theme-c", "theme-a", "theme-b"])
        self.assertEqual(len(panels), 3)


class TestExecCompWiring(unittest.TestCase):
    """#507 PR-7b: an injected exec_comp_fn populates the optional PvP panel
    fields WITHOUT entering the 6-field data_coverage basket (the PvP rule is
    post-2023, so its absence must not count as a data defect)."""

    def test_exec_comp_fn_populates_without_diluting_coverage(self):
        from alphalens_pipeline.buffett.comparison import _COVERAGE_FIELDS
        from alphalens_pipeline.buffett.exec_comp import ExecCompCoverage, ExecCompFacts

        facts = ExecCompFacts(cik="1", coverage=ExecCompCoverage.PRESENT, peo_to_neo_ratio=5.0)
        panel = compute_panel(
            "ZZZ",
            "t",
            ASOF,
            store=_FakeStore(),
            mcap_fn=_zero_mcap,
            dividends_fn=_no_dividends,
            exec_comp_fn=lambda _t, _a: facts,
        )
        self.assertEqual(panel.peo_to_neo_ratio, 5.0)
        self.assertEqual(panel.exec_comp_coverage, "present")
        # All 6 basket fields are None here → coverage 0 even though the ratio is set.
        self.assertEqual(panel.data_coverage, 0.0)
        self.assertEqual(len(_COVERAGE_FIELDS), 6)
        self.assertNotIn("peo_to_neo_ratio", _COVERAGE_FIELDS)

    def test_no_exec_comp_fn_leaves_fields_none(self):
        panel = compute_panel(
            "ZZZ", "t", ASOF, store=_FakeStore(), mcap_fn=_zero_mcap, dividends_fn=_no_dividends
        )
        self.assertIsNone(panel.peo_to_neo_ratio)
        self.assertIsNone(panel.exec_comp_coverage)

    def test_exec_comp_fn_failure_is_failsoft(self):
        def _boom(_t, _a):
            raise RuntimeError("sec down")

        panel = compute_panel(
            "ZZZ",
            "t",
            ASOF,
            store=_FakeStore(),
            mcap_fn=_zero_mcap,
            dividends_fn=_no_dividends,
            exec_comp_fn=_boom,
        )
        self.assertIsNone(panel.peo_to_neo_ratio)  # _safe swallows → None, no crash


if __name__ == "__main__":
    unittest.main()
