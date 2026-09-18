"""The generator behind the frozen pre-open setups (#1494 step 2).

The artefact it writes is committed, so what matters here is that the script refuses the
cases it must not guess at, and that it reads the price frame the way the pipeline did on
the day — cache-only and cut at the as-of.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "apps/alphalens-research/scripts"))

from build_pre_open_setups import (  # noqa: E402
    BuildError,
    _cache_loader,
    stored_order_ttl_days,
    stored_tickers,
)

ASOF = dt.date(2026, 6, 4)


def _write_brief(briefs_dir: Path, ttls: list[int | None]) -> None:
    briefs_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, ttl in enumerate(ttls):
        setup = None if ttl is None else json.dumps({"status": "OK", "order_ttl_days": ttl})
        rows.append({"ticker": f"T{index}", "brief_trade_setup": setup})
    pd.DataFrame(rows).to_parquet(briefs_dir / f"{ASOF.isoformat()}.parquet")


class TheOrderTtlComesFromTheDateItselfTest(unittest.TestCase):
    def test_a_date_that_agrees_with_itself_answers_that_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_brief(Path(tmp), [10, 10, 10])
            self.assertEqual(stored_order_ttl_days(ASOF, Path(tmp)), 10)

    def test_a_row_without_a_setup_does_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_brief(Path(tmp), [7, None])
            self.assertEqual(stored_order_ttl_days(ASOF, Path(tmp)), 7)

    def test_a_date_that_disagrees_with_itself_is_refused(self) -> None:
        # Picking one would put the rebuilt row under a different ladder config token
        # from its own date-mates.
        with tempfile.TemporaryDirectory() as tmp:
            _write_brief(Path(tmp), [7, 10])
            with self.assertRaises(BuildError):
                stored_order_ttl_days(ASOF, Path(tmp))

    def test_a_date_with_no_setup_at_all_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_brief(Path(tmp), [None])
            with self.assertRaises(BuildError):
                stored_order_ttl_days(ASOF, Path(tmp))

    def test_a_missing_brief_is_refused_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BuildError):
                stored_order_ttl_days(ASOF, Path(tmp))


class TheStoredTickersAreUpperCasedTest(unittest.TestCase):
    def test_it_reads_the_dates_own_brief(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_brief(Path(tmp), [7, 7])
            self.assertEqual(stored_tickers(ASOF, Path(tmp)), {"T0", "T1"})


class TheLoaderReadsTheFrameTheBriefSawTest(unittest.TestCase):
    def _write_frame(self, ohlcv_dir: Path) -> None:
        ohlcv_dir.mkdir(parents=True, exist_ok=True)
        index = pd.date_range("2026-06-01", "2026-06-10", freq="D")
        pd.DataFrame(
            {
                "open": range(len(index)),
                "high": range(len(index)),
                "low": range(len(index)),
                "close": range(len(index)),
                "volume": range(len(index)),
            },
            index=index,
        ).to_parquet(ohlcv_dir / f"AAA_{ASOF.isoformat()}.parquet")

    def test_it_cuts_the_frame_at_the_asof(self) -> None:
        # A bar after the as-of would be information the brief could not have had.
        with tempfile.TemporaryDirectory() as tmp:
            self._write_frame(Path(tmp))
            frame = _cache_loader(Path(tmp))("AAA", ASOF)
            self.assertEqual(frame.index.max(), pd.Timestamp(ASOF))

    def test_a_miss_yields_an_empty_frame_and_never_a_network_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(_cache_loader(Path(tmp))("NOSUCH", ASOF).empty)

    def test_it_keys_on_the_asof_not_the_newest_file(self) -> None:
        # The builder must not fall back to a neighbouring date's frame: that would
        # silently rebuild a setup from prices of another day.
        with tempfile.TemporaryDirectory() as tmp:
            self._write_frame(Path(tmp))
            self.assertTrue(_cache_loader(Path(tmp))("AAA", dt.date(2026, 6, 5)).empty)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
