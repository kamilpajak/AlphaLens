"""Pandera schema contracts for pipeline boundary DataFrames.

Tests the runtime guards in ``alphalens_pipeline.data.schemas``. The
schemas pin the contracts that attribution / backtest entry points
depend on; these tests document and pin the failure modes the schemas
exist to catch.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd
from alphalens_pipeline.data.schemas import (
    CARHART_FACTOR_COLUMNS,
    CARHART_FACTORS_SCHEMA,
    PORTFOLIO_RETURNS_SCHEMA,
    validate_carhart_factors,
    validate_portfolio_returns,
)
from pandera.errors import SchemaError


def _valid_carhart_panel(n: int = 30) -> pd.DataFrame:
    idx = pd.date_range("2024-01-02", periods=n, freq="B")
    return pd.DataFrame(
        {col: np.random.default_rng(42).normal(0, 0.01, n) for col in CARHART_FACTOR_COLUMNS},
        index=idx,
    )


def _valid_portfolio_returns(n: int = 30) -> pd.Series:
    idx = pd.date_range("2024-01-02", periods=n, freq="B")
    return pd.Series(np.random.default_rng(42).normal(0, 0.005, n), index=idx, name="portfolio")


class TestCarhartFactorsSchema(unittest.TestCase):
    def test_valid_panel_passes(self):
        panel = _valid_carhart_panel()
        result = validate_carhart_factors(panel)
        self.assertEqual(len(result), len(panel))

    def test_missing_column_raises(self):
        panel = _valid_carhart_panel().drop(columns=["RF"])
        with self.assertRaises(SchemaError):
            validate_carhart_factors(panel)

    def test_string_dtype_raises(self):
        # Vendor returned RF as strings -> would silently corrupt subtract_rf path.
        panel = _valid_carhart_panel()
        panel["RF"] = panel["RF"].astype(str)
        with self.assertRaises(SchemaError):
            validate_carhart_factors(panel)

    def test_nan_in_factor_raises(self):
        # NaN in a factor column would survive into pd.concat(...).dropna()
        # and silently shrink n_obs below the HAC variance estimate's stable range.
        panel = _valid_carhart_panel()
        panel.iloc[5, panel.columns.get_loc("Mom")] = np.nan
        with self.assertRaises(SchemaError):
            validate_carhart_factors(panel)

    def test_out_of_range_value_raises(self):
        # Single-day return of +500% is unambiguously a bug (unit error, decimal-vs-bps mix).
        panel = _valid_carhart_panel()
        panel.iloc[3, panel.columns.get_loc("Mkt-RF")] = 5.0
        with self.assertRaises(SchemaError):
            validate_carhart_factors(panel)

    def test_non_datetime_index_raises(self):
        panel = _valid_carhart_panel().reset_index(drop=True)
        with self.assertRaises(SchemaError):
            validate_carhart_factors(panel)

    def test_extra_columns_allowed(self):
        # strict=False -> extra cols (e.g. vendor-supplied EXTRA_FACTOR) should not block.
        panel = _valid_carhart_panel()
        panel["Vendor_Custom"] = 0.0
        result = validate_carhart_factors(panel)
        self.assertIn("Vendor_Custom", result.columns)

    def test_tz_aware_index_raises(self):
        # FF/Dartmouth panels are tz-naive ("datetime64[ns]") by construction.
        # Someone localising to UTC mid-pipeline would silently break alignment
        # against tz-naive portfolio_returns -> empty regression overlap.
        panel = _valid_carhart_panel()
        panel.index = panel.index.tz_localize("UTC")
        with self.assertRaises(SchemaError):
            validate_carhart_factors(panel)

    def test_empty_panel_accepted(self):
        # Pandera accepts an empty DataFrame as long as the column schema is
        # satisfied (zero rows trivially satisfy all row-level checks). Pin the
        # behaviour explicitly so a future change to the schema doesn't silently
        # flip this — call sites rely on it (start > end -> empty slice flows
        # through normally rather than as a contract violation).
        empty = pd.DataFrame(
            {col: pd.Series(dtype=float) for col in CARHART_FACTOR_COLUMNS},
            index=pd.DatetimeIndex([], dtype="datetime64[ns]"),
        )
        result = validate_carhart_factors(empty)
        self.assertEqual(len(result), 0)

    def test_duplicate_dates_raise(self):
        # Schema asserts index uniqueness — duplicate dates indicate a data
        # plumbing bug (Carhart panel from concat without dedupe). Zen
        # pre-merge LOW: silent acceptance hides upstream pipeline errors.
        panel = _valid_carhart_panel(n=10)
        panel.index = pd.DatetimeIndex([panel.index[0]] * 10)
        with self.assertRaises(SchemaError):
            validate_carhart_factors(panel)


class TestPortfolioReturnsSchema(unittest.TestCase):
    def test_valid_series_passes(self):
        s = _valid_portfolio_returns()
        result = validate_portfolio_returns(s)
        self.assertEqual(len(result), len(s))

    def test_nan_raises(self):
        s = _valid_portfolio_returns()
        s.iloc[7] = np.nan
        with self.assertRaises(SchemaError):
            validate_portfolio_returns(s)

    def test_range_index_raises(self):
        # Common bug: caller forgets to set DatetimeIndex, regression then
        # silently produces empty alignment with factor panel.
        s = pd.Series(np.zeros(10))
        with self.assertRaises(SchemaError):
            validate_portfolio_returns(s)

    def test_object_dtype_raises(self):
        s = _valid_portfolio_returns().astype(object)
        with self.assertRaises(SchemaError):
            validate_portfolio_returns(s)

    def test_schema_is_importable(self):
        self.assertIsNotNone(CARHART_FACTORS_SCHEMA)
        self.assertIsNotNone(PORTFOLIO_RETURNS_SCHEMA)


if __name__ == "__main__":
    unittest.main()
