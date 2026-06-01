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

import datetime as dt

import numpy as np
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
    index=pa.Index("datetime64[ns]", name=None, unique=True),
    strict=False,
    coerce=False,
    name="carhart_factors",
)

PORTFOLIO_RETURNS_SCHEMA = pa.SeriesSchema(
    float,
    nullable=False,
    checks=_RETURN_RANGE,
    index=pa.Index("datetime64[ns]", name=None, unique=True),
    name=None,
    coerce=False,
)


# ---------------------------------------------------------------------------
# Thematic pipeline parquet-hop schemas (test-strategy Phase 2, #8 + #2 + #3)
#
# Each thematic stage writes a parquet the next stage reads back; the seams are
# ``~/.alphalens/thematic_*`` files, NOT in-process hand-offs, so a column
# rename or dtype drift on the writer is invisible to the reader's unit tests
# (they mock the frame). These schemas pin the columns + dtypes a CONSUMER
# actually depends on at each hop — deliberately NOT the full producer column
# set (the Pact over-specification lesson: asserting provider internals makes
# the contract brittle without protecting any consumer). ``strict=False`` so
# extra / future columns pass; only the listed columns are enforced.
#
# Legacy tolerance: the PR-2/PR-3 audit columns (``extraction_method``,
# ``template_id``, ``template_fields_json``) are ``required=False`` because old
# on-disk events parquets predate them and ``event_extractor._backfill_legacy_columns``
# fills them on read. The join keys + always-present core stay required.
#   - NEWS    → consumed by ``event_extractor.extract_daily`` (id/timestamp/tickers/title/url)
#   - EVENTS  → consumed by ``catalyst_resolver`` (news_id join + themes/event_type/confidence)
#   - CANDIDATES → consumed by ``screening.scorer`` (ticker/theme/verified)
#   - SCORED  → consumed by ``argumentation.orchestrator.generate_briefs``
#     (ticker/theme + catalyst_template_id/catalyst_template_facts_json projected
#     to brief_template_* — the same JSON-string-not-dict contract the Django
#     coercer relies on, see briefs.ingest.coerce.coerce_json_obj)

_CONFIDENCE_RANGE = pa.Check.in_range(0.0, 1.0, include_min=True, include_max=True)
_ZERO_OFFSET = dt.timedelta(0)


def _is_utc_aware(value: object) -> bool:
    """True for a tz-aware UTC Timestamp; False for tz-naive or non-datetime.

    Resolution-agnostic (pandas 3.0 defaults list columns to ``[us]`` not
    ``[ns]``), so we check the offset rather than pin the dtype unit. This is
    the #2-class guard: a tz-naive ``timestamp`` silently shifts every
    downstream UTC comparison.
    """
    offset = getattr(value, "utcoffset", None)
    return callable(offset) and offset() == _ZERO_OFFSET


_UTC_TS_CHECK = pa.Check(
    _is_utc_aware,
    element_wise=True,
    error="timestamp must be tz-aware UTC (offset 0), not tz-naive",
)


def _is_listlike(value: object) -> bool:
    """True for list / tuple / numpy-array cells (parquet deserialises list
    columns to numpy arrays), False for a scalar string — the
    ``primary_entities='NVDA'`` corruption class the schema must reject.

    Restricted to the concrete list-like types parquet actually yields; a bare
    ``hasattr(value, "tolist")`` would also accept a pandas Series or any custom
    object exposing ``tolist``, which is not the contract.
    """
    return isinstance(value, (list, tuple, np.ndarray))


_LISTLIKE_CHECK = pa.Check(
    _is_listlike,
    element_wise=True,
    error="cell must be list-like (list/tuple/ndarray), not a scalar",
)

# Logical-type guard for text columns. We keep ``dtype=None`` (pandas 3.0
# infer_string makes the storage dtype unstable — see module note), but a
# content check still rejects an ``int``/``float`` cell where the consumer
# expects a string. nullable=True drops None/NaN before this runs, so optional
# string columns (template_id, …) accept missing values.
_STR_CHECK = pa.Check(
    lambda v: isinstance(v, str),
    element_wise=True,
    error="cell must be a string",
)

# JSON columns hold a *serialised string*, never a native dict/list. A dict cell
# has dtype ``object`` (same as a string), so a dtype check alone can't catch it;
# storing a dict here breaks the parquet round-trip and double-handles the
# Django ``coerce_json_obj`` consumer. nullable=True drops None before this runs.
_JSON_STRING_CHECK = pa.Check(
    lambda v: isinstance(v, str),
    element_wise=True,
    error="JSON column must hold a serialised string, not a native dict/list",
)

