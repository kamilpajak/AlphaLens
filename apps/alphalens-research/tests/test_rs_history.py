"""The persistent RS history store + the trailing-return percentile read.

Round-trip write/read, tri-state None sparsity, the PIT-intersection percentile, the
store-separation invariant (RS root != monitor root, default fetch forces adjusted=True),
and the disk-only guarantee (rs_percentile never fetches).
"""

from __future__ import annotations

import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alphalens_pipeline.data import rs_history
from alphalens_pipeline.data.rs_history import (
    DEFAULT_RS_HISTORY_ROOT,
    read_grouped_day,
    rs_percentile,
    write_grouped_day_atomic,
)

ASOF = dt.date(2026, 6, 12)
# n_sessions_before(2026-06-12, 252) == 2025-06-11 (verified live).
LOOKBACK = dt.date(2025, 6, 11)


def _bar(c: float) -> dict:
    return {"t": 0, "o": c, "h": c, "l": c, "c": c, "v": 1000, "vw": c}


class TestRoundTrip(unittest.TestCase):
    def test_write_then_read(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = {"AAA": _bar(110.0), "BBB": _bar(200.0)}
            write_grouped_day_atomic(root, ASOF, payload)
            got = read_grouped_day(root, ASOF)
            assert got is not None
            self.assertEqual(got["AAA"]["c"], 110.0)
            self.assertEqual(set(got.keys()), {"AAA", "BBB"})

    def test_read_missing_is_none(self):
        with TemporaryDirectory() as tmp:
            self.assertIsNone(read_grouped_day(Path(tmp), ASOF))

    def test_uppercases_symbol(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_grouped_day_atomic(root, ASOF, {"aaa": _bar(1.0)})
            got = read_grouped_day(root, ASOF)
            assert got is not None
            self.assertIn("AAA", got)


class TestRsPercentile(unittest.TestCase):
    def _seed(self, root: Path) -> None:
        # returns: AAA +10%, BBB +100%, CCC -50%
        write_grouped_day_atomic(
            root, ASOF, {"AAA": _bar(110.0), "BBB": _bar(200.0), "CCC": _bar(50.0)}
        )
        write_grouped_day_atomic(
            root, LOOKBACK, {"AAA": _bar(100.0), "BBB": _bar(100.0), "CCC": _bar(100.0)}
        )

    def test_ranks_within_universe(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._seed(root)
            # AAA +10%: 2 of 3 returns <= 0.10 (AAA, CCC) -> 66.67
            self.assertAlmostEqual(rs_percentile(root, "AAA", ASOF), 100.0 * 2 / 3)
            # BBB +100%: top -> 100.0
            self.assertAlmostEqual(rs_percentile(root, "BBB", ASOF), 100.0)
            # CCC -50%: bottom -> 33.33
            self.assertAlmostEqual(rs_percentile(root, "CCC", ASOF), 100.0 * 1 / 3)

    def test_none_when_lookback_snapshot_absent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_grouped_day_atomic(root, ASOF, {"AAA": _bar(110.0), "BBB": _bar(200.0)})
            # no lookback file on disk -> tri-state None, never a fake 0.0
            self.assertIsNone(rs_percentile(root, "AAA", ASOF))

    def test_none_when_candidate_absent_from_an_endpoint(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._seed(root)
            # DDD did not trade -> not in either snapshot -> None
            self.assertIsNone(rs_percentile(root, "DDD", ASOF))

    def test_non_positive_close_excluded(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_grouped_day_atomic(root, ASOF, {"AAA": _bar(110.0), "ZZZ": _bar(0.0)})
            write_grouped_day_atomic(root, LOOKBACK, {"AAA": _bar(100.0), "ZZZ": _bar(0.0)})
            # ZZZ (close 0) excluded from the universe AND not rankable itself.
            self.assertAlmostEqual(rs_percentile(root, "AAA", ASOF), 100.0)  # only AAA in universe
            self.assertIsNone(rs_percentile(root, "ZZZ", ASOF))

    def test_pit_intersection_universe(self):
        # A name present at asof but NOT at lookback (recent IPO) is excluded from the
        # universe denominator (delisting-survivorship-clean by construction).
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_grouped_day_atomic(root, ASOF, {"AAA": _bar(110.0), "IPO": _bar(999.0)})
            write_grouped_day_atomic(root, LOOKBACK, {"AAA": _bar(100.0)})
            # universe = {AAA} only (IPO absent at lookback); AAA alone -> 100.0
            self.assertAlmostEqual(rs_percentile(root, "AAA", ASOF), 100.0)
            self.assertIsNone(rs_percentile(root, "IPO", ASOF))  # IPO not in intersection


class TestStoreSeparationAndDiskOnly(unittest.TestCase):
    def test_rs_root_differs_from_monitor_root(self):
        # The RS store (adjusted=True) MUST live at a different root than the
        # population-monitor grouped cache (adjusted=False) — a refactor must not collapse them.
        monitor_root = Path.home() / ".alphalens" / "population_ladders" / "grouped"
        self.assertNotEqual(DEFAULT_RS_HISTORY_ROOT, monitor_root)
        self.assertEqual(
            DEFAULT_RS_HISTORY_ROOT, Path.home() / ".alphalens" / "grouped_daily_history"
        )

    def test_default_fetch_forces_adjusted_true(self):
        # The production fetch must request SPLIT-ADJUSTED bars (else returns carry split jumps).
        captured: dict[str, object] = {}

        class _SpyClient:
            def get_grouped_daily(self, date, *, adjusted=False, **kw):
                captured["adjusted"] = adjusted
                return {"AAA": _bar(1.0)}

        import alphalens_pipeline.data.alt_data.polygon_client as pc

        def _fake_default():
            return _SpyClient()

        orig = pc.get_default_polygon_client
        pc.get_default_polygon_client = _fake_default  # type: ignore[assignment]
        try:
            rs_history._default_grouped_fetch(ASOF)
            self.assertIs(captured["adjusted"], True)
        finally:
            pc.get_default_polygon_client = orig

    def test_rs_percentile_never_fetches(self):
        # The score-stage read path must touch ONLY the disk store — a store miss is a
        # clean None, NEVER a live fetch (the no-in-pass-Polygon guarantee).
        import alphalens_pipeline.data.alt_data.polygon_client as pc

        def _boom():
            raise AssertionError("rs_percentile must not call the Polygon client")

        orig = pc.get_default_polygon_client
        pc.get_default_polygon_client = _boom  # type: ignore[assignment]
        try:
            with TemporaryDirectory() as tmp:
                self.assertIsNone(
                    rs_percentile(Path(tmp), "AAA", ASOF)
                )  # empty store -> None, no fetch
        finally:
            pc.get_default_polygon_client = orig


if __name__ == "__main__":
    unittest.main()
