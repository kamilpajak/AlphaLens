"""Unit tests for the sector-relative EDGE outcome (PR-2b, D4 decoupling).

``sector_excess_return = forward_return − sector_etf_window_return`` measures a
candidate against ITS OWN SPDR sector ETF over the SAME window as the SPY
benchmark-excess — a different series from the SPY-derived market_state label
(memo §4.2). Mirrors ``test_benchmark_excess``: fake bar_fetch + patched sector
resolution, no network.
"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

_ASOF_LAST_CLOSED = dt.date(2026, 7, 1)


def _bars_factory(reference: float, last_close: float, *, counter: list | None = None):
    """A bar_fetch returning two bars anchored on the fetch's ``start`` (arrival)."""

    def fetch(ticker, start, end):
        if counter is not None:
            counter.append(ticker)
        return [
            {"t": int(start.timestamp() * 1000), "c": reference, "v": 1000},
            {
                "t": int((start + dt.timedelta(minutes=470)).timestamp() * 1000),
                "c": last_close,
                "v": 1000,
            },
        ]

    return fetch


def _grouped(closes: dict):
    """A grouped-daily stub: session -> {TICKER: {"c": close}}; empty for unknown sessions."""
    return lambda session: {t: {"c": c} for t, c in closes.get(session, {}).items()}


_ETFS = ("XLK", "XLE")


def _etf_closes(close: float):
    """Grouped-daily stub: every sector ETF's official close is ``close`` on EVERY session."""
    return lambda _session: {etf: {"c": close} for etf in _ETFS}


def _row(
    ticker="NVDA",
    *,
    forward_return: float | None = 0.15,
    brief_date="2026-06-11",
    matured_at="2026-06-25",
):
    return {
        "ticker": ticker,
        "forward_return": forward_return,
        "brief_date": brief_date,
        "matured_at": matured_at,
    }


class TestComputeSectorExcessForRow(unittest.TestCase):
    def test_resolved_sector_returns_etf_window_and_excess(self):
        from alphalens_pipeline.feedback import sector_excess

        # window return = (110 − 100) / 100 = 0.10; excess = 0.15 − 0.10 = 0.05
        fetch = _bars_factory(100.0, 110.0)
        with patch.object(sector_excess, "sector_etf_for_ticker", return_value="XLK"):
            etf, wret, excess = sector_excess.compute_sector_excess_for_row(
                _row(),
                bar_fetch=fetch,
                exit_close_of=lambda _t, _s: 110.0,
                last_closed_session=_ASOF_LAST_CLOSED,
            )

        self.assertEqual(etf, "XLK")
        self.assertAlmostEqual(wret, 0.10, places=6)
        self.assertAlmostEqual(excess, 0.05, places=6)

    def test_unresolvable_sector_returns_all_none(self):
        from alphalens_pipeline.feedback import sector_excess

        fetch = _bars_factory(100.0, 110.0)
        with patch.object(sector_excess, "sector_etf_for_ticker", return_value=None):
            result = sector_excess.compute_sector_excess_for_row(
                _row("ZZZZ"),
                bar_fetch=fetch,
                exit_close_of=lambda _t, _s: 110.0,
                last_closed_session=_ASOF_LAST_CLOSED,
            )

        self.assertEqual(result, (None, None, None))

    def test_missing_forward_return_keeps_etf_but_null_metric(self):
        from alphalens_pipeline.feedback import sector_excess

        fetch = _bars_factory(100.0, 110.0)
        with patch.object(sector_excess, "sector_etf_for_ticker", return_value="XLK"):
            etf, wret, excess = sector_excess.compute_sector_excess_for_row(
                _row(forward_return=None),
                bar_fetch=fetch,
                exit_close_of=lambda _t, _s: 110.0,
                last_closed_session=_ASOF_LAST_CLOSED,
            )

        self.assertEqual(etf, "XLK")
        self.assertIsNone(wret)
        self.assertIsNone(excess)

    def test_never_falls_back_to_spy(self):
        from alphalens_pipeline.feedback import sector_excess

        # A row whose sector is unresolvable must NEVER be benchmarked against SPY.
        fetch = _bars_factory(100.0, 999.0)  # would give a huge excess if used
        with patch.object(sector_excess, "sector_etf_for_ticker", return_value=None):
            _etf, _wret, excess = sector_excess.compute_sector_excess_for_row(
                _row("ZZZZ"),
                bar_fetch=fetch,
                exit_close_of=lambda _t, _s: 999.0,
                last_closed_session=_ASOF_LAST_CLOSED,
            )

        self.assertIsNone(excess)


