"""Cross-sourcing the label store against a uniformly adjusted second vendor (#1533).

The store is a patchwork: one parquet per session, never re-fetched, each adjusted as
of its own fetch date. A split after a file is written therefore leaves ONE unadjusted
step. The audit finds that step by comparing LEVELS against a reference that was fetched
in one piece.

Every number in the "measured" cases below came from a real run against
``~/.alphalens/grouped_daily_history`` and cached yfinance bars, and they are the reason
the design is shaped this way:

- 13203 session comparisons over 48 tickers with no artefact: the level ratio deviates
  from its own median by 0.000000 at the 99th percentile and by at most 0.039087 on one
  single session. Not one session in 13203 deviated by more than 5%.
- MQ, the one real artefact (1-for-4 reverse): the level ratio is EXACTLY 0.2500 for the
  20 sessions to 2026-06-29 and EXACTLY 1.0000 for the 22 sessions from 2026-06-30, with
  volume moving 4.0000 -> 1.0000 the other way.

So a persistent shift and a one-session excursion are separated by two orders of
magnitude, and the tolerance sits in the gap. A per-session threshold could not do this:
a 5% stock dividend leaves a 4.76% artefact, which is BELOW the 3.9% single-session noise
plus a margin, so the two would overlap.
"""

from __future__ import annotations

import datetime as dt
import unittest

import pandas as pd
from alphalens_pipeline.feedback import split_audit as sa

D = dt.date


