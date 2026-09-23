"""Unit tests for the #1227 ATR confirmation — the registered one-shot look.

What these cases protect is the DECISION RULE, not the number. The number comes
from one run on held-out data and is never a regression fixture; the rule was
frozen before that run and must not drift afterwards, which is exactly the kind
of thing a test can hold.

The load-bearing cases are :class:`TestTheDecisionRule` (the floor, not the
p-value, is the binding condition) and :class:`TestTheLiveTiltMapping` (a
non-promotion and a sign flip are DIFFERENT outcomes for the live scorer, and
collapsing them would retire a live tilt the July registration did not ask to
retire).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


def _load():
    path = (
        Path(__file__).resolve().parents[1] / "scripts" / "ml" / "2026_09_a20_atr_confirmation.py"
    )
    spec = importlib.util.spec_from_file_location("_a20_atr_confirmation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The module must be in sys.modules BEFORE it executes: @dataclass resolves
    # its own class's module to read annotations, and a module missing from the
    # table makes that lookup return None.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


atr = _load()


class TestTheRegisteredConstants(unittest.TestCase):
    """The frozen numbers. A change here is a protocol deviation, not a refactor."""

    def test_the_smallest_actionable_effect_is_frozen_at_one_tenth(self):
        self.assertEqual(atr.DELTA, 0.10)

    def test_the_test_is_one_sided_at_five_percent(self):
        self.assertEqual(atr.ALPHA, 0.05)
        self.assertEqual(atr.ALTERNATIVE, "less")

    def test_the_interval_is_the_equivalence_consistent_ninety_percent(self):
        # TOST convention at a one-sided 0.05 test. A 95% interval here would
        # make the equivalence arm unreachable by construction.
        self.assertEqual(atr.CI_LEVEL, 0.90)

    def test_the_confirmation_population_is_the_briefed_one(self):
        self.assertEqual(atr.POPULATION, "briefed")

    def test_the_discovery_cutoff_is_the_july_freeze(self):
        self.assertEqual(atr.DISCOVERY_CUTOFF, "2026-07-05")


class TestTheDecisionRule(unittest.TestCase):
    """PASS requires BOTH conditions; the floor is the binding one."""

    def _decide(self, beta, p_value, ci=(-0.30, -0.05)):
        return atr.decide(beta=beta, p_value=p_value, ci=ci)

    def test_a_significant_slope_past_the_floor_promotes(self):
        self.assertTrue(self._decide(-0.19, 0.004).promoted)

    def test_a_significant_slope_short_of_the_floor_does_not_promote(self):
        # The case that shows the floor is not decoration: p clears, the
        # magnitude does not.
        decision = self._decide(-0.095, 0.041)
        self.assertFalse(decision.promoted)

    def test_a_slope_past_the_floor_that_is_not_significant_does_not_promote(self):
        self.assertFalse(self._decide(-0.25, 0.12).promoted)

    def test_a_slope_exactly_at_the_floor_promotes(self):
        # The boundary is inclusive, stated so nobody has to infer it later.
        self.assertTrue(self._decide(-atr.DELTA, 0.01).promoted)

    def test_a_p_value_exactly_at_alpha_does_not_promote(self):
        self.assertFalse(self._decide(-0.25, atr.ALPHA).promoted)

    def test_a_positive_slope_never_promotes(self):
        # Direction was fixed at registration. A large POSITIVE effect is not a
        # finding under this registration, it is a refutation.
        self.assertFalse(self._decide(+0.40, 0.001).promoted)


class TestTheConclusionLanguage(unittest.TestCase):
    """The three-way wording, pre-committed so a null cannot be spun either way."""

    def test_a_promotion_reads_as_cleared(self):
        self.assertEqual(
            atr.decide(beta=-0.20, p_value=0.002, ci=(-0.31, -0.09)).conclusion, "cleared"
        )

    def test_a_null_whose_interval_still_covers_the_bound_is_inconclusive(self):
        decision = atr.decide(beta=-0.02, p_value=0.44, ci=(-0.13, +0.09))
        self.assertEqual(decision.conclusion, "inconclusive")

    def test_a_null_whose_interval_clears_the_bound_is_evidence_against(self):
        decision = atr.decide(beta=-0.01, p_value=0.46, ci=(-0.06, +0.04))
        self.assertEqual(decision.conclusion, "evidence-against")

    def test_an_interval_touching_the_bound_is_not_evidence_against(self):
        # Strict containment. An interval whose endpoint IS the bound has not
        # excluded it.
        decision = atr.decide(beta=0.0, p_value=0.50, ci=(-atr.DELTA, +0.05))
        self.assertEqual(decision.conclusion, "inconclusive")

    def test_the_equivalence_arm_cannot_be_reached_by_a_promotion(self):
        # A cleared result never reports "evidence against actionable effects",
        # whatever its interval does.
        decision = atr.decide(beta=-0.20, p_value=0.002, ci=(-0.05, +0.05))
        self.assertEqual(decision.conclusion, "cleared")


class TestTheLiveTiltMapping(unittest.TestCase):
    """The July kill line folded in: three outcomes for the LIVE scorer tilt."""

    def test_a_promotion_leaves_the_live_tilt_confirmed(self):
        self.assertEqual(atr.decide(beta=-0.22, p_value=0.001, ci=(-0.33, -0.11)).tilt, "confirmed")

    def test_a_negative_slope_short_of_promotion_leaves_the_tilt_unchanged(self):
        # Cluster 1 retires for SELECTION, but the July kill trigger (a sign
        # flip) did not fire, so the live tilt keeps running. Collapsing this
        # into "retire everything" would revert a live scorer on evidence its
        # own registration does not call a failure.
        self.assertEqual(atr.decide(beta=-0.06, p_value=0.19, ci=(-0.17, +0.05)).tilt, "unchanged")

    def test_a_positive_slope_retires_the_live_tilt(self):
        self.assertEqual(atr.decide(beta=+0.04, p_value=0.78, ci=(-0.07, +0.15)).tilt, "retired")

    def test_a_zero_slope_retires_the_live_tilt(self):
        # The July trigger is "rho <= 0", not "rho < 0".
        self.assertEqual(atr.decide(beta=0.0, p_value=0.50, ci=(-0.11, +0.11)).tilt, "retired")


class TestTheClusterVerdict(unittest.TestCase):
    """Anything that is not a promotion retires cluster 1 for selection."""

    def test_a_promotion_promotes_the_cluster(self):
        self.assertEqual(
            atr.decide(beta=-0.21, p_value=0.003, ci=(-0.32, -0.10)).cluster, "PROMOTE"
        )

    def test_an_inconclusive_result_still_retires_the_cluster(self):
        self.assertEqual(atr.decide(beta=-0.04, p_value=0.30, ci=(-0.15, +0.07)).cluster, "RETIRE")

    def test_evidence_against_retires_the_cluster(self):
        self.assertEqual(atr.decide(beta=-0.01, p_value=0.46, ci=(-0.06, +0.04)).cluster, "RETIRE")


def _sessions(n: int, start: dt.date = dt.date(2026, 7, 6)) -> list[str]:
    from alphalens_pipeline.paper.calendar import advance_trading_sessions

    return [advance_trading_sessions(start, i).isoformat() for i in range(n)]


class _Store:
    """Two directories of per-date parquets shaped like the real stores."""

    def __init__(self, root: Path):
        self.labels = root / "selection_labels"
        self.briefs = root / "thematic_briefs"
        self.labels.mkdir(parents=True)
        self.briefs.mkdir(parents=True)

    def write(
        self,
        brief_date: str,
        rows: list[tuple[str, float, float]],
        *,
        anchor: str | None = None,
        status: str = "ok",
        briefed: bool = True,
        regime: str = "low",
    ) -> None:
        anchor = anchor or brief_date
        labels = pd.DataFrame(
            {
                "brief_date": brief_date,
                "ticker": [t for t, _, _ in rows],
                "anchor_session": anchor,
                "briefed_any_theme": briefed,
                "sel_label_status_20": status,
                "sel_ar_20": [y for _, y, _ in rows],
            }
        )
        labels.to_parquet(self.labels / f"{brief_date}.parquet", index=False)
        briefs = pd.DataFrame(
            {
                "ticker": [t for t, _, _ in rows],
                "technical_atr_pct": [x for _, _, x in rows],
                "technical_ma50_distance_pct": np.linspace(-3.0, 3.0, len(rows)),
                "n_gates_passed": [i % 3 for i in range(len(rows))],
                "market_state": regime,
            }
        )
        briefs.to_parquet(self.briefs / f"{brief_date}.parquet", index=False)


class TestThePanel(unittest.TestCase):
    """What enters the registered panel, and what must never."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.store = _Store(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _fill(self, n_clusters: int, per_cluster: int = 12, **kwargs):
        rng = np.random.default_rng(7)
        for i, session in enumerate(_sessions(n_clusters)):
            rows = [
                (f"T{i:03d}X{j:03d}", float(rng.normal()), float(rng.normal() + 5.0))
                for j in range(per_cluster)
            ]
            self.store.write(session, rows, **kwargs)

    def test_a_healthy_store_yields_one_row_per_episode(self):
        self._fill(32, per_cluster=12)
        panel = atr.held_out_panel(self.store.labels, self.store.briefs)
        self.assertEqual(panel["arrival"].nunique(), 32)
        self.assertEqual(len(panel), 32 * 12)

    def test_burnt_dates_never_enter(self):
        # The refutation control for the held-out filter: without it this file
        # would be silently pooled into the confirmation.
        self._fill(32)
        self.store.write("2026-06-30", [("BURNT", 0.5, 4.0)])
        panel = atr.held_out_panel(self.store.labels, self.store.briefs)
        self.assertNotIn("BURNT", set(panel["ticker"]))

    def test_an_unresolved_horizon_never_enters(self):
        self._fill(32)
        self.store.write("2026-08-31", [("PENDING", 0.4, 4.0)], status="split_unchecked")
        panel = atr.held_out_panel(self.store.labels, self.store.briefs)
        self.assertNotIn("PENDING", set(panel["ticker"]))

    def test_a_name_the_brief_did_not_carry_never_enters(self):
        self._fill(32)
        self.store.write("2026-09-01", [("SHADOW", 0.4, 4.0)], briefed=False)
        panel = atr.held_out_panel(self.store.labels, self.store.briefs)
        self.assertNotIn("SHADOW", set(panel["ticker"]))


