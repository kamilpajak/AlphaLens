"""Short-interest telemetry enrichment (#1269) — TDD.

Telemetry-only forward stamp on the scored frame: settlement-dated FINRA
short interest via the existing PolygonShortInterestClient PIT surface.
Unlike options telemetry there is NO post-close window gate, NO freeze
marker and NO carry-forward — the value for a given asof is identical at
every run slot. market_state.enrich is the shape template (declared dtypes
on the empty frame, config version stamped unconditionally).
"""

from __future__ import annotations

import datetime as dt
import unittest
from datetime import date

import pandas as pd
from alphalens_pipeline.data.alt_data.polygon_short_interest import ShortInterestRecord
from alphalens_pipeline.thematic.short_interest_telemetry import enrichment as en

_ASOF = dt.date(2026, 9, 3)


def _record(ticker: str) -> ShortInterestRecord:
    return ShortInterestRecord(
        settlement_date=date(2026, 8, 14),
        ticker=ticker,
        short_interest=1_234_567,
        avg_daily_volume=500_000,
        days_to_cover=2.47,
    )


class _FakeClient:
    """DI stand-in for PolygonShortInterestClient (features_as_of surface)."""

    def __init__(self, records: dict[str, ShortInterestRecord | None]):
        self._records = records
        self.calls: list[tuple[str, dt.date, bool]] = []

    def features_as_of(self, ticker, asof, *, refresh_if_stale=False):
        self.calls.append((ticker, asof, refresh_if_stale))
        value = self._records.get(ticker.upper())
        if isinstance(value, Exception):
            raise value
        return value


def _frame(tickers: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "theme": ["q"] * len(tickers),
            "ticker": tickers,
            "company_name": [f"{t} Inc" for t in tickers],
        }
    )


class TestEnrichStampsShortInterest(unittest.TestCase):
    def test_happy_path_stamps_values_and_config_version(self):
        client = _FakeClient({"QUBT": _record("QUBT")})
        out = en.enrich(_frame(["QUBT"]), asof=_ASOF, si_client=client)

        row = out.iloc[0]
        self.assertEqual(float(row["si_shares_short"]), 1_234_567.0)
        self.assertAlmostEqual(float(row["si_days_to_cover"]), 2.47)
        self.assertEqual(row["si_settlement_date"], "2026-08-14")
        self.assertEqual(row["si_config_version"], en.SHORT_INTEREST_CONFIG_VERSION)

    def test_enrich_requests_staleness_refresh(self):
        # The per-ticker disk cache has no TTL; the stamper must opt into the
        # freshness rule or si_settlement_date freezes ~2 weeks after first touch.
        client = _FakeClient({"QUBT": _record("QUBT")})
        en.enrich(_frame(["QUBT"]), asof=_ASOF, si_client=client)
        self.assertEqual(client.calls, [("QUBT", _ASOF, True)])

    def test_no_pit_eligible_record_stamps_nulls_with_config_version(self):
        client = _FakeClient({"QUBT": None})
        out = en.enrich(_frame(["QUBT"]), asof=_ASOF, si_client=client)

        row = out.iloc[0]
        self.assertTrue(pd.isna(row["si_shares_short"]))
        self.assertTrue(pd.isna(row["si_days_to_cover"]))
        self.assertIsNone(row["si_settlement_date"])
        self.assertEqual(row["si_config_version"], en.SHORT_INTEREST_CONFIG_VERSION)

    def test_per_ticker_failure_is_fail_soft(self):
        # One ticker raising must not poison the others — nulls for the failed
        # row, real values for the rest, never a raise out of enrich.
        client = _FakeClient({"BAD": RuntimeError("polygon down"), "QUBT": _record("QUBT")})
        out = en.enrich(_frame(["BAD", "QUBT"]), asof=_ASOF, si_client=client)

        self.assertTrue(pd.isna(out.iloc[0]["si_shares_short"]))
        self.assertEqual(float(out.iloc[1]["si_shares_short"]), 1_234_567.0)
        self.assertEqual(out.iloc[0]["si_config_version"], en.SHORT_INTEREST_CONFIG_VERSION)

    def test_empty_frame_gets_columns_with_stable_dtypes(self):
        out = en.enrich(_frame([]), asof=_ASOF, si_client=_FakeClient({}))

        for col in en.SI_COLUMNS:
            self.assertIn(col, out.columns)
        self.assertEqual(str(out["si_shares_short"].dtype), "float64")
        self.assertEqual(str(out["si_days_to_cover"].dtype), "float64")
        self.assertEqual(str(out["si_settlement_date"].dtype), "object")
        self.assertEqual(str(out["si_config_version"].dtype), "object")

    def test_populated_frame_dtypes_match_empty_frame_schema(self):
        # All-null day must still produce the same parquet schema as a stamped
        # day (the market_state declared-dtype convention).
        client = _FakeClient({"QUBT": None})
        out = en.enrich(_frame(["QUBT"]), asof=_ASOF, si_client=client)
        self.assertEqual(str(out["si_shares_short"].dtype), "float64")
        self.assertEqual(str(out["si_days_to_cover"].dtype), "float64")

    def test_default_client_failure_stamps_nulls(self):
        # No DI client and from_env unavailable (e.g. no POLYGON_API_KEY):
        # stamp all-null + config version, never raise — the score stage
        # schema stays stable without the vendor.
        from unittest.mock import patch

        with patch.object(
            en,
            "_default_client",
            side_effect=RuntimeError("no key"),
        ):
            out = en.enrich(_frame(["QUBT"]), asof=_ASOF)
        self.assertTrue(pd.isna(out.iloc[0]["si_shares_short"]))
        self.assertEqual(out.iloc[0]["si_config_version"], en.SHORT_INTEREST_CONFIG_VERSION)


if __name__ == "__main__":
    unittest.main()
