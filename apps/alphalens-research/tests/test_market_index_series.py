"""Unit tests for the three raw index 5-session returns (display data).

``market_ret5_{spy,qqq,iwm}`` are raw arithmetic on a public close series —
``close(current) / close(5 sessions before current) - 1`` — with no threshold
and no emission rule, so they carry NO ``config_version`` and sit OUTSIDE
``MARKET_STATE_COLUMNS``. That boundary is the point of the family, so it is
pinned here rather than left to a comment: a test fails if any of the three
ever joins the classifier's tuple, and a test fails if the classifier's frozen
version string moves because of them.

Two discriminators the real store CANNOT provide, so both are synthetic:

* a store with ONE session of the window deleted. On the real grouped store
  SPY/QQQ/IWM are present in every session, so positional ``iloc[-6]`` and the
  calendar-anchored lookback agree on every real date and a test bound to real
  data has zero power to tell them apart (the ``fb_uniform_fixture`` shape);
* a store whose newest session lags ``asof`` by more than the staleness bound.
  Without the bound the enricher silently stamps ONE frozen trio onto every
  date past the store's end, which is the #1524 frozen-VIX defect in another
  dress.
"""

import datetime as dt
import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.market import market_state
from alphalens_pipeline.market.calendar import advance_trading_sessions, n_sessions_before

_TICKERS = ("SPY", "QQQ", "IWM")


def _sessions(anchor: dt.date, n: int) -> list[dt.date]:
    """``n`` real XNYS sessions ending at ``anchor`` (oldest first).

    Derived from the exchange calendar, not ``bdate_range``: a holiday inside
    the window would otherwise put a non-session file on disk and make the
    fixture disagree with the lookback the code computes.
    """
    return [n_sessions_before(anchor, k) for k in range(n - 1, -1, -1)]


def _seed(
    root: Path, sessions, closes: dict[str, list[float]], *, skip: set[dt.date] = frozenset()
):
    """Write one whole-market snapshot per session carrying the three indices.

    ``skip`` omits a session's file entirely — an unhealed store gap.
    """
    from alphalens_pipeline.data.rs_history import write_grouped_day_atomic

    for i, d in enumerate(sessions):
        if d in skip:
            continue
        payload = {}
        for tkr in closes:
            c = closes[tkr][i]
            payload[tkr] = {"t": 0, "o": c, "h": c, "l": c, "c": c, "v": 1000, "vw": c}
        payload["NOISE"] = {"t": 0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1}
        write_grouped_day_atomic(root, d, payload)


# Six sessions is the whole window: close[-1] / close[-6] - 1. The ladders are
# chosen so each ticker's answer is a DIFFERENT, exactly representable number,
# so a transposed column cannot pass.
_ANCHOR = dt.date(2026, 3, 20)
_CLOSES = {
    "SPY": [100.0, 101.0, 102.0, 103.0, 104.0, 99.0],  # 99/100 - 1 = -1%
    "QQQ": [200.0, 201.0, 202.0, 203.0, 204.0, 194.0],  # 194/200 - 1 = -3%
    "IWM": [50.0, 49.0, 48.0, 47.0, 46.0, 51.0],  # 51/50 - 1 = +2%
}
_EXPECTED = {
    "market_ret5_spy": -0.01,
    "market_ret5_qqq": -0.03,
    "market_ret5_iwm": 0.02,
}


