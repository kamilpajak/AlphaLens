"""Tests for the §10.2 missingness instrumentation (#1115, memo §7).

The stored parquet records only the ``None`` — never the reason — so the flow
table requires re-running the bracket-lens constructibility with reason
capture. The classifier here mirrors the DECISION ORDER of the production code
path (``replay_ladder_atr_bracket`` -> ``atr_bracket_levels``), and every
classification is verifiable against the real lens: ``classify_and_verify``
runs both and reports agreement, so the mirror cannot drift silently.
"""

from __future__ import annotations

import unittest

import pandas as pd
from alphalens_research.diagnostics import exit_policy_missingness as epm

_MIN = 60_000


def _bars(*lhc: tuple[float, float, float]) -> list[dict]:
    return [
        {"t": i * _MIN, "l": low, "h": high, "c": close} for i, (low, high, close) in enumerate(lhc)
    ]


def _setup(
    *,
    entries: list[tuple[float, float]] | None = None,
    tps: list[tuple[float, float]] | None = None,
    stop: float | None = 90.0,
    atr: float | None = 4.0,
    asof_close: float | None = None,
) -> dict:
    setup: dict = {
        "status": "OK",
        "disaster_stop": stop,
        "entry_tiers": [
            {"limit": lim, "alloc_pct": pct}
            for lim, pct in (entries if entries is not None else [(100.0, 100.0)])
        ],
        "tp_tranches": [
            {"target": tgt, "tranche_pct": pct}
            for tgt, pct in (tps if tps is not None else [(110.0, 100.0)])
        ],
        "atr": atr,
    }
    if asof_close is not None:
        setup["asof_close"] = asof_close
    return setup


_TOUCHING_BARS = _bars((100.0, 100.0, 100.0), (99.0, 101.0, 100.5))
_NO_TOUCH_BARS = _bars((101.0, 102.0, 101.5), (101.5, 103.0, 102.0))


class TestArmBNullReason(unittest.TestCase):
    def test_constructible_row_has_no_reason(self):
        self.assertIsNone(epm.arm_b_null_reason(_setup(), _TOUCHING_BARS, pct_off_52w_high=None))

    def test_each_reason_in_code_order(self):
        cases = (
            ({"status": "BAD"}, _TOUCHING_BARS, None, "setup_not_ok_or_missing_stop"),
            (_setup(stop=None), _TOUCHING_BARS, None, "setup_not_ok_or_missing_stop"),
            (_setup(), [], None, "no_bars"),
            (_setup(atr=None), _TOUCHING_BARS, None, "atr_missing_or_nonpositive"),
            (_setup(atr=-1.0), _TOUCHING_BARS, None, "atr_missing_or_nonpositive"),
            (_setup(), _NO_TOUCH_BARS, None, "no_fill_walk1"),
            (_setup(atr=80.0), _TOUCHING_BARS, None, "bracket_stop_nonpositive"),
            # ceiling at/below the anchor's cost floor: 52w high ~ asof_close
            # with pct 0 -> ceiling 100 <= 100 * 1.006.
            (_setup(asof_close=100.0), _TOUCHING_BARS, 0.0, "ceiling_at_or_below_cost_floor"),
        )
        for setup, bars, pct, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(epm.arm_b_null_reason(setup, bars, pct_off_52w_high=pct), expected)

    def test_reason_priority_follows_the_leaf_not_the_table(self):
        # A row that is BOTH stop-degenerate and ceiling-capped must classify
        # as bracket_stop_nonpositive — atr_bracket_levels checks the stop
        # first, and the classifier mirrors the CODE, not the memo table's
        # presentation order.
        setup = _setup(atr=80.0, asof_close=100.0)
        self.assertEqual(
            epm.arm_b_null_reason(setup, _TOUCHING_BARS, pct_off_52w_high=0.0),
            "bracket_stop_nonpositive",
        )


class TestClassifyAndVerify(unittest.TestCase):
    def test_agreement_with_the_real_lens_on_both_verdicts(self):
        # Null-reason present  <=> the production lens returns None. Verified
        # here on one constructible and one degenerate row; the script asserts
        # the same agreement on EVERY historical row it classifies.
        for setup, bars, pct in (
            (_setup(), _TOUCHING_BARS, None),
            (_setup(atr=80.0), _TOUCHING_BARS, None),
            (_setup(), _NO_TOUCH_BARS, None),
        ):
            with self.subTest():
                verdict = epm.classify_and_verify(setup, bars, pct_off_52w_high=pct)
                self.assertTrue(verdict.agrees, verdict)
                self.assertEqual(verdict.lens_value is None, verdict.reason is not None)


