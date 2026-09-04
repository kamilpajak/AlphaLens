"""Stamp si_* short-interest telemetry columns onto the scored frame (#1269).

Telemetry-only, parquet-only SoT: Django ingest drops the columns by design
(model-field-driven ingest, like options_*). Source is the existing
:class:`PolygonShortInterestClient` whose ``features_as_of`` already enforces
the PIT contract (settlement + 8 business days of FINRA dissemination lag
must be on or before the brief asof).

Shape follows ``market_state.enrich``, not options telemetry: short interest
is a settlement-dated biweekly print, identical at every run slot for a given
asof — so there is NO post-close window gate, NO freeze marker and NO
previous-slot carry-forward. Declared dtypes keep the parquet schema stable
across all-null and zero-row days.

``si_pct_float`` from the issue is deferred: Polygon's short-interest payload
carries no float / shares-outstanding, so the ratio has no source here.
Adding the column later is a non-event for the per-day parquets.
"""

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

logger = logging.getLogger(__name__)

SHORT_INTEREST_CONFIG_VERSION = "short-interest-telemetry-v1-polygon"

SI_COLUMNS: tuple[str, ...] = (
    "si_shares_short",
    "si_days_to_cover",
    "si_settlement_date",
    "si_config_version",
)

_EMPTY_COLUMN_DTYPES: dict[str, str] = {
    "si_shares_short": "float64",
    "si_days_to_cover": "float64",
    "si_settlement_date": "object",
    "si_config_version": "object",
}


def _default_client():
    from alphalens_pipeline.data.alt_data.polygon_short_interest import (
        PolygonShortInterestClient,
    )

    return PolygonShortInterestClient.from_env()


def _record_for(si_client, ticker: str, asof: dt.date):
    """One ticker's PIT-eligible record, or None. Never raises (fail-soft)."""
    if si_client is None:
        return None
    try:
        # refresh_if_stale opts into the cache-freshness rule — without it the
        # no-TTL per-ticker disk cache freezes si_settlement_date ~2 weeks
        # after a ticker's first touch.
        return si_client.features_as_of(ticker, asof, refresh_if_stale=True)
    except Exception:
        logger.warning(
            "short-interest telemetry: fetch failed for %s — stamping nulls",
            ticker,
            exc_info=True,
        )
        return None


def enrich(frame: pd.DataFrame, *, asof: dt.date, si_client=None) -> pd.DataFrame:
    """Return ``frame`` with the four si_* telemetry columns stamped per row.

    ``si_config_version`` is stamped unconditionally — even when the vendor
    client cannot be built (e.g. no POLYGON_API_KEY) every value column is
    null but the schema is intact. With no rows, all columns are still added
    (zero length, stable dtypes) for a stable parquet schema.
    """
    out = frame.copy()
    if len(out) == 0:
        for col, dtype in _EMPTY_COLUMN_DTYPES.items():
            out[col] = pd.Series([], dtype=dtype)
        return out

    if si_client is None:
        try:
            si_client = _default_client()
        except Exception:
            logger.warning(
                "short-interest telemetry: client unavailable — stamping nulls",
                exc_info=True,
            )
            si_client = None

    shares: list[float | None] = []
    days_to_cover: list[float | None] = []
    settlement: list[str | None] = []
    for ticker in out["ticker"].astype(str):
        rec = _record_for(si_client, ticker, asof)
        shares.append(float(rec.short_interest) if rec is not None else None)
        days_to_cover.append(float(rec.days_to_cover) if rec is not None else None)
        settlement.append(rec.settlement_date.isoformat() if rec is not None else None)

    out["si_shares_short"] = pd.Series(shares, index=out.index, dtype="float64")
    out["si_days_to_cover"] = pd.Series(days_to_cover, index=out.index, dtype="float64")
    out["si_settlement_date"] = pd.Series(settlement, index=out.index, dtype="object")
    out["si_config_version"] = SHORT_INTEREST_CONFIG_VERSION
    return out