class TestWhenTheRunIsVoid(unittest.TestCase):
    """A run that cannot be computed as registered returns the look unspent."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.store = _Store(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _fill(self, n_clusters: int, per_cluster: int):
        rng = np.random.default_rng(9)
        for i, session in enumerate(_sessions(n_clusters)):
            rows = [
                (f"T{i:03d}X{j:03d}", float(rng.normal()), float(rng.normal() + 5.0))
                for j in range(per_cluster)
            ]
            self.store.write(session, rows)

    def test_a_non_finite_statistic_voids_instead_of_deciding(self):
        # The defect this pins: NaN <= -DELTA is False and NaN < 0 is False, so
        # a numerical failure used to fall through to tilt="retired" — a bug
        # reported as "the July kill trigger fired, revert the live scorer".
        for beta, p_value, ci in (
            (float("nan"), 0.01, (-0.3, -0.1)),
            (-0.2, float("nan"), (-0.3, -0.1)),
            (-0.2, 0.01, (float("nan"), -0.1)),
            (float("inf"), 0.01, (-0.3, -0.1)),
        ):
            with self.subTest(beta=beta, p_value=p_value, ci=ci), self.assertRaises(atr.VoidError):
                atr.decide(beta=beta, p_value=p_value, ci=ci)

    def test_a_degenerate_signal_column_voids(self):
        # A zero-variance column standardises to zeros, and the shared OLS uses
        # a pseudo-inverse, so this does NOT crash: it silently returns a slope
        # of 0, which the rule would read as a sign flip.
        rng = np.random.default_rng(11)
        for i, session in enumerate(_sessions(32)):
            rows = [(f"T{i:03d}X{j:03d}", float(rng.normal()), 4.0) for j in range(12)]
            self.store.write(session, rows)
        with self.assertRaises(atr.VoidError):
            atr.held_out_panel(self.store.labels, self.store.briefs)

    def test_too_few_episodes_voids(self):
        self._fill(31, per_cluster=2)  # 62 episodes, clusters fine
        with self.assertRaises(atr.VoidError):
            atr.held_out_panel(self.store.labels, self.store.briefs)

    def test_too_few_clusters_voids(self):
        self._fill(8, per_cluster=60)  # 480 episodes, only 8 clusters
        with self.assertRaises(atr.VoidError):
            atr.held_out_panel(self.store.labels, self.store.briefs)

    def test_an_empty_store_voids_rather_than_returning_an_empty_panel(self):
        with self.assertRaises(atr.VoidError):
            atr.held_out_panel(self.store.labels, self.store.briefs)

    def test_a_void_prints_no_statistic_before_it_refuses(self):
        # If the slope were printed and THEN the run voided, the look would be
        # spent while the output claimed it was not. The verdict must be
        # decided before any feature-vs-outcome number reaches the screen.
        import contextlib
        import io

        rng = np.random.default_rng(13)
        for i, session in enumerate(_sessions(32)):
            rows = [
                (f"T{i:03d}X{j:03d}", float(rng.normal()), float(rng.normal() + 5.0))
                for j in range(12)
            ]
            self.store.write(session, rows)

        original = atr.atr_slope
        setattr(atr, "atr_slope", lambda _panel: float("nan"))  # noqa: B010
        self.addCleanup(setattr, atr, "atr_slope", original)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), self.assertRaises(atr.VoidError):
            atr.full_run(self.store.labels, self.store.briefs)
        printed = buffer.getvalue()
        self.assertNotIn("partial slope", printed)
        self.assertNotIn("bootstrap p", printed)

    def test_a_void_is_not_a_decision(self):
        # Stated as a test because the whole point of VOID is that it must not
        # reach `decide` and become a RETIRE.
        self.assertFalse(
            issubclass(atr.VoidError, type(atr.decide(beta=0.0, p_value=1.0, ci=(0, 0))))
        )


class TestTheEstimator(unittest.TestCase):
    """The slope, its one-sided p and its interval, on data with a known answer."""

    def _panel(self, beta: float, *, seed: int = 3, n_clusters: int = 35, per: int = 12):
        rng = np.random.default_rng(seed)
        rows = []
        for i, session in enumerate(_sessions(n_clusters)):
            shock = rng.normal(scale=0.3)
            for j in range(per):
                x = float(rng.normal())
                rows.append(
                    {
                        "arrival": session,
                        "ticker": f"T{i:03d}X{j:03d}",
                        "sel_ar_20": beta * x + shock + float(rng.normal()),
                        "technical_atr_pct": x,
                        "technical_ma50_distance_pct": float(rng.normal()),
                        "n_gates_passed": float(rng.integers(0, 3)),
                    }
                )
        return pd.DataFrame(rows)

    def test_a_planted_negative_slope_is_recovered(self):
        slope = atr.atr_slope(self._panel(-0.5))
        self.assertLess(slope, -0.3)

    def test_the_point_estimate_and_the_bootstrap_share_one_fit(self):
        # The printed slope and the tested slope must be the SAME number. Two
        # solvers agree on a full-rank design and diverge on a rank-deficient
        # one, so the registration uses the bootstrap's own fit for both.
        from alphalens_research.diagnostics.options_retro import _ols_beta

        panel = self._panel(-0.4)
        y, X = atr._design(panel)
        beta, _ = _ols_beta(y, X)
        self.assertEqual(atr.atr_slope(panel), float(beta[1]))

    def test_the_interval_does_not_depend_on_the_order_clusters_are_listed(self):
        # With a fixed seed the draw maps integers onto the group list, so a
        # first-appearance ordering would give a different interval for the same
        # data. Compared to 12 places, not exactly: summing a group's rows in a
        # different order moves the last bit or two, and that is arithmetic
        # rather than an ordering dependence.
        panel = self._panel(-0.3)
        shuffled = panel.sample(frac=1.0, random_state=7).reset_index(drop=True)
        first = atr.slope_ci(panel, n_boot=199, seed=8)
        second = atr.slope_ci(shuffled, n_boot=199, seed=8)
        for a, b in zip(first, second, strict=True):
            self.assertAlmostEqual(a, b, places=12)

    def test_a_planted_negative_slope_gives_a_small_one_sided_p(self):
        panel = self._panel(-0.5)
        self.assertLess(atr.one_sided_p(panel, n_boot=299, seed=1), 0.01)

    def test_a_planted_POSITIVE_slope_gives_a_large_one_sided_p(self):
        # The refutation control. Without the one-sided direction this would
        # also come back tiny, and the registered sign would mean nothing.
        panel = self._panel(+0.5)
        self.assertGreater(atr.one_sided_p(panel, n_boot=299, seed=1), 0.9)

    def test_a_null_panel_gives_a_large_one_sided_p(self):
        panel = self._panel(0.0)
        self.assertGreater(atr.one_sided_p(panel, n_boot=299, seed=2), 0.05)

    def test_the_interval_brackets_the_planted_slope(self):
        panel = self._panel(-0.5)
        lo, hi = atr.slope_ci(panel, n_boot=199, seed=4)
        self.assertLess(lo, atr.atr_slope(panel))
        self.assertGreater(hi, atr.atr_slope(panel))

    def test_the_interval_is_deterministic_given_the_seed(self):
        panel = self._panel(-0.3)
        self.assertEqual(
            atr.slope_ci(panel, n_boot=199, seed=5), atr.slope_ci(panel, n_boot=199, seed=5)
        )


class TestTheVolatilityInteractionSufficiency(unittest.TestCase):
    """Run the check mechanically and DISCLOSE insufficiency, never drop it."""

    def _panel(self, regimes: list[str]):
        return pd.DataFrame(
            {
                "arrival": _sessions(len(regimes)),
                "ticker": [f"T{i}" for i in range(len(regimes))],
                "market_state": regimes,
            }
        )

    def test_one_regime_state_is_insufficient(self):
        episodes, estimable = atr.volatility_sufficiency(self._panel(["low"] * 30))
        self.assertEqual(episodes, 1)
        self.assertFalse(estimable)

    def test_two_regime_episodes_are_still_insufficient(self):
        panel = self._panel(["low"] * 15 + ["high"] * 15)
        episodes, estimable = atr.volatility_sufficiency(panel)
        self.assertEqual(episodes, 2)
        self.assertFalse(estimable)

    def test_enough_independent_regime_episodes_is_estimable(self):
        panel = self._panel(["low"] * 8 + ["high"] * 8 + ["low"] * 8 + ["high"] * 8)
        episodes, estimable = atr.volatility_sufficiency(panel)
        self.assertEqual(episodes, 4)
        self.assertTrue(estimable)

    def test_the_per_regime_cluster_floor_is_a_frozen_constant(self):
        # It used to be computed inline as MIN_CLUSTERS // 3, which hides a
        # number the registration's reader should be able to see.
        self.assertIsInstance(atr.MIN_REGIME_CLUSTERS, int)
        self.assertGreater(atr.MIN_REGIME_CLUSTERS, 0)

    def test_a_regime_row_with_no_label_does_not_count_as_an_episode(self):
        panel = self._panel(["low"] * 10 + ["high"] * 10)
        panel.loc[5, "market_state"] = np.nan
        episodes, _ = atr.volatility_sufficiency(panel)
        self.assertEqual(episodes, 2)

    def test_a_missing_regime_column_is_insufficient_not_a_crash(self):
        panel = pd.DataFrame({"arrival": _sessions(5), "ticker": list("ABCDE")})
        episodes, estimable = atr.volatility_sufficiency(panel)
        self.assertEqual(episodes, 0)
        self.assertFalse(estimable)


class TestTheRunDateRail(unittest.TestCase):
    """The registration must be merged before the look can run."""

    def test_running_before_the_registration_date_is_refused(self):
        self.assertIsNotNone(atr.refuse_early_run(atr.RUN_NOT_BEFORE - dt.timedelta(days=1)))

    def test_running_on_the_registration_date_is_allowed(self):
        self.assertIsNone(atr.refuse_early_run(atr.RUN_NOT_BEFORE))

    def test_running_after_the_registration_date_is_allowed(self):
        self.assertIsNone(atr.refuse_early_run(atr.RUN_NOT_BEFORE + dt.timedelta(days=30)))


if __name__ == "__main__":
    unittest.main()