class TestFlowTable(unittest.TestCase):
    def test_levels_balance_to_the_total(self):
        # Includes a terminal NO_FILL row without an arm-A value — the store
        # holds 16 such rows in the historical span, so the table carries an
        # explicit level for them rather than refusing.
        rows = pd.DataFrame(
            {
                "plannable": [True, True, True, True, True, False],
                "terminal": [True, True, True, True, False, False],
                "arm_a_present": [True, True, True, False, False, False],
                "ladder_classification": [None, None, None, "NO_FILL", None, None],
                "arm_b_reason": [None, "no_bars", "no_fill_walk1", None, None, None],
            }
        )
        table = epm.flow_table(rows)
        counts = dict(table)
        self.assertEqual(counts["all rows in span"], 6)
        self.assertEqual(counts["dropped: plannable = False / NO_STRUCTURE"], 1)
        self.assertEqual(counts["dropped: not terminal at the read"], 1)
        self.assertEqual(counts["terminal: NO_FILL (no arm A value)"], 1)
        self.assertEqual(counts["terminal: arm A value present"], 3)
        self.assertEqual(counts["terminal: both arms present"], 1)
        self.assertEqual(
            counts["dropped: plannable = False / NO_STRUCTURE"]
            + counts["dropped: not terminal at the read"]
            + counts["terminal: NO_FILL (no arm A value)"]
            + counts["terminal: arm A value present"],
            counts["all rows in span"],
        )
        null_total = sum(n for level, n in table if level.startswith("terminal, arm B null:"))
        self.assertEqual(
            null_total + counts["terminal: both arms present"],
            counts["terminal: arm A value present"],
        )

    def test_unbalanced_input_raises(self):
        # A terminal row without an arm-A value that is NOT a NO_FILL is a
        # genuine store-shape violation the table must refuse.
        rows = pd.DataFrame(
            {
                "plannable": [True],
                "terminal": [True],
                "arm_a_present": [False],
                "ladder_classification": ["SL_HIT"],
                "arm_b_reason": [None],
            }
        )
        with self.assertRaises(ValueError):
            epm.flow_table(rows)


class TestDayClusterMeanDiff(unittest.TestCase):
    def test_recovers_a_known_group_difference_and_is_seed_deterministic(self):
        # Per-day means differ so the day-cluster CI is strictly wider than
        # the point estimate (a homogeneous fixture would collapse it).
        rows = pd.DataFrame(
            {
                "day": ["d1", "d1", "d2", "d2", "d3", "d3"],
                "is_null": [True, False] * 3,
                "y": [1.0, 3.0, 0.0, 4.0, 2.0, 2.0],
            }
        )
        first = epm.day_cluster_mean_diff(rows, indicator="is_null", y="y", day="day")
        second = epm.day_cluster_mean_diff(rows, indicator="is_null", y="y", day="day")
        self.assertAlmostEqual(first.diff, -2.0, places=9)  # null-group mean minus rest
        self.assertEqual(first.ci_low, second.ci_low)
        self.assertEqual(first.ci_high, second.ci_high)
        self.assertLess(first.ci_low, first.diff)
        self.assertGreater(first.ci_high, first.diff)

    def test_never_computes_an_a_vs_b_contrast(self):
        # §7.2: the diagnostic sees ONE outcome column. The API accepts no
        # second arm anywhere — pinned at the source level.
        import inspect

        src = inspect.getsource(epm)
        self.assertNotIn("net_b", src)
        self.assertNotIn("ARM_B", src)


class TestUnknownReasonRefused(unittest.TestCase):
    def test_flow_table_refuses_a_reason_outside_the_catalog(self):
        # A future leaf arm produces the sentinel; the table must refuse
        # loudly rather than let the row vanish from every level.
        rows = pd.DataFrame(
            {
                "plannable": [True],
                "terminal": [True],
                "arm_a_present": [True],
                "ladder_classification": [None],
                "arm_b_reason": ["leaf_rejected_for_unknown_reason"],
            }
        )
        with self.assertRaises(ValueError):
            epm.flow_table(rows)


class TestBriefAbsentPath(unittest.TestCase):
    def test_missing_setup_classifies_with_true_agreement(self):
        verdict = epm.classify_and_verify(None, _TOUCHING_BARS, pct_off_52w_high=None)
        self.assertEqual(verdict.reason, "setup_not_ok_or_missing_stop")
        self.assertTrue(verdict.agrees)


