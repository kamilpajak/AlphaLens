"""Brief parquet → CandidateBrief decoder tests.

Uses an in-memory pandas DataFrame round-tripped through parquet so the
contract with the real ``thematic_briefs/<date>.parquet`` shape is verified
end-to-end (rather than against a Python dict that could drift from the
parquet column conventions).
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.paper.brief_loader import load_brief


def _sample_setup() -> dict:
    return {
        "schema_version": "1.0.0",
        "status": "OK",
        "asof_close": 100.0,
        "atr": 1.5,
        "disaster_stop": 80.0,
        "suggested_size_pct": 5.0,
        "order_ttl_days": 10,
        "entry_tiers": [{"limit": 100.0, "alloc_pct": 100.0, "atr_distance": 0.0, "tag": "t0"}],
        "tp_tranches": [{"target": 110.0, "tranche_pct": 100.0, "r_multiple": 1.0, "tag": "tp"}],
    }


def _write_brief(dirpath: Path, brief_date: dt.date, rows: list[dict]) -> Path:
    path = dirpath / f"{brief_date.isoformat()}.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


class TestLoadBrief(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_verified_candidate_with_full_trade_setup(self):
        d = dt.date(2026, 5, 28)
        _write_brief(
            self.tmpdir,
            d,
            [
                {
                    "ticker": "NVDA",
                    "theme": "ai-infra",
                    "verified": True,
                    "brief_trade_setup": json.dumps(_sample_setup()),
                    "n_gates_passed": 4,
                    "n_gates_failed": 0,
                    "layer4_weighted_score": 18.0,
                }
            ],
        )
        candidates = load_brief(d, self.tmpdir)

        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(c.ticker, "NVDA")
        self.assertEqual(c.theme, "ai-infra")
        self.assertTrue(c.verified)
        self.assertEqual(c.suggested_size_pct, 5.0)
        self.assertEqual(c.trade_setup["status"], "OK")
        self.assertEqual(c.n_gates_passed, 4)

    def test_missing_brief_trade_setup_yields_none(self):
        d = dt.date(2026, 5, 28)
        _write_brief(
            self.tmpdir,
            d,
            [{"ticker": "OLD", "theme": "x", "verified": False}],
        )
        candidates = load_brief(d, self.tmpdir)
        self.assertIsNone(candidates[0].trade_setup)
        self.assertIsNone(candidates[0].suggested_size_pct)

    def test_unparseable_trade_setup_logged_and_skipped(self):
        d = dt.date(2026, 5, 28)
        _write_brief(
            self.tmpdir,
            d,
            [
                {
                    "ticker": "NVDA",
                    "theme": "ai-infra",
                    "verified": True,
                    "brief_trade_setup": "{not valid json",
                }
            ],
        )
        candidates = load_brief(d, self.tmpdir)
        self.assertIsNone(candidates[0].trade_setup)

    def test_already_dict_trade_setup_passes_through(self):
        """Some downstream-of-Django ingest pre-parses the JSON; accept both
        forms so callers don't need to know which shape they're getting."""
        d = dt.date(2026, 5, 28)
        # to_parquet with a dict column converts to struct; emulate the case
        # by writing JSON then reading and checking the dict form survives.
        _write_brief(
            self.tmpdir,
            d,
            [
                {
                    "ticker": "NVDA",
                    "theme": "ai-infra",
                    "verified": True,
                    "brief_trade_setup": json.dumps(_sample_setup()),
                }
            ],
        )
        candidates = load_brief(d, self.tmpdir)
        self.assertIsInstance(candidates[0].trade_setup, dict)

    def test_missing_parquet_raises(self):
        d = dt.date(2026, 5, 28)
        with self.assertRaises(FileNotFoundError):
            load_brief(d, self.tmpdir)

    def test_parquet_without_ticker_column_raises(self):
        d = dt.date(2026, 5, 28)
        _write_brief(self.tmpdir, d, [{"theme": "x", "verified": True}])
        with self.assertRaises(ValueError):
            load_brief(d, self.tmpdir)

    def test_legacy_rows_without_n_gates_default_to_zero(self):
        """Pre-2024 / pre-verification parquets don't have all the optional
        feature columns. The loader tolerates that — we still want the row."""
        d = dt.date(2024, 1, 1)
        _write_brief(self.tmpdir, d, [{"ticker": "FOO", "theme": "legacy"}])
        candidates = load_brief(d, self.tmpdir)
        self.assertEqual(candidates[0].n_gates_passed, 0)
        self.assertEqual(candidates[0].n_gates_failed, 0)
        self.assertIsNone(candidates[0].layer4_weighted_score)

    def test_scorer_config_version_populated_when_present(self):
        """Rows that carry scorer_config_version pass the value through."""
        d = dt.date(2026, 6, 25)
        _write_brief(
            self.tmpdir,
            d,
            [
                {
                    "ticker": "AAPL",
                    "theme": "tech",
                    "verified": True,
                    "scorer_config_version": "scorer-v2-oneil-r",
                }
            ],
        )
        candidates = load_brief(d, self.tmpdir)
        self.assertEqual(candidates[0].scorer_config_version, "scorer-v2-oneil-r")

    def test_scorer_config_version_defaults_to_empty_string_when_absent(self):
        """Legacy rows without scorer_config_version yield an empty string."""
        d = dt.date(2026, 6, 25)
        _write_brief(
            self.tmpdir,
            d,
            [{"ticker": "MSFT", "theme": "cloud"}],
        )
        candidates = load_brief(d, self.tmpdir)
        self.assertEqual(candidates[0].scorer_config_version, "")

    def test_scorer_config_version_nan_cell_coerces_to_empty_string(self):
        """A present-but-NaN scorer_config_version cell (e.g. float NaN from
        pandas when the column exists but the row has no value) must coerce to
        ``""`` rather than the string ``"nan"`` that the old ``str(...) or ""``
        expression would produce for a non-NaN float, or the unexpected ``"nan"``
        string that ``str(float('nan'))`` gives."""
        import numpy as np

        d = dt.date(2026, 6, 25)
        # Write a DataFrame that has the column but with a NaN value for this row.
        path = self.tmpdir / f"{d.isoformat()}.parquet"
        pd.DataFrame(
            [{"ticker": "TSLA", "theme": "ev", "scorer_config_version": np.nan}]
        ).to_parquet(path, index=False)
        candidates = load_brief(d, self.tmpdir)
        self.assertEqual(candidates[0].scorer_config_version, "")


if __name__ == "__main__":
    unittest.main()