class TestFiveSessionReturns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.sessions = _sessions(_ANCHOR, 6)
        _seed(cls.root, cls.sessions, _CLOSES)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_each_return_is_the_close_ratio_over_five_sessions(self):
        from alphalens_pipeline.market.market_state import five_session_index_returns

        got = five_session_index_returns(_ANCHOR, grouped_root=self.root)

        self.assertEqual(sorted(got), sorted(_EXPECTED))
        for col, expected in _EXPECTED.items():
            self.assertAlmostEqual(got[col], expected, places=12, msg=col)

    def test_a_weekend_asof_rolls_back_to_the_last_stored_session(self):
        # The thematic pipeline runs 7 days a week, so asof is routinely not a
        # session. 2026-03-21 is the Saturday after the anchor.
        from alphalens_pipeline.market.market_state import five_session_index_returns

        got = five_session_index_returns(dt.date(2026, 3, 21), grouped_root=self.root)
        self.assertAlmostEqual(got["market_ret5_spy"], -0.01, places=12)

    def test_a_missing_session_in_the_window_yields_nan_not_a_shorter_window(self):
        # Delete the lookback endpoint. Positional arithmetic would silently
        # measure a 7-session span and return a plausible number; the
        # calendar-anchored lookup must refuse.
        from alphalens_pipeline.market.market_state import five_session_index_returns

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root, self.sessions, _CLOSES, skip={self.sessions[0]})
            got = five_session_index_returns(_ANCHOR, grouped_root=root)

        for col in _EXPECTED:
            self.assertTrue(pd.isna(got[col]), msg=f"{col} should be NaN, got {got[col]!r}")

    def test_a_ticker_absent_from_one_endpoint_yields_nan_for_that_ticker_only(self):
        from alphalens_pipeline.market.market_state import five_session_index_returns

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            partial = {k: v for k, v in _CLOSES.items() if k != "QQQ"}
            _seed(root, self.sessions, partial)
            got = five_session_index_returns(_ANCHOR, grouped_root=root)

        self.assertTrue(pd.isna(got["market_ret5_qqq"]))
        self.assertAlmostEqual(got["market_ret5_spy"], -0.01, places=12)
        self.assertAlmostEqual(got["market_ret5_iwm"], 0.02, places=12)

    def test_a_store_lagging_past_the_bound_yields_nan_not_a_frozen_trio(self):
        from alphalens_pipeline.market.market_state import (
            MARKET_SERIES_MAX_LAG_SESSIONS,
            five_session_index_returns,
        )

        # The newest stored session is one session FURTHER behind asof than the
        # bound allows.
        asof = advance_trading_sessions(_ANCHOR, MARKET_SERIES_MAX_LAG_SESSIONS + 1)
        got = five_session_index_returns(asof, grouped_root=self.root)
        for col in _EXPECTED:
            self.assertTrue(pd.isna(got[col]), msg=f"{col} should be NaN, got {got[col]!r}")

    def test_a_store_inside_the_bound_still_answers(self):
        # The companion to the test above: the bound must not refuse a healthy
        # store that is merely a few sessions behind (free-tier Polygon publishes
        # session D around D+2), or every weekend brief would read NaN.
        from alphalens_pipeline.market.market_state import (
            MARKET_SERIES_MAX_LAG_SESSIONS,
            five_session_index_returns,
        )

        asof = advance_trading_sessions(_ANCHOR, MARKET_SERIES_MAX_LAG_SESSIONS)
        got = five_session_index_returns(asof, grouped_root=self.root)
        self.assertAlmostEqual(got["market_ret5_spy"], -0.01, places=12)

    def test_an_empty_store_yields_nan(self):
        from alphalens_pipeline.market.market_state import five_session_index_returns

        with tempfile.TemporaryDirectory() as tmp:
            got = five_session_index_returns(_ANCHOR, grouped_root=Path(tmp))
        for col in _EXPECTED:
            self.assertTrue(pd.isna(got[col]), msg=col)


class TestEnrichIndexSeries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.sessions = _sessions(_ANCHOR, 6)
        _seed(cls.root, cls.sessions, _CLOSES)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_broadcasts_the_same_trio_to_every_row(self):
        from alphalens_pipeline.market.market_state import enrich_index_series

        frame = pd.DataFrame({"ticker": ["AAA", "BBB", "CCC"]})
        out = enrich_index_series(frame, asof=_ANCHOR, grouped_root=self.root)

        self.assertEqual(len(out), 3)
        self.assertIn("ticker", out.columns)  # original columns preserved
        self.assertEqual(list(frame.columns), ["ticker"])  # input not mutated
        for col, expected in _EXPECTED.items():
            self.assertEqual(out[col].nunique(), 1, msg=col)
            self.assertAlmostEqual(out[col].iloc[0], expected, places=12, msg=col)
            self.assertEqual(str(out[col].dtype), "float64", msg=col)

    def test_the_declaration_is_exactly_what_the_writer_adds(self):
        # The tuple CHECKS the frame, it does not produce it: the three names are
        # string literals at the writer. Renaming a tuple entry must DISAGREE
        # with the stored column rather than silently rename it.
        from alphalens_pipeline.market.market_state import (
            MARKET_SERIES_COLUMNS,
            enrich_index_series,
        )

        frame = pd.DataFrame({"ticker": ["AAA"]})
        out = enrich_index_series(frame, asof=_ANCHOR, grouped_root=self.root)
        added = [c for c in out.columns if c not in set(frame.columns)]
        self.assertEqual(added, list(MARKET_SERIES_COLUMNS))

    def test_empty_frame_gets_every_column_with_declared_dtypes(self):
        from alphalens_pipeline.market.market_state import (
            MARKET_SERIES_COLUMNS,
            enrich_index_series,
        )

        out = enrich_index_series(
            pd.DataFrame({"ticker": pd.Series([], dtype=object)}),
            asof=_ANCHOR,
            grouped_root=self.root,
        )
        self.assertEqual(len(out), 0)
        for col in MARKET_SERIES_COLUMNS:
            self.assertIn(col, out.columns)
            self.assertEqual(str(out[col].dtype), "float64", msg=col)

    def test_a_raising_store_read_degrades_to_nan_rather_than_aborting(self):
        from alphalens_pipeline.market import market_state

        def _boom(*args, **kwargs):
            raise RuntimeError("disk on fire")

        original = market_state.five_session_index_returns
        market_state.five_session_index_returns = _boom
        try:
            with self.assertLogs("alphalens_pipeline.market.market_state", level="WARNING"):
                out = market_state.enrich_index_series(
                    pd.DataFrame({"ticker": ["AAA"]}), asof=_ANCHOR, grouped_root=self.root
                )
        finally:
            market_state.five_session_index_returns = original

        for col in market_state.MARKET_SERIES_COLUMNS:
            self.assertTrue(out[col].isna().all(), msg=col)
            self.assertEqual(str(out[col].dtype), "float64", msg=col)