class TestEnrichStoreSectorExcess(unittest.TestCase):
    def _write(self, root: Path, rows: list[dict]):
        pd.DataFrame(rows).to_parquet(root / "2026-06-11.parquet", index=False)

    def _sector_map(self, ticker):
        return {"NVDA": "XLK", "AAPL": "XLK"}.get(ticker)  # ZZZZ → None

    def test_enrich_stamps_four_columns_and_excludes_unresolvable(self):
        from alphalens_pipeline.feedback import sector_excess

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write(root, [_row("NVDA"), _row("ZZZZ")])
            fetch = _bars_factory(100.0, 110.0)
            with patch.object(sector_excess, "sector_etf_for_ticker", side_effect=self._sector_map):
                n = sector_excess.enrich_store_with_sector_excess(
                    root,
                    bar_fetch=fetch,
                    grouped_fetch=_etf_closes(110.0),
                    now=dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
                )

            out = pd.read_parquet(root / "2026-06-11.parquet")
            for col in sector_excess.SECTOR_EXCESS_COLUMNS:
                self.assertIn(col, out.columns)
            nvda = out[out["ticker"] == "NVDA"].iloc[0]
            self.assertEqual(nvda["sector_etf_ticker"], "XLK")
            self.assertAlmostEqual(nvda["sector_excess_return"], 0.05, places=6)
            zzzz = out[out["ticker"] == "ZZZZ"].iloc[0]
            self.assertTrue(pd.isna(zzzz["sector_excess_return"]))
            # version is stamped on EVERY row (poolability key)
            self.assertTrue(
                (out["outcome_benchmark_version"] == sector_excess.OUTCOME_BENCHMARK_VERSION).all()
            )
            self.assertEqual(n, 1)  # only NVDA got a non-null excess

    def test_shared_sector_window_is_fetched_once(self):
        from alphalens_pipeline.feedback import sector_excess

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write(root, [_row("NVDA"), _row("AAPL")])  # both → XLK, same window
            calls: list[str] = []
            fetch = _bars_factory(100.0, 110.0, counter=calls)
            with patch.object(sector_excess, "sector_etf_for_ticker", side_effect=self._sector_map):
                sector_excess.enrich_store_with_sector_excess(
                    root,
                    bar_fetch=fetch,
                    grouped_fetch=_etf_closes(110.0),
                    now=dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
                )

            self.assertEqual(calls, ["XLK"])  # memoized: one fetch for the shared window

    def test_idempotent(self):
        from alphalens_pipeline.feedback import sector_excess

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write(root, [_row("NVDA")])
            fetch = _bars_factory(100.0, 110.0)
            with patch.object(sector_excess, "sector_etf_for_ticker", side_effect=self._sector_map):
                sector_excess.enrich_store_with_sector_excess(
                    root,
                    bar_fetch=fetch,
                    grouped_fetch=_etf_closes(110.0),
                    now=dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
                )
                first = pd.read_parquet(root / "2026-06-11.parquet")[
                    "sector_excess_return"
                ].tolist()
                sector_excess.enrich_store_with_sector_excess(
                    root,
                    bar_fetch=fetch,
                    grouped_fetch=_etf_closes(110.0),
                    now=dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
                )
                second = pd.read_parquet(root / "2026-06-11.parquet")[
                    "sector_excess_return"
                ].tolist()

            self.assertEqual(first, second)


class _CountingDeadline:
    """``should_stop()`` answers False for the first ``allow`` calls, then True."""

    def __init__(self, allow: int):
        self.allow = allow
        self.calls = 0

    def should_stop(self) -> bool:
        self.calls += 1
        return self.calls > self.allow


def _settled_row(ticker="NVDA", *, etf="XLK", matured_at="2026-06-25", **overrides):
    from alphalens_pipeline.feedback.sector_excess import OUTCOME_BENCHMARK_VERSION

    row = _row(ticker, matured_at=matured_at)
    row.update(
        {
            "sector_etf_ticker": etf,
            "sector_etf_window_return": 0.10,
            "sector_excess_return": 0.05,  # == 0.15 - 0.10: arithmetically consistent
            "sector_window_exit": matured_at,
            "outcome_benchmark_version": OUTCOME_BENCHMARK_VERSION,
        }
    )
    row.update(overrides)
    return row


