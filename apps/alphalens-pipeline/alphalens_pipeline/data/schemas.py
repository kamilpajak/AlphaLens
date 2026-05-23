"""Pandera schemas for pipeline boundary DataFrames.

These are runtime contracts at the seams between layers — they catch
dtype drift, missing columns, NaN leakage, and index-type mistakes before
the value reaches the math. Each schema documents the WHY a specific
constraint matters; copy the rationale into the code that calls
``Schema.validate(df)`` if it's not already in the surrounding docstring.

Schemas defined here:
  - ``CARHART_FACTORS_SCHEMA`` — daily factor panel consumed by
    ``alphalens_research.attribution.factor_analysis.run_regression``.
    Wrong dtype on RF (object instead of float) silently demotes ``y =
    port - RF`` to elementwise subtraction-with-broadcasting, which would
    give a non-zero but wrong alpha. NaN in any factor column silently
    drops rows via ``pd.concat(..., join='inner').dropna()`` and a partial
    panel produces a low n_obs that breaks the HAC variance estimate.

  - ``PORTFOLIO_RETURNS_SCHEMA`` — the canonical
    ``BacktestReport.portfolio_returns`` shape (pd.Series of float, with
    DatetimeIndex). Wrong index type (int, RangeIndex) on the inbound
    series breaks ``pd.concat`` alignment to the factor panel and
    silently produces an empty regression — that's the failure mode
    pandera catches here.

These schemas are imported by attribution / backtest call sites; they
intentionally live in ``alphalens_pipeline.data`` because (per the
workspace DAG in CLAUDE.md / ADR 0011) ``alphalens_research`` may import
from ``alphalens_pipeline.{data, core, scorers}`` but not the reverse.
Schemas are pipeline-side so research-side validators can consume them
without inverting the dependency direction.
"""

from __future__ import annotations

import pandas as pd
import pandera.pandas as pa
from pandera.errors import SchemaError

CARHART_FACTOR_COLUMNS = ("Mkt-RF", "SMB", "HML", "Mom", "RF")

_RETURN_RANGE = pa.Check.in_range(-1.0, 1.0, include_min=True, include_max=True)

CARHART_FACTORS_SCHEMA = pa.DataFrameSchema(
    columns={
        col: pa.Column(float, nullable=False, checks=_RETURN_RANGE)
        for col in CARHART_FACTOR_COLUMNS
    },
    index=pa.Index("datetime64[ns]", name=None),
    strict=False,
    coerce=False,
    name="carhart_factors",
)

PORTFOLIO_RETURNS_SCHEMA = pa.SeriesSchema(
    float,
    nullable=False,
    checks=_RETURN_RANGE,
    index=pa.Index("datetime64[ns]", name=None),
    name=None,
    coerce=False,
)


def validate_carhart_factors(factors: pd.DataFrame) -> pd.DataFrame:
    """Validate a Carhart factor panel before regression.

    Returns the input unchanged on success. Raises pandera ``SchemaError``
    on contract violation. Call at attribution-layer entry points; the
    validation cost is one pass over the panel — cheap relative to the
    subsequent HAC regression.
    """
    return CARHART_FACTORS_SCHEMA.validate(factors)


def validate_portfolio_returns(returns: pd.Series) -> pd.Series:
    """Validate a portfolio-returns series before any downstream attribution.

    Returns the input unchanged on success. Raises pandera ``SchemaError``
    on contract violation (wrong dtype, NaN, wrong index type, out-of-range
    returns).
    """
    return PORTFOLIO_RETURNS_SCHEMA.validate(returns)


__all__ = [
    "CARHART_FACTORS_SCHEMA",
    "CARHART_FACTOR_COLUMNS",
    "PORTFOLIO_RETURNS_SCHEMA",
    "SchemaError",
    "validate_carhart_factors",
    "validate_portfolio_returns",
]