def _sessions(n: int, start: dt.date = D(2026, 6, 1)) -> list[dt.date]:
    """``n`` consecutive weekdays. The audit never reads a calendar, only an order."""
    out: list[dt.date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def _store(sessions: list[dt.date], closes: list[float]) -> dict[dt.date, float]:
    return dict(zip(sessions, closes, strict=True))


def _reference(sessions: list[dt.date], closes: list[float]) -> pd.Series:
    return pd.Series(closes, index=pd.to_datetime(sessions), dtype=float)


class TestTheLevelRatio(unittest.TestCase):
    def test_it_is_formed_only_on_dates_both_sources_carry(self):
        s = _sessions(4)
        ratios = sa.level_ratios(
            store_closes=_store(s, [10.0, 11.0, 12.0, 13.0]),
            reference=_reference(s[:2], [10.0, 11.0]),
        )
        self.assertEqual(sorted(ratios), s[:2])

    def test_identical_sources_give_a_flat_ratio_of_one(self):
        s = _sessions(5)
        closes = [10.0, 10.5, 9.8, 11.2, 10.9]
        ratios = sa.level_ratios(store_closes=_store(s, closes), reference=_reference(s, closes))
        self.assertEqual(set(ratios.values()), {1.0})

    def test_a_non_positive_price_contributes_no_ratio(self):
        s = _sessions(3)
        ratios = sa.level_ratios(
            store_closes=_store(s, [10.0, 0.0, 12.0]), reference=_reference(s, [10.0, 11.0, 0.0])
        )
        self.assertEqual(sorted(ratios), [s[0]])

    def test_a_missing_reference_yields_nothing_rather_than_raising(self):
        s = _sessions(3)
        self.assertEqual(
            sa.level_ratios(store_closes=_store(s, [1.0, 2.0, 3.0]), reference=None), {}
        )


class TestAPersistentShiftIsAnArtefact(unittest.TestCase):
    """The MQ shape: the store holds an older adjustment epoch before the break."""

    def _mq_like(self, n_before: int = 20, n_after: int = 22):
        s = _sessions(n_before + n_after)
        # The reference is uniformly adjusted; the store is on the PRE-split scale until
        # the break, which is what a never-re-fetched session file leaves behind.
        ref = [15.0 + 0.1 * i for i in range(len(s))]
        store = [p / 4.0 for p in ref[:n_before]] + list(ref[n_before:])
        return s, _store(s, store), _reference(s, ref), s[n_before]

    def test_the_break_session_is_reported(self):
        _, store, ref, break_session = self._mq_like()
        audit = sa.audit_span(store_closes=store, reference=ref)
        self.assertTrue(audit.answered)
        self.assertEqual(audit.breaks, frozenset({break_session}))

    def test_only_the_break_session_is_reported_not_the_whole_stale_stretch(self):
        # A window living entirely on the old scale computes a CORRECT return: a uniform
        # rescaling cancels. Only the step across the break is fabricated. Flagging the
        # whole stretch would throw away good episodes.
        _, store, ref, _ = self._mq_like()
        self.assertEqual(len(sa.audit_span(store_closes=store, reference=ref).breaks), 1)

    def test_nothing_is_reported_unchecked_when_the_reference_covers_everything(self):
        _, store, ref, _ = self._mq_like()
        self.assertEqual(sa.audit_span(store_closes=store, reference=ref).unchecked, frozenset())

    def test_a_forward_split_artefact_is_found_in_the_other_direction(self):
        s = _sessions(40)
        ref = [20.0] * 40
        store = [p * 2.0 for p in ref[:20]] + ref[20:]
        audit = sa.audit_span(store_closes=_store(s, store), reference=_reference(s, ref))
        self.assertEqual(audit.breaks, frozenset({s[20]}))

    def test_two_breaks_in_one_span_are_both_found(self):
        s = _sessions(60)
        ref = [20.0] * 60
        store = [p / 2.0 for p in ref[:20]] + [p / 6.0 for p in ref[20:40]] + ref[40:]
        audit = sa.audit_span(store_closes=_store(s, store), reference=_reference(s, ref))
        self.assertEqual(audit.breaks, frozenset({s[20], s[40]}))

    def test_two_breaks_closer_together_than_the_confirmation_window_are_both_found(self):
        # A reviewer argued the second break would be masked, because the confirming
        # median before it straddles the first break. Run: it is not. With only three
        # sessions between them the median before the second break already sits on the
        # intermediate level, so both are reported.
        s = _sessions(43)
        ref = [100.0] * 43
        store = [25.0] * 20 + [100.0] * 3 + [105.0] * 20
        audit = sa.audit_span(store_closes=_store(s, store), reference=_reference(s, ref))
        self.assertEqual(audit.breaks, frozenset({s[20], s[23]}))

    def test_even_two_sessions_apart_both_breaks_are_found(self):
        s = _sessions(42)
        ref = [100.0] * 42
        store = [25.0] * 20 + [100.0] * 2 + [105.0] * 20
        audit = sa.audit_span(store_closes=_store(s, store), reference=_reference(s, ref))
        self.assertEqual(audit.breaks, frozenset({s[20], s[22]}))

    def test_a_five_percent_stock_dividend_is_found(self):
        # The case that killed the per-step threshold: 1/1.05 is a 4.76% artefact, which a
        # 5% per-session rule would pass and a 3.9%-noise-aware rule could not separate.
        # On MEDIANS the noise floor is 0, so 4.76% is far above it.
        s = _sessions(40)
        ref = [30.0] * 40
        store = [p * 1.05 for p in ref[:20]] + ref[20:]
        audit = sa.audit_span(store_closes=_store(s, store), reference=_reference(s, ref))
        self.assertEqual(audit.breaks, frozenset({s[20]}))


class TestASingleSessionExcursionIsNot(unittest.TestCase):
    """The measured null: vendor close disagreement reverts, it does not persist."""

    def test_the_worst_measured_single_session_disagreement_does_not_flag(self):
        # CMPR, the worst of 13203 real comparisons: 3.9087% on one session, then back.
        s = _sessions(40)
        ref = [50.0] * 40
        store = [50.0] * 40
        store[20] = 50.0 * 1.039087
        audit = sa.audit_span(store_closes=_store(s, store), reference=_reference(s, ref))
        self.assertEqual(audit.breaks, frozenset())
        self.assertEqual(audit.unchecked, frozenset())

    def test_an_excursion_far_larger_than_any_split_still_does_not_flag(self):
        # A halted session reported as a stale carry-forward by one vendor can be huge.
        # Size is not the discriminator; persistence is.
        s = _sessions(40)
        ref = [50.0] * 40
        store = [50.0] * 40
        store[20] = 500.0
        self.assertEqual(
            sa.audit_span(store_closes=_store(s, store), reference=_reference(s, ref)).breaks,
            frozenset(),
        )

    def test_a_level_that_jumps_away_and_returns_within_the_window_is_not_a_break(self):
        # The boundary of the whole method, pinned rather than described. Two sessions on
        # a different level and then back is NOT reported, and that is deliberate: it is
        # the shape of vendor noise. It cannot be a store artefact either, because the
        # store's session files are written forward and never re-fetched, so an
        # adjustment epoch only ever changes one way. A store that could re-adjust a
        # middle stretch and revert would defeat this test, and nothing here would say so.
        s = _sessions(42)
        ref = [100.0] * 42
        store = [25.0] * 20 + [100.0] * 2 + [25.0] * 20
        self.assertEqual(
            sa.audit_span(store_closes=_store(s, store), reference=_reference(s, ref)).breaks,
            frozenset(),
        )

    def test_a_long_enough_excursion_is_reported_as_two_breaks(self):
        # The other side of that boundary: once the away-level lasts CONFIRM_SESSIONS it
        # is no longer an excursion, and both edges are reported.
        s = _sessions(45)
        ref = [100.0] * 45
        store = [25.0] * 20 + [100.0] * 5 + [25.0] * 20
        audit = sa.audit_span(store_closes=_store(s, store), reference=_reference(s, ref))
        self.assertEqual(audit.breaks, frozenset({s[20], s[25]}))

    def test_a_clean_span_reports_no_break(self):
        s = _sessions(40)
        closes = [10.0 + 0.3 * (i % 7) for i in range(40)]
        audit = sa.audit_span(store_closes=_store(s, closes), reference=_reference(s, closes))
        self.assertTrue(audit.answered)
        self.assertEqual(audit.breaks, frozenset())

    def test_a_real_market_crash_shared_by_both_sources_is_not_a_break(self):
        # MYGN: a genuine -47% day that BOTH vendors report. The old band failed three
        # rows on exactly this and never once caught a split.
        s = _sessions(40)
        closes = [5.37] * 20 + [2.86] * 20
        audit = sa.audit_span(store_closes=_store(s, closes), reference=_reference(s, closes))
        self.assertEqual(audit.breaks, frozenset())


class TestWhatCannotBeChecked(unittest.TestCase):
    """ "Could not check" must never collapse into "checked and clean"."""

    def test_no_reference_at_all_is_unanswered(self):
        s = _sessions(40)
        audit = sa.audit_span(store_closes=_store(s, [10.0] * 40), reference=None)
        self.assertFalse(audit.answered)

    def test_an_empty_reference_is_unanswered(self):
        s = _sessions(40)
        audit = sa.audit_span(store_closes=_store(s, [10.0] * 40), reference=pd.Series(dtype=float))
        self.assertFalse(audit.answered)

    def test_too_few_shared_sessions_is_unanswered(self):
        s = _sessions(40)
        few = s[: sa.MIN_COMPARABLE_SESSIONS - 1]
        audit = sa.audit_span(
            store_closes=_store(s, [10.0] * 40),
            reference=_reference(few, [10.0] * len(few)),
        )
        self.assertFalse(audit.answered)

    def test_a_store_session_the_reference_omits_is_reported_unchecked(self):
        s = _sessions(40)
        covered = s[:20] + s[21:]
        audit = sa.audit_span(
            store_closes=_store(s, [10.0] * 40),
            reference=_reference(covered, [10.0] * len(covered)),
        )
        self.assertTrue(audit.answered)
        self.assertEqual(audit.unchecked, frozenset({s[20]}))

    def test_a_shift_too_close_to_the_span_edge_to_confirm_is_unchecked_not_clean(self):
        # Fewer than CONFIRM_SESSIONS after the step, so persistence cannot be judged.
        # Calling it clean would be the exact failure this module exists to refuse.
        s = _sessions(30)
        ref = [10.0] * 30
        store = [10.0] * 28 + [5.0, 5.0]
        audit = sa.audit_span(store_closes=_store(s, store), reference=_reference(s, ref))
        self.assertEqual(audit.breaks, frozenset())
        self.assertIn(s[28], audit.unchecked)

    def test_the_unanswered_span_names_no_break(self):
        # A caller that only looked at `breaks` must not read an unanswered audit as clean;
        # `answered` is the field that carries that, and it is False.
        audit = sa.audit_span(store_closes={}, reference=None)
        self.assertFalse(audit.answered)
        self.assertEqual(audit.breaks, frozenset())


class TestTheSpanAuditQueries(unittest.TestCase):
    """What the label asks of an audit."""

    def setUp(self):
        self.s = _sessions(10)
        self.audit = sa.SpanAudit(
            answered=True, breaks=frozenset({self.s[4]}), unchecked=frozenset({self.s[7]})
        )

    def test_a_window_containing_the_break_is_corrupted(self):
        self.assertTrue(self.audit.corrupts(self.s[3:6]))

    def test_a_window_ending_before_the_break_is_not(self):
        self.assertFalse(self.audit.corrupts(self.s[0:4]))

    def test_a_window_starting_at_the_break_is_not_corrupted(self):
        # The first session on the new scale is the anchor of a consistent stretch: the
        # step INTO it is fabricated, but a window that begins there never crosses it.
        self.assertFalse(self.audit.corrupts(self.s[4:8]))

    def test_a_window_touching_an_unchecked_session_reports_it(self):
        self.assertTrue(self.audit.has_unchecked(self.s[6:9]))

    def test_a_window_clear_of_both_reports_neither(self):
        self.assertFalse(self.audit.corrupts(self.s[0:4]))
        self.assertFalse(self.audit.has_unchecked(self.s[0:4]))

    def test_an_unanswered_audit_reports_every_window_unchecked(self):
        unanswered = sa.UNANSWERED
        self.assertTrue(unanswered.has_unchecked(self.s))
        self.assertFalse(unanswered.corrupts(self.s))


if __name__ == "__main__":
    unittest.main()