class TestSectorReuseFirst(unittest.TestCase):
    """A settled terminal row is carried with no fetch; anything that could make the
    stored pair wrong (moved window, map version, a ticker whose ETF changed under
    the same version) makes it a gap again (#1435)."""

    _NOW = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)

    def _run(
        self,
        root: Path,
        rows: list[dict],
        *,
        fetch,
        resolver,
        deadline=None,
        path="2026-06-11.parquet",
        close=120.0,
    ):
        from alphalens_pipeline.feedback import sector_excess

        pd.DataFrame(rows).to_parquet(root / path, index=False)
        with patch.object(sector_excess, "sector_etf_for_ticker", side_effect=resolver):
            return sector_excess.enrich_store_with_sector_excess(
                root,
                bar_fetch=fetch,
                grouped_fetch=_etf_closes(close),
                now=self._NOW,
                deadline=deadline,
            )

    @staticmethod
    def _xlk(_ticker):
        return "XLK"

    def test_settled_row_is_reused_without_a_fetch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            calls: list[str] = []
            self._run(
                root,
                [_settled_row()],
                fetch=_bars_factory(100.0, 120.0, counter=calls),
                resolver=self._xlk,
            )
            out = pd.read_parquet(root / "2026-06-11.parquet").iloc[0]
            self.assertEqual(calls, [])
            self.assertAlmostEqual(out["sector_etf_window_return"], 0.10, places=9)
            self.assertAlmostEqual(out["sector_excess_return"], 0.05, places=9)

    def test_pair_whose_window_stamp_moved_is_recomputed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            calls: list[str] = []
            self._run(
                root,
                [_settled_row(sector_window_exit="2026-09-10")],
                fetch=_bars_factory(100.0, 120.0, counter=calls),
                resolver=self._xlk,
            )
            out = pd.read_parquet(root / "2026-06-11.parquet").iloc[0]
            self.assertEqual(calls, ["XLK"])
            self.assertAlmostEqual(out["sector_etf_window_return"], 0.20, places=9)
            self.assertEqual(out["sector_window_exit"], "2026-06-25")

    def test_pair_from_another_map_version_is_recomputed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            calls: list[str] = []
            self._run(
                root,
                [_settled_row(outcome_benchmark_version="sector-etf-v0-old")],
                fetch=_bars_factory(100.0, 120.0, counter=calls),
                resolver=self._xlk,
            )
            self.assertEqual(calls, ["XLK"])

    def test_pair_without_a_window_stamp_is_recomputed(self):
        # The shape of the store after the #1444 repair: pair nulled, no stamp column.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            calls: list[str] = []
            row = _settled_row()
            del row["sector_window_exit"]
            self._run(
                root, [row], fetch=_bars_factory(100.0, 120.0, counter=calls), resolver=self._xlk
            )
            self.assertEqual(calls, ["XLK"])
            self.assertEqual(
                pd.read_parquet(root / "2026-06-11.parquet").iloc[0]["sector_window_exit"],
                "2026-06-25",
            )

    def test_ticker_whose_etf_changed_under_the_same_version_is_recomputed_against_the_new_etf(
        self,
    ):
        # sic_index.parquet is refreshed by hand with SECTOR_ETF_MAP_VERSION unchanged, so a
        # ticker can move sector; the stored XLK pair must not be frozen under an XLE label.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            calls: list[str] = []
            self._run(
                root,
                [_settled_row(etf="XLK")],
                fetch=_bars_factory(100.0, 120.0, counter=calls),
                resolver=lambda _t: "XLE",
            )
            out = pd.read_parquet(root / "2026-06-11.parquet").iloc[0]
            self.assertEqual(calls, ["XLE"])
            self.assertEqual(out["sector_etf_ticker"], "XLE")
            self.assertAlmostEqual(out["sector_etf_window_return"], 0.20, places=9)
            self.assertAlmostEqual(out["sector_excess_return"], 0.15 - 0.20, places=9)

    def test_ongoing_row_gets_its_etf_and_no_pair_and_no_fetch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            calls: list[str] = []
            stale = _settled_row(
                matured_at=None
            )  # an ongoing row that carried a pair from the old pass
            stale["sector_window_exit"] = None
            self._run(
                root, [stale], fetch=_bars_factory(100.0, 120.0, counter=calls), resolver=self._xlk
            )
            out = pd.read_parquet(root / "2026-06-11.parquet").iloc[0]
            self.assertEqual(calls, [])
            self.assertEqual(out["sector_etf_ticker"], "XLK")
            self.assertTrue(pd.isna(out["sector_etf_window_return"]))
            self.assertTrue(pd.isna(out["sector_excess_return"]))

    def test_gap_row_is_served_from_a_settled_sibling_with_the_same_window(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            calls: list[str] = []
            gap = _row("AAPL")  # same brief, same matured_at, same ETF -> same (etf, arrival, exit)
            self._run(
                root,
                [_settled_row("NVDA"), gap],
                fetch=_bars_factory(100.0, 120.0, counter=calls),
                resolver=self._xlk,
            )
            out = pd.read_parquet(root / "2026-06-11.parquet").set_index("ticker")
            self.assertEqual(calls, [])
            self.assertAlmostEqual(out.loc["AAPL", "sector_etf_window_return"], 0.10, places=9)
            self.assertAlmostEqual(out.loc["AAPL", "sector_excess_return"], 0.05, places=9)
            self.assertEqual(out.loc["AAPL", "sector_window_exit"], "2026-06-25")

    def test_a_row_without_forward_return_does_not_deny_its_sibling_a_fetch(self):
        # A terminal row with no candidate leg computes nothing; it must not leave a
        # None in the per-run window cache that a sibling of the same window would
        # then read as "already tried" (review finding on #1435).
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            calls: list[str] = []
            no_leg = _row("NVDA", forward_return=None)
            sibling = _row("AAPL")
            self._run(
                root,
                [no_leg, sibling],
                fetch=_bars_factory(100.0, 110.0, counter=calls),
                resolver=self._xlk,
                close=110.0,
            )
            out = pd.read_parquet(root / "2026-06-11.parquet").set_index("ticker")
            self.assertEqual(calls, ["XLK"])
            self.assertTrue(pd.isna(out.loc["NVDA", "sector_excess_return"]))
            self.assertAlmostEqual(out.loc["AAPL", "sector_excess_return"], 0.05, places=9)
            self.assertEqual(out.loc["AAPL", "sector_window_exit"], "2026-06-25")

    def test_event_lane_row_is_windowed_from_the_ladder_arrival(self):
        from alphalens_pipeline.feedback.ladder_config import ladder_arrival_session
        from alphalens_pipeline.paper.calendar import session_open_utc

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            starts: list[dt.datetime] = []

            def fetch(ticker, start, end):
                starts.append(start)
                return _bars_factory(100.0, 110.0)(ticker, start, end)

            row = _row("NVDA")
            row["source"] = "insider_cluster"
            self._run(root, [row], fetch=fetch, resolver=self._xlk, close=110.0)
            expected = session_open_utc(ladder_arrival_session(dt.date(2026, 6, 11)), "XNYS")
            self.assertEqual(starts, [expected])


class TestSectorSweepOrderAndWrites(unittest.TestCase):
    _NOW = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)

    @staticmethod
    def _xlk(_ticker):
        return "XLK"

    def _enrich(self, root, *, fetch, deadline=None):
        from alphalens_pipeline.feedback import sector_excess

        with patch.object(sector_excess, "sector_etf_for_ticker", side_effect=self._xlk):
            return sector_excess.enrich_store_with_sector_excess(
                root,
                bar_fetch=fetch,
                grouped_fetch=_etf_closes(110.0),
                now=self._NOW,
                deadline=deadline,
            )

    def test_newest_file_is_written_before_the_deadline_reaches_the_older_one(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            older, newer = root / "2026-06-11.parquet", root / "2026-06-12.parquet"
            pd.DataFrame([_row("NVDA", brief_date="2026-06-11")]).to_parquet(older, index=False)
            pd.DataFrame([_row("AAPL", brief_date="2026-06-12")]).to_parquet(newer, index=False)
            older_mtime = older.stat().st_mtime_ns

            n = self._enrich(
                root, fetch=_bars_factory(100.0, 110.0), deadline=_CountingDeadline(allow=1)
            )

            self.assertEqual(n, 1)
            self.assertAlmostEqual(
                pd.read_parquet(newer).iloc[0]["sector_excess_return"], 0.05, places=9
            )
            self.assertEqual(older.stat().st_mtime_ns, older_mtime)
            self.assertNotIn("sector_excess_return", pd.read_parquet(older).columns)

    def test_mid_file_trip_leaves_the_file_untouched_and_uncounted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = root / "2026-06-11.parquet"
            pd.DataFrame([_row("NVDA"), _row("AAPL")]).to_parquet(path, index=False)
            mtime = path.stat().st_mtime_ns

            n = self._enrich(
                root, fetch=_bars_factory(100.0, 110.0), deadline=_CountingDeadline(allow=1)
            )

            self.assertEqual(n, 0)
            self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_all_reused_file_is_not_rewritten_and_the_second_run_fetches_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = root / "2026-06-11.parquet"
            pd.DataFrame([_row("NVDA")]).to_parquet(path, index=False)  # no sector columns at all
            first: list[str] = []
            self._enrich(root, fetch=_bars_factory(100.0, 110.0, counter=first))
            self.assertEqual(first, ["XLK"], "precondition: the first run creates the columns")
            self.assertIn("sector_window_exit", pd.read_parquet(path).columns)
            mtime = path.stat().st_mtime_ns

            second: list[str] = []
            with self.assertLogs("alphalens_pipeline.feedback.sector_excess", level="INFO") as logs:
                n = self._enrich(root, fetch=_bars_factory(100.0, 110.0, counter=second))

            self.assertEqual(second, [])
            self.assertEqual(n, 1)
            self.assertEqual(path.stat().st_mtime_ns, mtime)
            self.assertTrue(
                any(
                    "sector-excess: enriched 1 (reused 1, fetched 0, stopped_early=False)" in line
                    for line in logs.output
                ),
                logs.output,
            )

    def test_log_line_reports_a_deadline_stop(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pd.DataFrame([_row("NVDA")]).to_parquet(root / "2026-06-11.parquet", index=False)
            with self.assertLogs("alphalens_pipeline.feedback.sector_excess", level="INFO") as logs:
                self._enrich(
                    root, fetch=_bars_factory(100.0, 110.0), deadline=_CountingDeadline(allow=0)
                )
            self.assertTrue(any("stopped_early=True" in line for line in logs.output), logs.output)


class TestSplitInvalidatedRowsGetNoSectorPair(unittest.TestCase):
    """A ``SPLIT_INVALIDATED`` quarantine keeps its ETF label and gets no pair —
    not computed, not reused, not seeded into the per-run cache (#1452)."""

    _NOW = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    _PATH = "2026-06-11.parquet"

    @staticmethod
    def _xlk(_ticker):
        return "XLK"

    @staticmethod
    def _quarantined(**overrides):
        return _settled_row("MQ", ladder_classification="SPLIT_INVALIDATED", **overrides)

    def _run(self, root: Path, rows: list[dict], *, counter: list[str], write: bool = True):
        from alphalens_pipeline.feedback import sector_excess

        if write:
            pd.DataFrame(rows).to_parquet(root / self._PATH, index=False)
        with patch.object(sector_excess, "sector_etf_for_ticker", side_effect=self._xlk):
            return sector_excess.enrich_store_with_sector_excess(
                root,
                bar_fetch=_bars_factory(100.0, 110.0, counter=counter),
                grouped_fetch=_etf_closes(110.0),
                now=self._NOW,
            )

    def test_a_settled_quarantined_pair_is_nulled_not_reused(self):
        from alphalens_pipeline.feedback.sector_excess import OUTCOME_BENCHMARK_VERSION

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            calls: list[str] = []
            n = self._run(root, [self._quarantined()], counter=calls)
            out = pd.read_parquet(root / self._PATH).iloc[0]
            self.assertEqual(calls, [])
            self.assertEqual(n, 0)
            self.assertEqual(out["sector_etf_ticker"], "XLK")
            self.assertTrue(pd.isna(out["sector_etf_window_return"]))
            self.assertTrue(pd.isna(out["sector_excess_return"]))
            self.assertTrue(pd.isna(out["sector_window_exit"]))
            self.assertEqual(out["outcome_benchmark_version"], OUTCOME_BENCHMARK_VERSION)

    def test_a_quarantined_gap_is_not_fetched(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            calls: list[str] = []
            self._run(
                root,
                [
                    self._quarantined(
                        sector_etf_window_return=None,
                        sector_excess_return=None,
                        sector_window_exit=None,
                    )
                ],
                counter=calls,
            )
            out = pd.read_parquet(root / self._PATH).iloc[0]
            self.assertEqual(calls, [])
            self.assertTrue(pd.isna(out["sector_excess_return"]))

    def test_a_gap_sibling_in_the_same_window_pays_its_own_fetch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            calls: list[str] = []
            gap = _row("NVDA")  # same brief_date + matured_at as the quarantine → same window
            self._run(root, [self._quarantined(), gap], counter=calls)
            df = pd.read_parquet(root / self._PATH).set_index("ticker")
            self.assertEqual(calls, ["XLK"])
            self.assertAlmostEqual(df.loc["NVDA", "sector_excess_return"], 0.05, places=9)
            self.assertTrue(pd.isna(df.loc["MQ", "sector_excess_return"]))

    def test_the_second_run_over_a_nulled_quarantine_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._run(root, [self._quarantined()], counter=[])
            path = root / self._PATH
            mtime = path.stat().st_mtime_ns

            calls: list[str] = []
            with self.assertLogs("alphalens_pipeline.feedback.sector_excess", level="INFO") as logs:
                self._run(root, [], counter=calls, write=False)

            self.assertEqual(calls, [])
            self.assertEqual(path.stat().st_mtime_ns, mtime)
            self.assertTrue(
                any(
                    "sector-excess: enriched 0 (reused 0, fetched 0, stopped_early=False)" in line
                    for line in logs.output
                ),
                logs.output,
            )


class TestOfficialCloseSectorLeg(unittest.TestCase):
    """The sector leg ends at the ETF's OFFICIAL close of the exit session (#1445):
    one anchor fetch per (ETF, arrival), the close from the grouped cache, and a
    v1 (last-bar) pair is recomputed under the v2 poolability key."""

    _NOW = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    _PATH = "2026-06-11.parquet"

    @staticmethod
    def _xlk(_ticker):
        return "XLK"

    def _run(self, root: Path, rows: list[dict], *, closes: dict, counter: list[str]):
        from alphalens_pipeline.feedback import sector_excess

        pd.DataFrame(rows).to_parquet(root / self._PATH, index=False)
        with patch.object(sector_excess, "sector_etf_for_ticker", side_effect=self._xlk):
            return sector_excess.enrich_store_with_sector_excess(
                root,
                bar_fetch=_bars_factory(100.0, 999.0, counter=counter),
                grouped_fetch=_grouped(closes),
                now=self._NOW,
            )

    def test_exit_leg_is_the_official_close_and_the_version_is_v2(self):
        from alphalens_pipeline.feedback.sector_excess import OUTCOME_BENCHMARK_VERSION

        self.assertTrue(OUTCOME_BENCHMARK_VERSION.startswith("sector-etf-v2-"))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            calls: list[str] = []
            self._run(
                root, [_row("NVDA")], closes={dt.date(2026, 6, 25): {"XLK": 110.0}}, counter=calls
            )
            out = pd.read_parquet(root / self._PATH).iloc[0]
            self.assertEqual(calls, ["XLK"])
            self.assertAlmostEqual(out["sector_etf_window_return"], 0.10, places=9)
            self.assertAlmostEqual(out["sector_excess_return"], 0.05, places=9)
            self.assertEqual(out["outcome_benchmark_version"], OUTCOME_BENCHMARK_VERSION)

    def test_a_v1_pair_is_recomputed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            calls: list[str] = []
            v1 = _settled_row(outcome_benchmark_version="sector-etf-v1-sic2-spdr-v1")
            self._run(root, [v1], closes={dt.date(2026, 6, 25): {"XLK": 120.0}}, counter=calls)
            out = pd.read_parquet(root / self._PATH).iloc[0]
            self.assertEqual(calls, ["XLK"])
            self.assertAlmostEqual(out["sector_etf_window_return"], 0.20, places=9)

    def test_rows_sharing_an_anchor_with_different_exits_pay_one_fetch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            calls: list[str] = []
            self._run(
                root,
                [_row("NVDA", matured_at="2026-06-25"), _row("AAPL", matured_at="2026-06-26")],
                closes={dt.date(2026, 6, 25): {"XLK": 110.0}, dt.date(2026, 6, 26): {"XLK": 120.0}},
                counter=calls,
            )
            df = pd.read_parquet(root / self._PATH).set_index("ticker")
            self.assertEqual(calls, ["XLK"])
            self.assertAlmostEqual(df.loc["NVDA", "sector_etf_window_return"], 0.10, places=9)
            self.assertAlmostEqual(df.loc["AAPL", "sector_etf_window_return"], 0.20, places=9)

    def test_missing_official_close_leaves_the_pair_none(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._run(root, [_row("NVDA")], closes={}, counter=[])
            out = pd.read_parquet(root / self._PATH).iloc[0]
            self.assertEqual(out["sector_etf_ticker"], "XLK")
            self.assertTrue(pd.isna(out["sector_excess_return"]))
            self.assertTrue(pd.isna(out["sector_window_exit"]))


if __name__ == "__main__":
    unittest.main()
