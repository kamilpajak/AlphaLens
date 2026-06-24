"""Unit tests for the PEAD v2 experiment driver script glue.

The pre-registered SCORER lives in ``score_pead_pss.py`` (pinned by path in the
preregistration ledger). This suite covers the driver/scaffold glue in
``scripts/experiment_pead_pss_v2.py`` only — no methodology surface.
"""

import importlib
import unittest
from datetime import date, timedelta

import pandas as pd


def _import_script():
    return importlib.import_module("scripts.experiment_pead_pss_v2")


class TestFactorWindowEnd(unittest.TestCase):
    """The factor/calendar window must extend past ``is_end`` by ~1.5x the
    holding period so ``build_daily_weights`` can complete the hold window for
    events whose ``reported_date`` falls near ``is_end``. The callees
    (``load_carhart_daily`` / ``_ensure_business_calendar``) are typed on
    ``datetime.date``, so the helper must return a ``date`` — not a pandas
    ``Timestamp`` (the prior code called ``.date()`` on a value that was
    already a ``date``, raising AttributeError at smoke time)."""

    def test_returns_date_extended_by_one_point_five_holding(self) -> None:
        mod = _import_script()
        out = mod._factor_window_end(date(2018, 3, 31), 20)
        # int(20 * 1.5) == 30 calendar days
        self.assertEqual(out, date(2018, 3, 31) + timedelta(days=30))

    def test_return_type_is_plain_date(self) -> None:
        mod = _import_script()
        out = mod._factor_window_end(date(2018, 3, 31), 20)
        self.assertIsInstance(out, date)
        # ``date`` has no ``.date()`` — the original bug. Guard the contract.
        self.assertFalse(hasattr(out, "date"))


class TestRestrictToIsWindow(unittest.TestCase):
    """``weights`` is indexed by the trading calendar, which is a list of
    ``datetime.date`` (object dtype). Restricting to ``<= is_end`` must compare
    date-to-date; wrapping ``is_end`` in a ``pd.Timestamp`` raises 'Cannot
    compare Timestamp with datetime.date' against the object-dtype index."""

    def test_drops_rows_after_is_end_keeps_boundary(self) -> None:
        mod = _import_script()
        idx = [date(2018, 3, 29), date(2018, 3, 30), date(2018, 4, 2)]
        weights = pd.DataFrame({"AAA": [0.1, 0.2, 0.3]}, index=idx)
        out = mod._restrict_to_is_window(weights, date(2018, 3, 30))
        self.assertEqual(list(out.index), [date(2018, 3, 29), date(2018, 3, 30)])


class TestFormatResultLine(unittest.TestCase):
    """The per-cost result line MUST match the multi-phase orchestrator's
    ``_RESULT_LINE`` regex, or ``run_audit`` parses zero rows and the verdict
    is empty. PEAD v2 was the only experiment script not emitting the canonical
    ``Sh gross=.. net=.. | excess gross=..% net=..% | α 4F=..% t=..`` line."""

    def test_line_matches_orchestrator_regex_and_roundtrips(self) -> None:
        from phase_robust_backtesting.audit_multi_phase import _RESULT_LINE

        mod = _import_script()
        stats = {
            "n": 117,
            "sharpe_gross": 0.42,
            "sharpe_net": 0.21,
            "excess_gross_ann": 0.187,
            "excess_net_ann": 0.161,
            "alpha_gross_4f": 0.278,
            "t_4f": 1.37,
            "alpha_net_4f": 0.255,
            "t_net_4f": 1.20,
        }
        line = mod._format_result_line(stats, 5.0)
        m = _RESULT_LINE.search(line)
        self.assertIsNotNone(m, f"orchestrator _RESULT_LINE did not match: {line!r}")
        assert m is not None  # narrow Optional[Match] for the type-checker
        self.assertAlmostEqual(float(m.group("sg")), 0.42)
        self.assertAlmostEqual(float(m.group("sn")), 0.21)
        self.assertAlmostEqual(float(m.group("eg")), 18.7)
        self.assertAlmostEqual(float(m.group("en")), 16.1)
        self.assertAlmostEqual(float(m.group("a")), 27.8)
        self.assertAlmostEqual(float(m.group("t")), 1.37)
        self.assertAlmostEqual(float(m.group("an")), 25.5)
        self.assertAlmostEqual(float(m.group("tn")), 1.20)

    def test_config_key_survives_the_n_split(self) -> None:
        # _config_key_from_line splits on " | n=" — the cost prefix must come
        # first so each cost groups across phases under its own key.
        mod = _import_script()
        stats = {
            "n": 90,
            "sharpe_gross": 0.1,
            "sharpe_net": 0.0,
            "excess_gross_ann": 0.05,
            "excess_net_ann": 0.04,
            "alpha_gross_4f": 0.01,
            "t_4f": 0.5,
            "alpha_net_4f": 0.0,
            "t_net_4f": 0.4,
        }
        line = mod._format_result_line(stats, 15.0)
        self.assertTrue(line.startswith("cost=15bps | n=90 |"))


