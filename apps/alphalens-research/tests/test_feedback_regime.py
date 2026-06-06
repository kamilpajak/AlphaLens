"""Tests for the surviving feedback primitive — the VIX-regime helper.

The Track-A user-action click ledger (the SQLite ``Decision`` ``store``) was
removed (#465): no UI, no Django feedback app, no writer, so the ``decisions``
table had no input and the whole store subsystem was removed. The only piece of
``alphalens_feedback`` that stays live is ``regime`` — the pure-stdlib VIX-cache
read + bucket classifier that Django imports for its regime constant.

Design memo: ``docs/research/feedback_ledger_design_2026_05_29.md``.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from alphalens_feedback import regime

UTC = dt.UTC


class TestMarketRegime(unittest.TestCase):
    """Pure VIX bucket classifier — no network."""

    def test_low_below_15(self):
        self.assertEqual(regime.classify_vix(10.0), "low")
        self.assertEqual(regime.classify_vix(14.99), "low")

    def test_mid_15_to_25(self):
        self.assertEqual(regime.classify_vix(15.0), "mid")
        self.assertEqual(regime.classify_vix(20.0), "mid")
        self.assertEqual(regime.classify_vix(24.99), "mid")

    def test_high_at_or_above_25(self):
        self.assertEqual(regime.classify_vix(25.0), "high")
        self.assertEqual(regime.classify_vix(35.0), "high")

    def test_classify_vix_handles_none_as_unknown(self):
        # If the caller couldn't fetch VIX (e.g. weekend, holiday, network
        # blip), the regime label is `unknown` rather than blowing up.
        self.assertEqual(regime.classify_vix(None), "unknown")


class TestVixCacheReader(unittest.TestCase):
    """Hot-path VIX cache reader feeding classify_vix.

    get_cached_vix does ONE local file read, zero network, and degrades to
    None (-> classify_vix returns "unknown") on ANY failure. Staleness is
    measured on ``fetched_at`` with a 96h ceiling.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "vix_regime_cache.json"

    def tearDown(self):
        self._td.cleanup()

    def _write(self, *, vix, fetched_at: dt.datetime, observation_date: str = "2026-05-29"):
        import json

        self.path.write_text(
            json.dumps(
                {
                    "observation_date": observation_date,
                    "vix": vix,
                    "fetched_at": fetched_at.isoformat(),
                    "series": "VIXCLS",
                }
            )
        )

    def test_returns_value_on_fresh_cache(self):
        now = dt.datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
        self._write(vix=18.5, fetched_at=now)
        self.assertEqual(regime.get_cached_vix(self.path, now=now), 18.5)

    def test_missing_file_returns_none(self):
        missing = Path(self._td.name) / "nope.json"
        self.assertIsNone(regime.get_cached_vix(missing))

    def test_stale_beyond_96h_returns_none(self):
        now = dt.datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
        self._write(vix=18.5, fetched_at=now - dt.timedelta(hours=97))
        self.assertIsNone(regime.get_cached_vix(self.path, now=now))

    def test_at_96h_boundary_returns_value(self):
        # Policy is age > 96h -> stale; exactly 96h is still fresh.
        now = dt.datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
        self._write(vix=18.5, fetched_at=now - dt.timedelta(hours=96))
        self.assertEqual(regime.get_cached_vix(self.path, now=now), 18.5)

    def test_within_96h_weekend_returns_value(self):
        now = dt.datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
        self._write(vix=21.0, fetched_at=now - dt.timedelta(hours=70))
        self.assertEqual(regime.get_cached_vix(self.path, now=now), 21.0)

    def test_fresh_fetched_at_old_observation_still_returns_value(self):
        # fetched_at is the SOLE freshness gate: a live refresher re-stamping
        # fetched_at every few hours proves liveness even if FRED's last
        # published observation is several days old (holiday week).
        now = dt.datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
        self._write(vix=19.0, fetched_at=now, observation_date="2026-05-22")
        self.assertEqual(regime.get_cached_vix(self.path, now=now), 19.0)

    def test_malformed_json_returns_none(self):
        self.path.write_text("not json {")
        self.assertIsNone(regime.get_cached_vix(self.path))

    def test_missing_fetched_at_key_returns_none(self):
        import json

        self.path.write_text(json.dumps({"vix": 18.5, "series": "VIXCLS"}))
        self.assertIsNone(regime.get_cached_vix(self.path))

    def test_non_numeric_vix_returns_none(self):
        now = dt.datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
        self._write(vix="not-a-number", fetched_at=now)
        self.assertIsNone(regime.get_cached_vix(self.path, now=now))

    def test_classify_vix_of_cached_value_buckets_correctly(self):
        now = dt.datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
        for vix, expected in ((12.0, "low"), (22.0, "mid"), (30.0, "high")):
            self._write(vix=vix, fetched_at=now)
            self.assertEqual(
                regime.classify_vix(regime.get_cached_vix(self.path, now=now)), expected
            )


if __name__ == "__main__":
    unittest.main()