# Hop 0 → 1: news_ingest writes ``NEWS_COLUMNS``; extract_daily reads it.
NEWS_FRAME_SCHEMA = pa.DataFrameSchema(
    columns={
        "id": pa.Column(None, nullable=False, checks=_STR_CHECK),
        "source": pa.Column(None, nullable=False, checks=_STR_CHECK),
        "timestamp": pa.Column(None, nullable=False, checks=_UTC_TS_CHECK),
        "tickers": pa.Column(None, nullable=False, checks=_LISTLIKE_CHECK),
        "title": pa.Column(None, nullable=False, checks=_STR_CHECK),
        "url": pa.Column(None, nullable=False, checks=_STR_CHECK),
    },
    strict=False,
    coerce=False,
    name="thematic_news",
)

# Hop 1 → 2: event_extractor writes events; catalyst_resolver reads them.
THEMATIC_EVENTS_SCHEMA = pa.DataFrameSchema(
    columns={
        "news_id": pa.Column(None, nullable=False, checks=_STR_CHECK),
        "event_type": pa.Column(None, nullable=False, checks=_STR_CHECK),
        "confidence": pa.Column(float, nullable=False, checks=_CONFIDENCE_RANGE),
        "themes": pa.Column(None, nullable=False, checks=_LISTLIKE_CHECK),
        "primary_entities": pa.Column(None, nullable=False, checks=_LISTLIKE_CHECK),
        # PR-2/PR-3 audit columns — backfilled on legacy frames, so not required.
        "extraction_method": pa.Column(
            None,
            nullable=False,
            required=False,
            checks=pa.Check.isin(("template", "flash")),
        ),
        "template_id": pa.Column(None, nullable=True, required=False, checks=_STR_CHECK),
        "template_fields_json": pa.Column(
            None, nullable=True, required=False, checks=_JSON_STRING_CHECK
        ),
    },
    strict=False,
    coerce=False,
    name="thematic_events",
)

# Hop 2 → 3: mapping.orchestrator writes candidates; scorer reads them.
THEMATIC_CANDIDATES_SCHEMA = pa.DataFrameSchema(
    columns={
        "ticker": pa.Column(None, nullable=False, checks=_STR_CHECK),
        "theme": pa.Column(None, nullable=False, checks=_STR_CHECK),
        "verified": pa.Column(bool, nullable=False),
    },
    strict=False,
    coerce=False,
    name="thematic_candidates",
)

# Hop 3 → 4: scorer writes scored; argumentation.orchestrator reads them.
# catalyst_template_facts_json is a JSON STRING (not a dict) — the Django
# coercer parses it; storing a dict here would break the parquet round-trip.
THEMATIC_SCORED_SCHEMA = pa.DataFrameSchema(
    columns={
        "ticker": pa.Column(None, nullable=False, checks=_STR_CHECK),
        "theme": pa.Column(None, nullable=False, checks=_STR_CHECK),
        "verified": pa.Column(bool, nullable=False),
        "layer4_weighted_score": pa.Column(float, nullable=True, required=False),
        "catalyst_template_id": pa.Column(None, nullable=True, required=False, checks=_STR_CHECK),
        "catalyst_template_facts_json": pa.Column(
            None, nullable=True, required=False, checks=_JSON_STRING_CHECK
        ),
    },
    strict=False,
    coerce=False,
    name="thematic_scored",
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


def validate_thematic_events(events: pd.DataFrame) -> pd.DataFrame:
    """Validate an events frame at the extract→catalyst hop.

    The required core (``news_id`` join key, ``themes``, ``event_type``,
    ``confidence``, ``primary_entities``) is always enforced. The PR-2/PR-3
    audit columns (``extraction_method``, ``template_id``,
    ``template_fields_json``) are ``required=False``, so a legacy frame that
    predates them validates WITHOUT calling
    ``event_extractor._backfill_legacy_columns`` first — the backfill is what
    the catalyst-resolver consumer needs, not what the schema requires. Raises
    pandera ``SchemaError`` on contract violation.
    """
    return THEMATIC_EVENTS_SCHEMA.validate(events)


def validate_thematic_scored(scored: pd.DataFrame) -> pd.DataFrame:
    """Validate a scored frame at the score→brief hop.

    Pins the columns ``generate_briefs`` projects into ``brief_*`` — including
    ``catalyst_template_facts_json`` as a JSON string (not a dict), the same
    shape the Django ``coerce_json_obj`` consumer expects.
    """
    return THEMATIC_SCORED_SCHEMA.validate(scored)


__all__ = [
    "CARHART_FACTORS_SCHEMA",
    "CARHART_FACTOR_COLUMNS",
    "NEWS_FRAME_SCHEMA",
    "PORTFOLIO_RETURNS_SCHEMA",
    "THEMATIC_CANDIDATES_SCHEMA",
    "THEMATIC_EVENTS_SCHEMA",
    "THEMATIC_SCORED_SCHEMA",
    "SchemaError",
    "validate_carhart_factors",
    "validate_portfolio_returns",
    "validate_thematic_events",
    "validate_thematic_scored",
]