class TestMalformedBarsFile(unittest.TestCase):
    def test_wrong_columns_classify_as_no_bars(self):
        import tempfile
        from pathlib import Path

        from scripts import exit_policy_missingness as script

        with tempfile.TemporaryDirectory() as tmp:
            bars_dir = Path(tmp) / "bars"
            bars_dir.mkdir()
            pd.DataFrame({"x": [1]}).to_parquet(bars_dir / "TST_2026-08-20.parquet")
            original = script.STORE_DIR
            script.STORE_DIR = Path(tmp)
            try:
                self.assertEqual(script._bars_for("TST", "2026-08-20"), [])
            finally:
                script.STORE_DIR = original


class TestStoredBracketNull(unittest.TestCase):
    def test_reads_the_stamped_lens_nullness(self):
        cases = (
            ('{"atr_bracket_1p5": 0.7}', False),
            ('{"atr_bracket_1p5": null}', True),
            ('{"be_0p5r": 0.1}', True),  # key absent = never stamped
            (None, True),
            ("not json", True),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(epm.stored_bracket_null(payload), expected)


class TestArtifactGuard(unittest.TestCase):
    def test_refuses_the_production_ladder_store(self):
        from pathlib import Path

        with self.assertRaises(SystemExit):
            epm.ensure_artifact_dir(Path.home() / ".alphalens" / "population_ladders")


class TestDriverEndToEnd(unittest.TestCase):
    """classify_span + main over a synthetic three-row store in a tmp dir —
    covers the join, the classification wiring, the payload assembly and the
    --write artifact path without touching any real store."""

    def _build_store(self, root):
        import json as _json
        from pathlib import Path

        store = Path(root) / "population_ladders"
        (store / "bars").mkdir(parents=True)
        briefs = Path(root) / "thematic_briefs"
        briefs.mkdir()
        day = "2026-06-02"
        pd.DataFrame(
            {
                "ticker": ["AAA", "BBB", "CCC"],
                "plannable": [False, True, True],
                "terminal": [False, True, True],
                "realized_r": [None, None, 0.5],
                "ladder_classification": [None, "NO_FILL", "TP_FULL"],
                "breakeven_realized_r_json": [None, None, _json.dumps({"atr_bracket_1p5": 0.4})],
            }
        ).to_parquet(store / f"{day}.parquet", index=False)
        setup = _setup()
        pd.DataFrame(
            {
                "ticker": ["BBB", "CCC"],
                "brief_trade_setup": [_json.dumps(setup), _json.dumps(setup)],
                "technical_pct_off_52w_high": [None, None],
            }
        ).to_parquet(briefs / f"{day}.parquet", index=False)
        pd.DataFrame(_TOUCHING_BARS).to_parquet(store / "bars" / f"CCC_{day}.parquet", index=False)
        return store, briefs, day

    def test_classify_span_and_main_write(self):
        import io
        import json as _json
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path
        from unittest import mock

        from scripts import exit_policy_missingness as script

        with tempfile.TemporaryDirectory() as tmp:
            store, briefs, day = self._build_store(tmp)
            artifact = Path(tmp) / "artifact"
            with (
                mock.patch.object(script, "STORE_DIR", store),
                mock.patch.object(script, "BRIEFS_DIR", briefs),
                mock.patch.object(script, "ARTIFACT_DIR", artifact),
                mock.patch.object(
                    script.sys,
                    "argv",
                    ["x", "--span-start", day, "--span-end", day, "--write", "--json"],
                ),
            ):
                frame = script.classify_span(day, day)
                self.assertEqual(len(frame), 3)
                ccc = frame[frame["ticker"] == "CCC"].iloc[0]
                self.assertIsNone(ccc["arm_b_reason"])  # constructible, fills
                self.assertTrue(ccc["mirror_agrees_with_lens"])
                out = io.StringIO()
                with redirect_stdout(out):
                    code = script.main()
            self.assertEqual(code, 0)
            payload = _json.loads(out.getvalue())
            counts = dict(map(tuple, payload["flow_table"]))
            self.assertEqual(counts["all rows in span"], 3)
            self.assertEqual(counts["terminal: NO_FILL (no arm A value)"], 1)
            self.assertEqual(counts["terminal: both arms present"], 1)
            self.assertEqual(payload["mirror_vs_lens_agreement"]["rows_disagreeing"], 0)
            written = list(artifact.iterdir())
            self.assertEqual(sorted(p.suffix for p in written), [".json", ".parquet"])

    def test_main_returns_one_on_an_empty_span(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from scripts import exit_policy_missingness as script

        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "population_ladders"
            (empty / "bars").mkdir(parents=True)
            with (
                mock.patch.object(script, "STORE_DIR", empty),
                mock.patch.object(script.sys, "argv", ["x"]),
            ):
                self.assertEqual(script.main(), 1)


if __name__ == "__main__":
    unittest.main()