class TestDropUncompletableTailEvents(unittest.TestCase):
    """At the factor-data tail, an event whose entry + 20-day hold extends past
    the available trading calendar cannot have its drift observed yet. The
    pre-reg ``build_daily_weights``/``compute_exit_day`` RAISE on such an event,
    which crashed the full/FL windows (the 2018-Q1 smoke never reached the
    tail). The driver must right-censor those events BEFORE building weights."""

    def _cal(self, n: int) -> list:
        return [d.date() for d in pd.bdate_range("2018-01-01", periods=n)]

    def _ev(self, ticker: str, rd, report_time: str = "pre-market"):
        from alphalens_research.screeners.event_drift.av_earnings_ingestion import (
            AVEarningsAnnouncement,
        )

        return AVEarningsAnnouncement(
            ticker=ticker,
            period_end=rd,
            reported_date=rd,
            reported_eps=1.0,
            estimated_eps=0.5,
            report_time=report_time,  # type: ignore[arg-type]
        )

    def test_drops_event_whose_hold_exceeds_calendar(self) -> None:
        mod = _import_script()
        cal = self._cal(30)
        early = self._ev("AAA", cal[0])  # entry idx 0 → 0+20 < 30 → keep
        late = self._ev("BBB", cal[15])  # entry idx 15 → 15+20 ≥ 30 → drop
        kept = mod._drop_uncompletable_tail_events([early, late], cal, hold_days=20)
        tickers = {e.ticker for e in kept}
        self.assertIn("AAA", tickers)
        self.assertNotIn("BBB", tickers)

    def test_drops_event_reported_after_calendar_end(self) -> None:
        mod = _import_script()
        cal = self._cal(30)
        after = self._ev("CCC", date(2030, 1, 1))  # no entry day → ValueError → drop
        self.assertEqual(mod._drop_uncompletable_tail_events([after], cal, hold_days=20), [])

    def test_kept_events_survive_build_daily_weights(self) -> None:
        mod = _import_script()
        from alphalens_research.screeners.event_drift.pead_pss_scorer import (
            build_daily_weights,
        )

        cal = self._cal(60)
        evs = [self._ev("AAA", cal[5]), self._ev("BBB", cal[55])]  # BBB: 55+20 ≥ 60 → drop
        kept = mod._drop_uncompletable_tail_events(evs, cal, hold_days=20)
        # The whole point: build_daily_weights must not raise on the kept set.
        weights = build_daily_weights(events=kept, calendar=cal, n_fixed=150, hold_days=20)
        self.assertEqual(list(weights.columns), ["AAA"])


