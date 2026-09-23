"""Tests for the selection-label stamper (ML label registry, memo §5.1, §8 step 1).

All prices are synthetic. A window is described by its daily returns, so every
expected label below is computed by hand from those returns, never by calling the
code under test.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import pyarrow as pa
from alphalens_pipeline.feedback import selection_label as sl
from alphalens_pipeline.feedback.market_beta import BETA_ESTIMATED, BETA_FALLBACK_THIN
from alphalens_pipeline.feedback.split_audit import UNANSWERED, SpanAudit
from alphalens_pipeline.paper.calendar import advance_trading_sessions, n_sessions_before

#: A second vendor that looked at the whole span and found nothing wrong. Passing this
#: explicitly in every case is deliberate: the audit has no default, so a caller cannot
#: forget it and silently get an unguarded label.
CLEAN = SpanAudit(answered=True, breaks=frozenset(), unchecked=frozenset())


def _audit(*, breaks=(), unchecked=()) -> SpanAudit:
    return SpanAudit(answered=True, breaks=frozenset(breaks), unchecked=frozenset(unchecked))


D = dt.date
BRIEF_TUE = D(2026, 3, 3)  # Tuesday; a reader trades Wednesday 03-04
T0 = D(2026, 3, 4)
LATE = D(2026, 9, 1)  # every horizon of T0 has closed by then

# Pre-window IWM returns with zero mean, so a stock at k × IWM has intercept 0 and beta k.
_PRE_IWM = [0.01, -0.01, 0.02, -0.02, 0.005, -0.005] * 42  # 252 >= 250


def _pre_sessions(t0: dt.date, n: int = sl.BETA_WINDOW_SESSIONS + 1) -> list[dt.date]:
    return [n_sessions_before(t0, k) for k in range(n, 0, -1)]


def _win_sessions(t0: dt.date, n: int) -> list[dt.date]:
    return [advance_trading_sessions(t0, k) for k in range(n)]


def _closes(returns: list[float], start: float) -> list[float]:
    out = [start]
    for r in returns:
        out.append(out[-1] * (1.0 + r))
    return out


def make_book(
    t0: dt.date = T0,
    *,
    beta: float = 1.0,
    stock_window: list[float] | None = None,
    iwm_window: list[float] | None = None,
    stock_open: float = 10.0,
    iwm_open: float = 100.0,
    ticker: str = "AAA",
    pre_sessions: int = sl.BETA_WINDOW_SESSIONS + 1,
) -> dict[dt.date, dict[str, tuple[float | None, float | None]]]:
    """A price book with a clean pre-window (stock = beta × IWM) and a window.

    ``stock_window[k]`` is the return of session k: open→close on ``t0`` (k = 0),
    close→close after that. Same for IWM.
    """
    book: dict[dt.date, dict[str, tuple[float | None, float | None]]] = {}
    pre = _pre_sessions(t0, pre_sessions)
    iwm_pre = _closes(_PRE_IWM[: len(pre) - 1], 90.0)
    stock_pre = _closes([beta * r for r in _PRE_IWM[: len(pre) - 1]], 9.0)
    for s, ic, sc in zip(pre, iwm_pre, stock_pre, strict=True):
        book[s] = {"IWM": (ic, ic), ticker: (sc, sc)}
    stock_window = stock_window or []
    iwm_window = iwm_window or [0.0] * len(stock_window)
    sessions = _win_sessions(t0, len(stock_window))
    s_close, i_close = stock_open, iwm_open
    for s, rs, ri in zip(sessions, stock_window, iwm_window, strict=True):
        s_close, i_close = s_close * (1 + rs), i_close * (1 + ri)
        book[s] = {
            "IWM": (iwm_open if s == t0 else i_close, i_close),
            ticker: (stock_open if s == t0 else s_close, s_close),
        }
    return book


def label(
    book, *, brief_date=BRIEF_TUE, published=True, now_session=LATE, newest=None, audit=CLEAN, **kw
):
    return sl.compute_selection_label(
        book,
        "AAA",
        brief_date=brief_date,
        published_before_open=published,
        last_closed_session=now_session,
        newest_session=newest if newest is not None else max(book, default=None),
        audit=audit,
        **kw,
    )


class TestAnchor(unittest.TestCase):
    def test_weekday_brief_anchors_on_the_next_session(self):
        result = label(make_book(stock_window=[0.0] * 40))
        self.assertEqual(result.anchor_session, T0)

    def test_a_move_on_the_brief_date_itself_is_not_in_the_label(self):
        book = make_book(stock_window=[0.10])
        quiet = label(book).values["sel_ar_1"]
        # Session D (the brief date) jumps +50 %: it is in the pre-window, not the label.
        a, _ = book[BRIEF_TUE]["AAA"]
        book[BRIEF_TUE] = {**book[BRIEF_TUE], "AAA": (a, a * 1.5)}
        self.assertAlmostEqual(label(book).values["sel_ar_1"], quiet, places=12)

    def test_weekend_brief_anchors_on_monday(self):
        saturday = D(2026, 3, 7)
        result = label(make_book(D(2026, 3, 9), stock_window=[0.0]), brief_date=saturday)
        self.assertEqual(result.anchor_session, D(2026, 3, 9))

    def test_holiday_is_skipped(self):
        # 2026-05-22 is a Friday; Monday 05-25 is Memorial Day.
        result = label(make_book(D(2026, 5, 26), stock_window=[0.0]), brief_date=D(2026, 5, 22))
        self.assertEqual(result.anchor_session, D(2026, 5, 26))


class TestConstruction(unittest.TestCase):
    def test_h1_is_open_to_close_of_the_anchor_session_minus_beta_iwm(self):
        result = label(make_book(beta=1.0, stock_window=[0.05], iwm_window=[0.02]))
        self.assertEqual(result.statuses["sel_ar_1"], sl.STATUS_OK)
        self.assertAlmostEqual(result.values["sel_ar_1"], 0.05 - 0.02, places=9)

    def test_wealth_paths_compound_daily_beta_times_iwm(self):
        # beta 0.5: stock +5 %, -2 %, +5 %; IWM +2 %, -2 %, +2 %.
        book = make_book(beta=0.5, stock_window=[0.05, -0.02, 0.05], iwm_window=[0.02, -0.02, 0.02])
        result = label(book)
        self.assertAlmostEqual(result.pre.beta.beta, 0.5, places=9)
        w_stock = 1.05 * 0.98 * 1.05
        w_bench = 1.01 * 0.99 * 1.01
        self.assertAlmostEqual(result.values["sel_ar_3"], w_stock - w_bench, places=9)
        naive = (w_stock - 1) - 0.5 * (1.02 * 0.98 * 1.02 - 1)
        self.assertGreater(abs(result.values["sel_ar_3"] - naive), 1e-5)

    def test_intercept_is_not_carried(self):
        # Flat window for both legs: whatever the pre-window drift, the label is 0.
        book = make_book(beta=1.0, stock_window=[0.0, 0.0, 0.0])
        pre = _pre_sessions(T0)
        for i, s in enumerate(pre):  # add a steady +0.3 %/day drift to the stock history
            o, c = book[s]["AAA"]
            book[s]["AAA"] = (o * 1.003**i, c * 1.003**i)
        self.assertAlmostEqual(label(book).values["sel_ar_3"], 0.0, places=12)

    def test_increments_and_path_mean(self):
        stock = [0.01] * 40
        result = label(make_book(beta=1.0, stock_window=stock))
        path = [1.01**k - 1.0 for k in range(1, 41)]  # IWM flat
        v = result.values
        self.assertAlmostEqual(v["sel_ar_20"], path[19], places=9)
        self.assertAlmostEqual(v["sel_ar_40"], path[39], places=9)
        self.assertAlmostEqual(v["sel_ar_inc_1_10"], path[9], places=9)
        self.assertAlmostEqual(v["sel_ar_inc_11_20"], path[19] - path[9], places=9)
        self.assertAlmostEqual(v["sel_ar_inc_21_40"], path[39] - path[19], places=9)
        self.assertAlmostEqual(v["sel_car_mean_20"], sum(path[:20]) / 20, places=9)
        self.assertEqual(result.statuses["sel_car_mean_20"], sl.STATUS_OK)

    def test_scaled_label_uses_the_pre_window_residual_sd_only(self):
        book = make_book(beta=1.0, stock_window=[0.04] * 5)
        pre = _pre_sessions(T0)
        # Residual noise on the last 60 pre-window returns: alternate +/-1 % on the stock.
        for j, s in enumerate(pre[-61:]):
            o, c = book[s]["AAA"]
            f = 1.01 if j % 2 else 1.0
            book[s]["AAA"] = (o * f, c * f)
        result = label(book)
        sigma = result.pre.sigma_resid_pre
        self.assertIsNotNone(sigma)
        self.assertAlmostEqual(
            result.values["sel_zar_5"], result.values["sel_ar_5"] / (sigma * math.sqrt(5)), places=9
        )
        # A huge move inside the window does not change sigma.
        book2 = dict(book)
        o, c = book2[advance_trading_sessions(T0, 2)]["AAA"]
        book2[advance_trading_sessions(T0, 2)] = {
            **book2[advance_trading_sessions(T0, 2)],
            "AAA": (o, c * 1.3),
        }
        self.assertEqual(label(book2).pre.sigma_resid_pre, sigma)


class TestBeta(unittest.TestCase):
    def test_known_slope_over_250_sessions(self):
        result = label(make_book(beta=1.7, stock_window=[0.0]))
        self.assertEqual(result.pre.beta.source, BETA_ESTIMATED)
        self.assertAlmostEqual(result.pre.beta.beta, 1.7, places=9)
        self.assertEqual(result.pre.beta.n_observations, sl.BETA_WINDOW_SESSIONS)

    def test_short_history_falls_back_to_one(self):
        book = make_book(beta=1.7, stock_window=[0.0], pre_sessions=sl.MIN_BETA_OBS + 1)
        for s in _pre_sessions(T0)[: -sl.MIN_BETA_OBS]:
            book.pop(s, None)
        book[_pre_sessions(T0)[-sl.MIN_BETA_OBS]]["AAA"] = (None, None)  # one pair short
        result = label(book)
        self.assertEqual(result.pre.beta.source, BETA_FALLBACK_THIN)
        self.assertEqual(result.pre.beta.beta, 1.0)

    def test_a_confirmed_break_in_the_pre_window_is_dropped_and_counted(self):
        book = make_book(beta=1.7, stock_window=[0.0])
        pre = _pre_sessions(T0)
        for s in pre[100:]:  # an unadjusted 1:2 step at session 100
            o, c = book[s]["AAA"]
            book[s]["AAA"] = (o * 0.5, c * 0.5)
        result = label(book, audit=_audit(breaks=[pre[100]]))
        self.assertEqual(result.pre.n_split_dropped, 1)
        self.assertAlmostEqual(result.pre.beta.beta, 1.7, places=9)
        self.assertEqual(result.pre.beta.n_observations, sl.BETA_WINDOW_SESSIONS - 2)

    def test_an_unconfirmed_jump_in_the_pre_window_is_kept(self):
        # The old band nulled any close whose ratio left (0.55, 1.8), which on the whole
        # store meant three real moves dropped and no split ever caught. Beta is a
        # nuisance parameter: a genuine move belongs in it.
        book = make_book(beta=1.7, stock_window=[0.0])
        pre = _pre_sessions(T0)
        for s in pre[100:]:
            o, c = book[s]["AAA"]
            book[s]["AAA"] = (o * 0.5, c * 0.5)
        self.assertEqual(label(book, audit=CLEAN).pre.n_split_dropped, 0)

    def test_a_given_pre_window_is_reused_not_recomputed(self):
        book = make_book(beta=1.7, stock_window=[0.02], iwm_window=[0.01])
        pre = label(book).pre
        for s in _pre_sessions(T0):
            book.pop(s)
        result = label(book, pre=pre)
        self.assertAlmostEqual(result.values["sel_ar_1"], 0.02 - 1.7 * 0.01, places=9)


class TestStatus(unittest.TestCase):
    def test_late_publication_wins_over_everything(self):
        result = label({}, published=False)
        self.assertEqual(set(result.statuses.values()), {sl.STATUS_SET_FINAL_AFTER_OPEN})
        self.assertTrue(all(v is None for v in result.values.values()))

    def test_unknown_publication(self):
        result = label(make_book(stock_window=[0.0] * 40), published=None)
        self.assertEqual(set(result.statuses.values()), {sl.STATUS_PUBLICATION_UNKNOWN})

    def test_immature_after_the_last_closed_session(self):
        book = make_book(stock_window=[0.0] * 5)
        result = label(book, now_session=advance_trading_sessions(T0, 2))
        self.assertEqual(result.statuses["sel_ar_3"], sl.STATUS_OK)
        self.assertEqual(result.statuses["sel_ar_5"], sl.STATUS_IMMATURE)

    def test_immature_after_the_newest_file_on_disk(self):
        book = make_book(stock_window=[0.0] * 5)
        result = label(book, newest=advance_trading_sessions(T0, 2))
        self.assertEqual(result.statuses["sel_ar_5"], sl.STATUS_IMMATURE)

    def test_missing_session_file_is_not_a_missing_ticker(self):
        book = make_book(stock_window=[0.0] * 5)
        book[advance_trading_sessions(T0, 1)] = None  # file absent / unreadable
        result = label(book)
        self.assertEqual(result.statuses["sel_ar_1"], sl.STATUS_OK)
        self.assertEqual(result.statuses["sel_ar_3"], sl.STATUS_GROUPED_SESSION_MISSING)

    def test_no_open(self):
        book = make_book(stock_window=[0.0] * 3)
        _, c = book[T0]["AAA"]
        book[T0]["AAA"] = (None, c)
        self.assertEqual(label(book).statuses["sel_ar_1"], sl.STATUS_NO_OPEN)

    def test_benchmark_missing(self):
        book = make_book(stock_window=[0.0] * 3)
        del book[advance_trading_sessions(T0, 2)]["IWM"]
        result = label(book)
        self.assertEqual(result.statuses["sel_ar_1"], sl.STATUS_OK)
        self.assertEqual(result.statuses["sel_ar_3"], sl.STATUS_BENCHMARK_MISSING)

    def test_ticker_absent_from_a_present_file(self):
        book = make_book(stock_window=[0.0] * 3)
        del book[advance_trading_sessions(T0, 2)]["AAA"]
        result = label(book)
        self.assertEqual(result.statuses["sel_ar_3"], sl.STATUS_NO_CLOSE_AT_HORIZON)
        self.assertIsNone(result.values["sel_ar_3"])

    def test_derived_values_take_the_status_of_their_longest_horizon(self):
        result = label(make_book(stock_window=[0.0] * 15))
        self.assertEqual(result.statuses["sel_ar_inc_1_10"], sl.STATUS_OK)
        self.assertEqual(result.statuses["sel_ar_inc_11_20"], sl.STATUS_IMMATURE)
        self.assertEqual(result.statuses["sel_car_mean_20"], sl.STATUS_IMMATURE)
        self.assertIsNone(result.values["sel_car_mean_20"])


class TestTheReferenceCache(unittest.TestCase):
    """One reference series per ticker per run, and a failure must not erase a success."""

    def setUp(self):
        self.calls: list[tuple[str, dt.date, dt.date]] = []

    def _fetch(self, answers):
        def fetch(ticker, start, end):
            self.calls.append((ticker, start, end))
            return answers.pop(0)

        return fetch

    @staticmethod
    def _series(n=5):
        days = [D(2026, 3, 2) + dt.timedelta(days=i) for i in range(n)]
        return pd.Series([10.0] * n, index=pd.to_datetime(days), dtype=float)

    def test_a_request_inside_a_held_span_does_not_refetch(self):
        cache = sl._ReferenceCloses(self._fetch([self._series()]))
        cache.closes("AAA", D(2026, 3, 1), D(2026, 3, 31))
        cache.closes("aaa", D(2026, 3, 5), D(2026, 3, 20))
        self.assertEqual(len(self.calls), 1)

    def test_a_wider_request_refetches_the_union_span(self):
        cache = sl._ReferenceCloses(self._fetch([self._series(), self._series()]))
        cache.closes("AAA", D(2026, 3, 1), D(2026, 3, 31))
        cache.closes("AAA", D(2026, 2, 1), D(2026, 3, 10))
        self.assertEqual(len(self.calls), 2)
        _, start, end = self.calls[1]
        self.assertEqual(start, D(2026, 2, 1))
        self.assertGreater(end, D(2026, 3, 31))

    def test_a_failed_widening_does_not_throw_away_the_series_already_held(self):
        # Without this, one refused fetch on a wider span would turn every row that the
        # narrower span had already answered into `split_unchecked` for the rest of the
        # run - a vendor hiccup silently demoting work that was already done.
        held = self._series()
        cache = sl._ReferenceCloses(self._fetch([held, None]))
        cache.closes("AAA", D(2026, 3, 1), D(2026, 3, 31))
        self.assertIsNone(cache.closes("AAA", D(2026, 2, 1), D(2026, 3, 10)))
        self.assertIs(cache.closes("AAA", D(2026, 3, 2), D(2026, 3, 20)), held)
        self.assertEqual(len(self.calls), 2)

    def test_a_first_fetch_that_fails_is_not_retried_within_the_run(self):
        cache = sl._ReferenceCloses(self._fetch([None]))
        self.assertIsNone(cache.closes("AAA", D(2026, 3, 1), D(2026, 3, 31)))
        self.assertIsNone(cache.closes("AAA", D(2026, 3, 5), D(2026, 3, 20)))
        self.assertEqual(len(self.calls), 1)

    def test_a_raising_fetch_is_reported_as_no_answer_not_a_crash(self):
        def boom(ticker, start, end):
            raise RuntimeError("vendor down")

        self.assertIsNone(sl._ReferenceCloses(boom).closes("AAA", D(2026, 3, 1), D(2026, 3, 31)))


class TestTheCrossSourcedSplitGuard(unittest.TestCase):
    """#1533: a second vendor decides, not the size of the jump.

    The band this replaces never caught a split in the whole store and failed three rows
    on one real -47% day. The cases below are the two halves of that: a confirmed
    adjustment break must fail the row, and a large move both sources agree on must not.
    """

    def test_a_confirmed_break_inside_the_window_fails_that_horizon(self):
        book = make_book(stock_window=[0.0, -0.5, 0.0])
        result = label(book, audit=_audit(breaks=[advance_trading_sessions(T0, 1)]))
        self.assertEqual(result.statuses["sel_ar_1"], sl.STATUS_OK)
        self.assertEqual(result.statuses["sel_ar_3"], sl.STATUS_SPLIT_GUARD)
        self.assertIsNone(result.values["sel_ar_3"])

    def test_a_break_at_the_anchor_does_not_fail_the_window(self):
        # The anchor is the first session of the window, so the step INTO it is never
        # inside the window's own return. A uniform rescaling from the anchor onward
        # cancels out of every horizon.
        book = make_book(stock_window=[0.0] * 5)
        result = label(book, audit=_audit(breaks=[T0]))
        self.assertEqual(result.statuses["sel_ar_3"], sl.STATUS_OK)

    def test_a_break_after_the_horizon_does_not_fail_the_shorter_horizons(self):
        book = make_book(stock_window=[0.0] * 10)
        result = label(book, audit=_audit(breaks=[advance_trading_sessions(T0, 7)]))
        self.assertEqual(result.statuses["sel_ar_5"], sl.STATUS_OK)
        self.assertEqual(result.statuses["sel_ar_10"], sl.STATUS_SPLIT_GUARD)

    def test_a_large_move_both_sources_report_is_kept(self):
        # The MYGN case, and the whole reason for the change: a genuine -47% session that
        # the old band failed. Every firing the band ever produced was this shape.
        book = make_book(stock_window=[0.0, -0.47, 0.0])
        result = label(book, audit=CLEAN)
        self.assertEqual(result.statuses["sel_ar_3"], sl.STATUS_OK)
        self.assertIsNotNone(result.values["sel_ar_3"])

    def test_a_within_session_move_is_never_an_adjustment_artefact(self):
        # Open and close of one session come from the SAME store file, hence the same
        # adjustment epoch, so no corporate action can sit between them. The old band
        # checked open->close of the anchor and failed a +90% intraday move on it.
        result = label(make_book(stock_window=[0.9]), audit=CLEAN)
        self.assertEqual(result.statuses["sel_ar_1"], sl.STATUS_OK)

    def test_an_unanswered_reference_makes_the_row_unchecked_not_ok(self):
        book = make_book(stock_window=[0.0] * 5)
        result = label(book, audit=UNANSWERED)
        self.assertEqual(result.statuses["sel_ar_3"], sl.STATUS_SPLIT_UNCHECKED)
        self.assertIsNone(result.values["sel_ar_3"])

    def test_an_unchecked_session_inside_the_window_makes_that_horizon_unchecked(self):
        book = make_book(stock_window=[0.0] * 5)
        result = label(book, audit=_audit(unchecked=[advance_trading_sessions(T0, 2)]))
        self.assertEqual(result.statuses["sel_ar_1"], sl.STATUS_OK)
        self.assertEqual(result.statuses["sel_ar_3"], sl.STATUS_SPLIT_UNCHECKED)

    def test_a_confirmed_break_outranks_an_unchecked_session(self):
        # Both present: the row must report the stronger statement, which is the one the
        # reference actually established.
        book = make_book(stock_window=[0.0] * 5)
        result = label(
            book,
            audit=_audit(
                breaks=[advance_trading_sessions(T0, 1)],
                unchecked=[advance_trading_sessions(T0, 2)],
            ),
        )
        self.assertEqual(result.statuses["sel_ar_3"], sl.STATUS_SPLIT_GUARD)


class TestWhenAnUncheckedRowStopsBeingRetried(unittest.TestCase):
    """A ticker the reference will never serve must not be retried forever.

    Retrying forever keeps the row non-terminal, so it never carries a usable label and
    quietly leaves the panel. Names that vanish from a price vendor are disproportionately
    delisted ones, so that silent shrinkage would be a selection effect correlated with
    bad outcomes - worse than the artefact the guard exists to catch. After the grace
    window the row becomes terminal and DISCLOSES that it was never checked.
    """

    def _row(self, status, anchor=T0):
        row = {"sel_label_version": sl.SEL_LABEL_VERSION, "anchor_session": anchor}
        for h in sl.HORIZONS:
            row[sl.status_key(h)] = status
        return row

    def _at(self, days):
        return dt.datetime.combine(T0 + dt.timedelta(days=days), dt.time(12, 0), dt.UTC)

    def test_unchecked_is_retried_inside_the_grace_window(self):
        row = self._row(sl.STATUS_SPLIT_UNCHECKED)
        self.assertTrue(sl._is_non_terminal(row, self._at(sl.SPLIT_UNCHECKED_RETRY_DAYS - 1)))

    def test_unchecked_becomes_terminal_after_it(self):
        row = self._row(sl.STATUS_SPLIT_UNCHECKED)
        self.assertFalse(sl._is_non_terminal(row, self._at(sl.SPLIT_UNCHECKED_RETRY_DAYS + 1)))

    def test_the_grace_window_outlasts_the_longest_horizon(self):
        # Retiring a row before its own 40-session window has even closed would strand it
        # as unchecked while the reference still had every chance to answer.
        last = advance_trading_sessions(T0, sl.MAX_HORIZON - 1)
        self.assertGreater(sl.SPLIT_UNCHECKED_RETRY_DAYS, (last - T0).days)

    def test_a_confirmed_guard_is_terminal_immediately(self):
        self.assertFalse(sl._is_non_terminal(self._row(sl.STATUS_SPLIT_GUARD), self._at(1)))

    def test_an_immature_row_is_still_retried_long_after_the_grace_window(self):
        self.assertTrue(sl._is_non_terminal(self._row(sl.STATUS_IMMATURE), self._at(10_000)))

    def test_a_row_written_by_an_older_version_is_always_recomputed(self):
        row = self._row(sl.STATUS_OK)
        row["sel_label_version"] = "sel-label-v2"
        self.assertTrue(sl._is_non_terminal(row, self._at(10_000)))

    def test_an_unchecked_row_with_no_anchor_is_retried(self):
        row = self._row(sl.STATUS_SPLIT_UNCHECKED, anchor=None)
        self.assertTrue(sl._is_non_terminal(row, self._at(10_000)))


# ---------------------------------------------------------------------------
# Population and the store pass
# ---------------------------------------------------------------------------


def _brief(rows, published_at="2026-03-04T02:00:00Z"):
    df = pd.DataFrame(rows)
    if published_at is not None:
        df["brief_published_at"] = pd.Timestamp(published_at)
    return df


def _shadow(rows):
    df = pd.DataFrame(rows)
    df["brief_date"] = BRIEF_TUE
    return df


class TestPopulation(unittest.TestCase):
    def test_union_of_briefed_names_and_llm_proposals(self):
        brief = _brief(
            [
                {"theme": "t1", "ticker": "AAA", "mapper_config_version": "v4"},
                {"theme": "t2", "ticker": "AAA", "mapper_config_version": "v4"},
            ]
        )
        shadow = _shadow(
            [
                {"theme": "t1", "ticker": "AAA", "source": "llm", "mapper_config_version": "v4"},
                {"theme": "t3", "ticker": "AAA", "source": "llm", "mapper_config_version": "v4"},
                {"theme": "t1", "ticker": "BBB", "source": "llm", "mapper_config_version": "v4"},
                {
                    "theme": "t1",
                    "ticker": "CCC",
                    "source": "mechanical",
                    "mapper_config_version": "v4",
                },
            ]
        )
        pop = sl.build_population(brief, shadow).set_index("ticker")
        self.assertEqual(sorted(pop.index), ["AAA", "BBB"])
        self.assertTrue(pop.loc["AAA", "briefed_any_theme"])
        self.assertEqual(list(pop.loc["AAA", "themes_briefed"]), ["t1", "t2"])
        self.assertEqual(list(pop.loc["AAA", "themes_proposed"]), ["t1", "t3"])
        self.assertFalse(pop.loc["BBB", "briefed_any_theme"])
        self.assertTrue(pop.loc["BBB", "shadow_available"])

    def test_event_lane_rows_are_not_labelled_but_overlap_cards_are(self):
        brief = _brief(
            [
                {"theme": "t1", "ticker": "AAA", "source": "thematic", "event_overlap": True},
                {"theme": "", "ticker": "EEE", "source": "insider_cluster", "event_overlap": False},
            ]
        )
        pop = sl.build_population(brief, None).set_index("ticker")
        self.assertEqual(list(pop.index), ["AAA"])
        self.assertTrue(pop.loc["AAA", "event_overlap"])
        self.assertFalse(pop.loc["AAA", "shadow_available"])

    def test_publication_stamp_is_read_from_the_brief(self):
        pop = sl.build_population(_brief([{"theme": "t", "ticker": "aaa"}]), None)
        self.assertEqual(pop.loc[0, "ticker"], "AAA")
        self.assertEqual(pop.loc[0, "brief_published_at"], pd.Timestamp("2026-03-04T02:00:00Z"))


def _reference_from_grouped(grouped: Path):
    """A second vendor that agrees with the store on every session it holds.

    Reading the store back as the reference makes the level ratio exactly 1.0
    everywhere, so the audit is clean and these cases test the store pass rather than
    the guard. The guard's own cases live in TestTheCrossSourcedSplitGuard, and a case
    that wants a DISAGREEING vendor overrides this.
    """

    def fetch(ticker: str, start: dt.date, end: dt.date):
        closes: dict[dt.date, float] = {}
        for path in sorted(grouped.glob("*.parquet")):
            try:
                session = dt.date.fromisoformat(path.stem)
            except ValueError:
                continue
            if not (start <= session < end):
                continue
            try:
                frame = pd.read_parquet(path)
            except (OSError, ValueError, pa.ArrowInvalid):
                continue  # a case that plants an unreadable session file
            hit = frame[frame["T"].astype(str).str.upper() == ticker.upper()]
            if len(hit):
                closes[session] = float(hit["c"].iloc[0])
        if not closes:
            return pd.Series(dtype=float)
        ordered = sorted(closes)
        return pd.Series([closes[s] for s in ordered], index=pd.to_datetime(ordered), dtype=float)

    return fetch


class _Deadline:
    def __init__(self, stop_after: int):
        self.calls = 0
        self.stop_after = stop_after

    def should_stop(self) -> bool:
        self.calls += 1
        return self.calls > self.stop_after


class TestEnrichStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.briefs, self.shadow, self.labels, self.grouped = (
            root / "briefs",
            root / "shadow",
            root / "labels",
            root / "grouped",
        )
        for p in (self.briefs, self.shadow, self.grouped):
            p.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_book(self, book, *, until=None):
        for s, bars in book.items():
            if bars is None or (until is not None and s > until):
                continue
            rows = [{"T": t, "o": o, "c": c, "v": 1.0} for t, (o, c) in bars.items()]
            pd.DataFrame(rows).to_parquet(self.grouped / f"{s.isoformat()}.parquet")

    def _run(self, now=dt.datetime(2026, 9, 1, 7, 0, tzinfo=dt.UTC), deadline=None, reference=None):
        return sl.enrich_selection_labels(
            briefs_dir=self.briefs,
            shadow_dir=self.shadow,
            labels_dir=self.labels,
            grouped_root=self.grouped,
            now=now,
            deadline=deadline,
            reference_closes=reference or _reference_from_grouped(self.grouped),
        )

    def _read(self, d=BRIEF_TUE):
        return pd.read_parquet(self.labels / f"{d.isoformat()}.parquet").set_index("ticker")

    def test_stamps_briefed_and_rejected_names_into_their_own_store(self):
        self._write_book(make_book(stock_window=[0.01] * 40))
        _brief([{"theme": "t1", "ticker": "AAA"}]).to_parquet(self.briefs / "2026-03-03.parquet")
        _shadow([{"theme": "t1", "ticker": "ZZZ", "source": "llm"}]).to_parquet(
            self.shadow / "2026-03-03.parquet"
        )
        self._run()
        out = self._read()
        self.assertEqual(out.loc["AAA", "sel_label_status_20"], sl.STATUS_OK)
        self.assertEqual(out.loc["AAA", "sel_label_version"], sl.SEL_LABEL_VERSION)
        self.assertEqual(out.loc["AAA", "beta_source"], BETA_ESTIMATED)
        self.assertEqual(out.loc["ZZZ", "sel_label_status_1"], sl.STATUS_NO_OPEN)
        self.assertFalse(out.loc["ZZZ", "briefed_any_theme"])
        self.assertEqual(out.loc["AAA", "anchor_session"], T0)

    def test_history_date_published_after_the_open_is_excluded(self):
        # 2026-06-09 is in PUBLISHED_AFTER_OPEN_HISTORY and had NO list before the open,
        # so no recovered population replaces it.
        late_day = D(2026, 6, 9)
        _brief([{"theme": "t", "ticker": "AAA"}], published_at=None).to_parquet(
            self.briefs / f"{late_day.isoformat()}.parquet"
        )
        self._run()
        self.assertEqual(
            self._read(late_day).loc["AAA", "sel_label_status_20"], sl.STATUS_SET_FINAL_AFTER_OPEN
        )

    def test_a_stamp_after_the_open_is_excluded(self):
        self._write_book(make_book(stock_window=[0.01] * 40))
        _brief([{"theme": "t", "ticker": "AAA"}], published_at="2026-03-04T15:00:00Z").to_parquet(
            self.briefs / "2026-03-03.parquet"
        )
        self._run()
        self.assertEqual(
            self._read().loc["AAA", "sel_label_status_1"], sl.STATUS_SET_FINAL_AFTER_OPEN
        )

    def test_terminal_rows_are_not_rewritten(self):
        self._write_book(make_book(stock_window=[0.01] * 40))
        _brief([{"theme": "t", "ticker": "AAA"}]).to_parquet(self.briefs / "2026-03-03.parquet")
        self._run()
        path = self.labels / "2026-03-03.parquet"
        os.utime(path, (1_000_000, 1_000_000))
        self._run()
        self.assertEqual(path.stat().st_mtime, 1_000_000)

    def test_every_label_file_has_the_same_schema_and_the_store_reads_as_one_dataset(self):
        import pyarrow.dataset as ds
        import pyarrow.parquet as pq

        self._write_book(make_book(stock_window=[0.01] * 40))
        _brief([{"theme": "t", "ticker": "AAA"}]).to_parquet(self.briefs / "2026-03-03.parquet")
        # A date whose rows carry no value, no beta, no stamp and no themes proposed:
        # 2026-06-09 was set after the open and has no recovered list to replace it.
        _brief([{"theme": "t", "ticker": "AAA"}], published_at=None).to_parquet(
            self.briefs / "2026-06-09.parquet"
        )
        self._run()
        files = sorted(self.labels.glob("*.parquet"))
        self.assertEqual(len(files), 2)
        schemas = [pq.read_schema(f).remove_metadata() for f in files]
        self.assertTrue(schemas[0].equals(schemas[1]), f"{schemas[0]}\n!=\n{schemas[1]}")
        self.assertEqual(str(schemas[1].field("sel_ar_20").type), "double")
        self.assertEqual(ds.dataset([str(f) for f in files]).to_table().num_rows, 2)

    def test_a_recomputed_row_that_did_not_change_is_not_rewritten(self):
        book = make_book(stock_window=[0.01] * 40)
        self._write_book(book, until=advance_trading_sessions(T0, 9))
        _brief([{"theme": "t", "ticker": "AAA"}]).to_parquet(self.briefs / "2026-03-03.parquet")
        self._run()
        path = self.labels / "2026-03-03.parquet"
        os.utime(path, (1_000_000, 1_000_000))
        report = self._run(now=dt.datetime(2026, 9, 2, 7, 0, tzinfo=dt.UTC))  # still immature
        self.assertEqual(path.stat().st_mtime, 1_000_000)
        self.assertEqual(report.dates_written, 0)

    def test_immature_horizon_is_completed_when_the_session_arrives(self):
        book = make_book(stock_window=[0.01] * 40)
        cut = advance_trading_sessions(T0, 9)
        self._write_book(book, until=cut)
        _brief([{"theme": "t", "ticker": "AAA"}]).to_parquet(self.briefs / "2026-03-03.parquet")
        self._run()
        first = self._read().loc["AAA"]
        self.assertEqual(first["sel_label_status_10"], sl.STATUS_OK)
        self.assertEqual(first["sel_label_status_20"], sl.STATUS_IMMATURE)
        self._write_book(book)
        self._run()
        second = self._read().loc["AAA"]
        self.assertEqual(second["sel_label_status_20"], sl.STATUS_OK)
        self.assertAlmostEqual(second["sel_ar_20"], 1.01**20 - 1.0, places=9)
        self.assertEqual(second["beta_ols"], first["beta_ols"])

    def test_version_change_recomputes(self):
        self._write_book(make_book(stock_window=[0.01] * 40))
        _brief([{"theme": "t", "ticker": "AAA"}]).to_parquet(self.briefs / "2026-03-03.parquet")
        self._run()
        path = self.labels / "2026-03-03.parquet"
        df = pd.read_parquet(path)
        df["sel_label_version"] = "sel-label-v0"
        df["sel_ar_1"] = 99.0
        df.to_parquet(path, index=False)
        self._run()
        self.assertEqual(self._read().loc["AAA", "sel_label_version"], sl.SEL_LABEL_VERSION)
        self.assertAlmostEqual(self._read().loc["AAA", "sel_ar_1"], 0.01, places=9)

    def test_a_new_proposal_is_appended_and_existing_rows_kept(self):
        self._write_book(make_book(stock_window=[0.01] * 40))
        _brief([{"theme": "t", "ticker": "AAA"}]).to_parquet(self.briefs / "2026-03-03.parquet")
        self._run()
        _shadow([{"theme": "t", "ticker": "BBB", "source": "llm"}]).to_parquet(
            self.shadow / "2026-03-03.parquet"
        )
        self._run()
        self.assertEqual(sorted(self._read().index), ["AAA", "BBB"])

    def test_deadline_stops_before_the_next_date(self):
        self._write_book(make_book(stock_window=[0.01] * 40))
        for d in ("2026-03-02", "2026-03-03"):
            _brief([{"theme": "t", "ticker": "AAA"}]).to_parquet(self.briefs / f"{d}.parquet")
        self._run(deadline=_Deadline(stop_after=1))
        self.assertTrue((self.labels / "2026-03-03.parquet").exists())  # newest first
        self.assertFalse((self.labels / "2026-03-02.parquet").exists())

    def test_a_corrupt_input_is_skipped_and_the_pass_continues(self):
        self._write_book(make_book(stock_window=[0.01] * 40))
        (self.briefs / "2026-03-05.parquet").write_bytes(b"not a parquet")
        _brief([{"theme": "t", "ticker": "AAA"}]).to_parquet(self.briefs / "2026-03-03.parquet")
        report = self._run()
        self.assertTrue((self.labels / "2026-03-03.parquet").exists())
        self.assertEqual(report.dates_failed, 1)

    def test_non_date_files_are_ignored(self):
        (self.briefs / "_backup_pre_pr185").mkdir()
        (self.briefs / "notes.parquet").write_bytes(b"x")
        report = self._run()
        self.assertEqual(report.dates_failed, 0)

    def test_report_counts_statuses_not_values(self):
        self._write_book(make_book(stock_window=[0.01] * 40))
        _brief([{"theme": "t", "ticker": "AAA"}]).to_parquet(self.briefs / "2026-03-03.parquet")
        report = self._run()
        self.assertEqual(report.status_counts_h20, {sl.STATUS_OK: 1})
        self.assertEqual(report.dates_written, 1)


def _merge_books(*books):
    """One price book holding every ticker of the books given."""
    merged: dict[dt.date, dict[str, tuple[float | None, float | None]]] = {}
    for book in books:
        for session, bars in book.items():
            merged.setdefault(session, {}).update(bars)
    return merged


class TestRecoveredPreOpenDates(unittest.TestCase):
    """A date whose list was set after the open is labelled on the RECOVERED list (#1494)."""

    # 2026-08-18 is in PUBLISHED_AFTER_OPEN_HISTORY and recovered exactly: TTD, ETSY.
    RECOVERED_DAY = D(2026, 8, 18)
    ANCHOR = D(2026, 8, 19)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.briefs, self.shadow, self.labels, self.grouped = (
            root / "briefs",
            root / "shadow",
            root / "labels",
            root / "grouped",
        )
        for path in (self.briefs, self.shadow, self.grouped):
            path.mkdir()
        book = _merge_books(
            make_book(self.ANCHOR, ticker="TTD", stock_window=[0.01] * 10),
            make_book(self.ANCHOR, ticker="ETSY", stock_window=[0.02] * 10),
            make_book(self.ANCHOR, ticker="ADDED", stock_window=[0.03] * 10),
        )
        for session, bars in book.items():
            rows = [{"T": t, "o": o, "c": c, "v": 1.0} for t, (o, c) in bars.items()]
            pd.DataFrame(rows).to_parquet(self.grouped / f"{session.isoformat()}.parquet")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_stored_brief(self, tickers):
        _brief([{"theme": "t1", "ticker": t} for t in tickers], published_at=None).to_parquet(
            self.briefs / f"{self.RECOVERED_DAY.isoformat()}.parquet"
        )

    def _run(self):
        return sl.enrich_selection_labels(
            briefs_dir=self.briefs,
            shadow_dir=self.shadow,
            labels_dir=self.labels,
            grouped_root=self.grouped,
            now=dt.datetime(2026, 9, 1, 7, 0, tzinfo=dt.UTC),
            reference_closes=_reference_from_grouped(self.grouped),
        )

    def _read(self):
        path = self.labels / f"{self.RECOVERED_DAY.isoformat()}.parquet"
        return pd.read_parquet(path).set_index("ticker")

    def test_the_date_is_labelled_instead_of_excluded(self):
        self._write_stored_brief(["TTD", "ADDED"])
        self._run()
        out = self._read()
        self.assertEqual(out.loc["TTD", "sel_label_status_1"], sl.STATUS_OK)
        self.assertTrue(bool(out.loc["TTD", "published_before_open"]))

    def test_a_name_added_after_the_open_is_removed_from_the_store(self):
        self._write_stored_brief(["TTD", "ADDED"])
        self._run()
        self.assertEqual(sorted(self._read().index), ["ETSY", "TTD"])

    def test_a_name_dropped_after_the_open_is_labelled_again(self):
        self._write_stored_brief(["TTD", "ADDED"])
        self._run()
        out = self._read()
        self.assertEqual(out.loc["ETSY", "sel_label_status_1"], sl.STATUS_OK)
        self.assertTrue(bool(out.loc["ETSY", "briefed_any_theme"]))
        self.assertEqual(list(out.loc["ETSY", "themes_briefed"]), [])

    def test_every_row_says_which_population_it_came_from(self):
        self._write_stored_brief(["TTD", "ADDED"])
        self._run()
        out = self._read()
        self.assertEqual(set(out["population"]), {sl.POPULATION_PRE_OPEN_RECOVERED})

    def test_a_recovered_date_reports_its_shadow_file_on_every_row(self):
        # shadow_available is a fact about the date: a re-added name must not read as
        # "no proposals were recorded that day".
        self._write_stored_brief(["TTD", "ADDED"])
        _shadow([{"theme": "t1", "ticker": "QQQ", "source": "llm"}]).to_parquet(
            self.shadow / f"{self.RECOVERED_DAY.isoformat()}.parquet"
        )
        self._run()
        out = self._read()
        self.assertEqual(set(out["shadow_available"]), {True})
        self.assertNotIn("QQQ", out.index)

    def test_a_stage_value_of_a_kept_name_survives(self):
        _brief(
            [{"theme": "t1", "ticker": "TTD", "event_overlap": True}], published_at=None
        ).to_parquet(self.briefs / f"{self.RECOVERED_DAY.isoformat()}.parquet")
        self._run()
        out = self._read()
        self.assertTrue(bool(out.loc["TTD", "event_overlap"]))
        self.assertEqual(list(out.loc["TTD", "themes_briefed"]), ["t1"])

    def test_a_row_stamped_under_the_old_version_is_recomputed(self):
        self._write_stored_brief(["TTD", "ADDED"])
        self._run()
        path = self.labels / f"{self.RECOVERED_DAY.isoformat()}.parquet"
        stored = pd.read_parquet(path)
        self.assertEqual(set(stored["sel_label_version"]), {sl.SEL_LABEL_VERSION})
        stored["sel_label_version"] = "sel-label-v1"
        stored.to_parquet(path)
        self._run()
        self.assertEqual(set(self._read()["sel_label_version"]), {sl.SEL_LABEL_VERSION})


if __name__ == "__main__":
    unittest.main()