class TestTheDisplayDataBoundary(unittest.TestCase):
    """The whole point of the family: it is NOT part of the classifier."""

    def test_no_series_column_is_in_market_state_columns(self):
        from alphalens_pipeline.market.market_state import (
            MARKET_SERIES_COLUMNS,
            MARKET_STATE_COLUMNS,
        )

        overlap = sorted(set(MARKET_SERIES_COLUMNS) & set(MARKET_STATE_COLUMNS))
        self.assertEqual(overlap, [])

    def test_no_series_column_carries_a_config_version(self):
        from alphalens_pipeline.market.market_state import MARKET_SERIES_COLUMNS

        offenders = [c for c in MARKET_SERIES_COLUMNS if "config_version" in c]
        self.assertEqual(offenders, [])

    def test_enrich_index_series_stamps_no_version_column(self):
        from alphalens_pipeline.market.market_state import enrich_index_series

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root, _sessions(_ANCHOR, 6), _CLOSES)
            out = enrich_index_series(
                pd.DataFrame({"ticker": ["AAA"]}), asof=_ANCHOR, grouped_root=root
            )
        offenders = [c for c in out.columns if "version" in c]
        self.assertEqual(offenders, [])

    def test_the_classifier_version_is_untouched_by_this_family(self):
        # A bump would orphan the v1.2 cohort for data with no threshold to
        # version. Pinned literally, same as test_market_state does.
        from alphalens_pipeline.market.market_state import MARKET_STATE_CONFIG_VERSION

        self.assertEqual(
            MARKET_STATE_CONFIG_VERSION,
            "mstate-v1.2-spy-sma50x200-atrq70-vix15_25-UNVALIDATED",
        )

    def test_the_declared_columns_are_the_three_named_indices(self):
        from alphalens_pipeline.market.market_state import MARKET_SERIES_COLUMNS

        self.assertEqual(
            MARKET_SERIES_COLUMNS,
            ("market_ret5_spy", "market_ret5_qqq", "market_ret5_iwm"),
        )

    def test_the_three_tickers_are_the_three_broad_indices(self):
        from alphalens_pipeline.market.market_state import MARKET_SERIES_TICKERS

        self.assertEqual(tuple(sorted(MARKET_SERIES_TICKERS.values())), tuple(sorted(_TICKERS)))


if __name__ == "__main__":
    unittest.main()


class TestTheStallBoundIsTightEnoughToMatter(unittest.TestCase):
    """A 5-session bound let a THREE-day stall answer, and the backfill would then
    bake three identical trios into three published parquets. Measured 2026-10-30
    against a real store whose newest session was 2026-09-03.

    The bound must admit a weekend — the pipeline runs 7 days a week and a Saturday
    `asof` legitimately resolves to the Friday — and one failed top-up night, and
    refuse anything deeper.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        _seed(self.root, _sessions(_ANCHOR, 6), _CLOSES)

    def _spy(self, asof: dt.date) -> float:
        return market_state.five_session_index_returns(asof, grouped_root=self.root)[
            "market_ret5_spy"
        ]

    def test_the_anchor_session_itself_answers(self):
        self.assertAlmostEqual(self._spy(_ANCHOR), -0.01)

    def test_a_weekend_asof_answers_from_the_friday(self):
        saturday = _ANCHOR + dt.timedelta(days=1)
        self.assertEqual(saturday.weekday(), 5, "fixture anchor is not a Friday")
        self.assertAlmostEqual(self._spy(saturday), -0.01)

    def test_one_session_of_stall_still_answers(self):
        one = advance_trading_sessions(_ANCHOR, 1)
        self.assertFalse(
            math.isnan(self._spy(one)), "one missed top-up night must not blank the strip"
        )

    def test_two_sessions_of_stall_is_nan_not_a_repeated_trio(self):
        two = advance_trading_sessions(_ANCHOR, 2)
        values = market_state.five_session_index_returns(two, grouped_root=self.root)
        for column, value in values.items():
            self.assertTrue(math.isnan(value), msg=f"{column} answered from a stalled store")

    def test_the_constant_is_not_the_listing_one(self):
        # They held the same value for the wrong reason: one guards a boolean that
        # a week of lag cannot change, the other a five-session return.
        from alphalens_pipeline.data import rs_history

        self.assertLess(
            market_state.MARKET_SERIES_MAX_LAG_SESSIONS,
            rs_history.LISTING_STATUS_MAX_LAG_SESSIONS,
        )