class TestInferenceDiagnostics(unittest.TestCase):
    """Gate #4 (§18.2 bootstrap-CI) + §18.1 all-days companion αt — both are
    REPORTED diagnostics (no v3, no Bonferroni increment). The driver must
    expose the net returns series so it can compute, beside the binding
    invested-days-only αt: (a) an all-days (cash-inclusive) Carhart αt whose
    gap from the invested-only number flags masking lift (§18.1 suspect
    >0.2t), and (b) a moving-block bootstrap 95% CI on the net Carhart-4F α
    (§18.2; required to exclude 0 for any candidate PASS)."""

    def _synthetic(self, n: int = 90, seed: int = 0):
        import numpy as np

        rng = np.random.default_rng(seed)
        idx = pd.bdate_range("2018-01-02", periods=n)
        factors = pd.DataFrame(
            {
                "Mkt-RF": rng.normal(0.0, 0.01, n),
                "SMB": rng.normal(0.0, 0.005, n),
                "HML": rng.normal(0.0, 0.005, n),
                "Mom": rng.normal(0.0, 0.005, n),
                "RF": [0.0001] * n,
            },
            index=idx,
        )
        n_invested = int(n * 0.75)
        invested_mask = pd.Series([True] * n_invested + [False] * (n - n_invested), index=idx)
        net = pd.Series(rng.normal(0.001, 0.01, n), index=idx)
        # uninvested days hold cash → 0.0 return (NOT NaN), matching the B2
        # adapter contract the all-days regressand depends on.
        net = net.where(invested_mask, 0.0)
        return net, invested_mask, factors

    def test_alldays_companion_returns_float(self) -> None:
        mod = _import_script()
        net, _, factors = self._synthetic()
        t = mod._alldays_companion_alpha_t(net, factors, maxlags=5)
        self.assertIsInstance(t, float)

    def test_alldays_companion_none_when_too_few_obs(self) -> None:
        mod = _import_script()
        # run_regression needs >=20 overlapping obs → ValueError → None.
        net, _, factors = self._synthetic(n=12)
        self.assertIsNone(mod._alldays_companion_alpha_t(net, factors, maxlags=5))

    def test_bootstrap_none_below_50_obs(self) -> None:
        mod = _import_script()
        net, mask, factors = self._synthetic(n=40)  # 30 invested < 50
        self.assertIsNone(mod._bootstrap_net_alpha_ci(net.where(mask), factors, iterations=50))

    def test_bootstrap_returns_ordered_tuple(self) -> None:
        mod = _import_script()
        net, mask, factors = self._synthetic(n=120)  # 90 invested >= 50
        ci = mod._bootstrap_net_alpha_ci(net.where(mask), factors, iterations=100)
        self.assertIsNotNone(ci)
        assert ci is not None
        lo, hi = ci
        self.assertLessEqual(lo, hi)

    def test_inference_diagnostics_assembles_and_flags_suspect(self) -> None:
        mod = _import_script()
        net, mask, factors = self._synthetic(n=120)
        d = mod._inference_diagnostics(
            invested_only_alpha_t_net=3.0,
            net_daily=net,
            invested_mask=mask,
            factors=factors,
            maxlags=5,
            bootstrap_iterations=100,
        )
        self.assertIn("alldays_alpha_t_net", d)
        self.assertIn("bootstrap_net_alpha_ci_95", d)
        self.assertIn("bootstrap_ci_excludes_zero", d)
        self.assertEqual(d["invested_only_alpha_t_net"], 3.0)
        # suspect = (invested_only − all_days) > 0.2t
        gap = 3.0 - d["alldays_alpha_t_net"]
        self.assertAlmostEqual(d["invested_minus_alldays_t"], gap)
        self.assertEqual(d["suspect_masking_lift"], gap > mod._ALLDAYS_GAP_SUSPECT_T)

    def test_inference_diagnostics_handles_missing_alldays(self) -> None:
        mod = _import_script()
        # Too few obs → all-days αt is None; gap/suspect degrade gracefully.
        net, mask, factors = self._synthetic(n=12)
        d = mod._inference_diagnostics(
            invested_only_alpha_t_net=4.0,
            net_daily=net,
            invested_mask=mask,
            factors=factors,
            maxlags=5,
            bootstrap_iterations=50,
        )
        self.assertIsNone(d["alldays_alpha_t_net"])
        self.assertIsNone(d["invested_minus_alldays_t"])
        self.assertFalse(d["suspect_masking_lift"])

    def test_suspect_threshold_constant_is_0_2(self) -> None:
        mod = _import_script()
        self.assertEqual(mod._ALLDAYS_GAP_SUSPECT_T, 0.2)


class TestWindowReturns(unittest.TestCase):
    """``_window_returns`` factors the gross/net/invested-mask construction out
    of ``assess()`` so both the cost-grid regression AND the §18.1/§18.2
    diagnostics consume the SAME net series (no drift between the binding
    number and its companion)."""

    def test_net_is_gross_minus_drag_and_mask_tracks_weights(self) -> None:
        mod = _import_script()
        idx = [date(2018, 1, 2), date(2018, 1, 3), date(2018, 1, 4)]
        # AAA live on days 0,1; flat day 2 → invested mask [T,T,F].
        weights = pd.DataFrame({"AAA": [1 / 150, 1 / 150, 0.0]}, index=idx)
        panel = pd.DataFrame({"AAA": [0.01, -0.02, 0.03]}, index=pd.DatetimeIndex(idx))
        gross, net, mask = mod._window_returns(weights, panel, 5.0)
        self.assertEqual(list(mask), [True, True, False])
        # net = gross − constant scalar drag (same shift every day).
        diff = (gross - net).round(12)
        self.assertEqual(diff.nunique(), 1)
        self.assertGreater(float(diff.iloc[0]), 0.0)


class TestInvestedFractionDiag(unittest.TestCase):
    """Memo §6.3 / success-criterion-6 + §17.2 launch gate: a window with
    invested-fraction < 0.40 must be FLAGGED (low deployment maximises the
    masking lift, a false-PASS direction). The code previously only checked
    absolute n_invested >= 20."""

    def test_below_floor_is_flagged(self) -> None:
        mod = _import_script()
        d = mod._invested_fraction_diag(30, 100)
        self.assertAlmostEqual(d["invested_fraction"], 0.30)
        self.assertTrue(d["below_floor"])
        self.assertEqual(d["n_invested"], 30)
        self.assertEqual(d["n_total"], 100)

    def test_at_or_above_floor_not_flagged(self) -> None:
        mod = _import_script()
        d = mod._invested_fraction_diag(63, 100)
        self.assertAlmostEqual(d["invested_fraction"], 0.63)
        self.assertFalse(d["below_floor"])

    def test_zero_total_is_safe_and_flagged(self) -> None:
        mod = _import_script()
        d = mod._invested_fraction_diag(0, 0)
        self.assertEqual(d["invested_fraction"], 0.0)
        self.assertTrue(d["below_floor"])

    def test_floor_constant_is_0_40(self) -> None:
        mod = _import_script()
        self.assertEqual(mod._INVESTED_FRACTION_FLOOR, 0.40)


if __name__ == "__main__":
    unittest.main()
