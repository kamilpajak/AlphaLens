"""The index-return backfill must be ADDITIVE, and provably so.

It rewrites PUBLISHED brief parquets, which the #1482 publication freeze
otherwise forbids. The licence for that rests entirely on the rewrite changing
none of the four things the freeze protects — the list, the order, the prose and
the trade levels — so these tests are the proof, not a formality:

* every pre-existing column survives with the SAME Arrow field (name, type,
  nullability) and the SAME values, NaNs included, and the row count is
  unchanged;
* the schema check has real power. One test builds a fixture that mixes
  ``string`` and ``large_string``, which is exactly what a ``pd.read_parquet``
  /``to_parquet`` round trip silently rewrites on the older brief dates;
* the refusal path FIRES. A corrupted column must abort the whole run and leave
  every published file untouched, not land 162 of 163 dates.

The store is seeded, never the real one: this is a laptop and the real grouped
store on it ends months before the brief dates (memory ``ref_local_mirror``).
"""

import datetime as dt
import importlib.util
import math
import sys
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from alphalens_pipeline.market.calendar import n_sessions_before

_ANCHOR = dt.date(2026, 3, 20)
_SECOND = dt.date(2026, 3, 19)
_CLOSES = {
    "SPY": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 99.0],
    "QQQ": [200.0, 201.0, 202.0, 203.0, 204.0, 205.0, 194.0],
    "IWM": [50.0, 49.0, 48.0, 47.0, 46.0, 45.0, 51.0],
}
# Seven sessions on disk, so BOTH brief dates have a full six-session window.
_EXPECTED_ANCHOR = {
    "market_ret5_spy": 99.0 / 101.0 - 1.0,
    "market_ret5_qqq": 194.0 / 201.0 - 1.0,
    "market_ret5_iwm": 51.0 / 49.0 - 1.0,
}
_EXPECTED_SECOND = {
    "market_ret5_spy": 105.0 / 100.0 - 1.0,
    "market_ret5_qqq": 205.0 / 200.0 - 1.0,
    "market_ret5_iwm": 45.0 / 50.0 - 1.0,
}


def _seed_store(root: Path) -> list[dt.date]:
    from alphalens_pipeline.data.rs_history import write_grouped_day_atomic

    sessions = [n_sessions_before(_ANCHOR, k) for k in range(6, -1, -1)]
    for i, d in enumerate(sessions):
        payload = {
            tkr: {"t": 0, "o": c[i], "h": c[i], "l": c[i], "c": c[i], "v": 10, "vw": c[i]}
            for tkr, c in _CLOSES.items()
        }
        write_grouped_day_atomic(root, d, payload)
    return sessions


def _brief_table(rows: int = 3, *, with_large_string: bool = True) -> pa.Table:
    """A brief-shaped table: text, floats with NaN, a bool, a nested list.

    ``large_string`` is included on purpose — it is the Arrow type a pandas
    round trip rewrites ``string`` into on the older brief dates, so a fixture
    carrying both is what gives the schema check power.
    """
    text_type = pa.large_string() if with_large_string else pa.string()
    schema = pa.schema(
        [
            pa.field("ticker", pa.string()),
            pa.field("theme", text_type),
            pa.field("rationale", text_type),
            pa.field("selection_score", pa.float64()),
            pa.field("market_state", pa.string()),
            pa.field("market_state_squeeze_on", pa.bool_()),
            pa.field("rank_in_day", pa.int64()),
            pa.field("also_in_themes", pa.list_(pa.string())),
        ]
    )
    return pa.table(
        {
            "ticker": [f"T{i}" for i in range(rows)],
            "theme": ["ai-infra"] * rows,
            "rationale": ["prose that must not move"] * rows,
            # A NaN in the fixture is load-bearing: pyarrow's own ``equals``
            # reports NaN != NaN, so a check built on it would flag an untouched
            # column as changed.
            "selection_score": [0.5, float("nan"), 0.25][:rows],
            "market_state": ["bull_quiet"] * rows,
            "market_state_squeeze_on": [True, None, False][:rows],
            "rank_in_day": list(range(1, rows + 1)),
            "also_in_themes": [["a", "b"], [], None][:rows],
        },
        schema=schema,
    )


def _write_brief(briefs_dir: Path, date: dt.date, table: pa.Table) -> Path:
    path = briefs_dir / f"{date.isoformat()}.parquet"
    pq.write_table(table, path, compression="snappy")
    return path


# The script lives under scripts/ (not an importable package) — load it by path,
# the same way tests/test_backfill_grouped_daily_history.py does. A
# `from scripts...` import resolves only under `unittest discover -t .`, which
# left this file's own `__main__` block unable to run it at all.
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_market_index_series.py"
_spec = importlib.util.spec_from_file_location("backfill_market_index_series", _SCRIPT)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
sys.modules["backfill_market_index_series"] = mod
_spec.loader.exec_module(mod)


class _Env:
    """A seeded grouped store plus a briefs dir holding the two brief dates."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.store = base / "grouped"
        self.store.mkdir()
        self.briefs = base / "briefs"
        self.briefs.mkdir()
        _seed_store(self.store)
        self.table = _brief_table()
        self.paths = {d: _write_brief(self.briefs, d, self.table) for d in (_SECOND, _ANCHOR)}

    def cleanup(self):
        self._tmp.cleanup()

    def read(self, date: dt.date) -> pa.Table:
        return pq.read_table(self.paths[date])


class TestTheBackfillIsAdditive(unittest.TestCase):
    def setUp(self):
        self.env = _Env()
        self.addCleanup(self.env.cleanup)

    def test_it_adds_exactly_the_three_columns_with_the_computed_values(self):
        run_backfill = mod.run_backfill

        report = run_backfill(briefs_dir=self.env.briefs, grouped_root=self.env.store, write=True)

        self.assertEqual(report["refused"], [])
        self.assertEqual(report["written"], 2)

        for date, expected in ((_ANCHOR, _EXPECTED_ANCHOR), (_SECOND, _EXPECTED_SECOND)):
            after = self.env.read(date)
            added = [c for c in after.column_names if c not in self.env.table.column_names]
            self.assertEqual(
                added, ["market_ret5_spy", "market_ret5_qqq", "market_ret5_iwm"], msg=str(date)
            )
            for col, value in expected.items():
                got = after.column(col).to_pylist()
                self.assertEqual(len(set(got)), 1, msg=f"{date} {col} not broadcast")
                self.assertAlmostEqual(got[0], value, places=12, msg=f"{date} {col}")
                self.assertEqual(after.schema.field(col).type, pa.float64(), msg=col)

    def test_every_pre_existing_field_and_value_survives_unchanged(self):
        run_backfill = mod.run_backfill

        before = self.env.read(_ANCHOR)
        run_backfill(briefs_dir=self.env.briefs, grouped_root=self.env.store, write=True)
        after = self.env.read(_ANCHOR)

        self.assertEqual(after.num_rows, before.num_rows)
        for i, field in enumerate(before.schema):
            self.assertEqual(after.schema.field(i), field, msg=field.name)
            lhs = before.column(i).to_pylist()
            rhs = after.column(i).to_pylist()
            same = all(
                (x == y)
                or (
                    isinstance(x, float)
                    and isinstance(y, float)
                    and math.isnan(x)
                    and math.isnan(y)
                )
                for x, y in zip(lhs, rhs, strict=True)
            )
            self.assertTrue(same, msg=f"{field.name}: {lhs!r} -> {rhs!r}")

    def test_a_large_string_column_keeps_its_arrow_type(self):
        # The discriminator against a pandas round trip: measured, read_parquet
        # + to_parquet rewrites `string` as `large_string` on 6 of 112 real brief
        # parquets. A fixture carrying BOTH types fails under that writer.
        run_backfill = mod.run_backfill

        run_backfill(briefs_dir=self.env.briefs, grouped_root=self.env.store, write=True)
        after = self.env.read(_ANCHOR)
        self.assertEqual(after.schema.field("ticker").type, pa.string())
        self.assertEqual(after.schema.field("theme").type, pa.large_string())
        self.assertEqual(after.schema.field("rationale").type, pa.large_string())

    def test_a_second_run_is_idempotent(self):
        run_backfill = mod.run_backfill

        run_backfill(briefs_dir=self.env.briefs, grouped_root=self.env.store, write=True)
        first = self.env.read(_ANCHOR)
        second_report = run_backfill(
            briefs_dir=self.env.briefs, grouped_root=self.env.store, write=True
        )
        second = self.env.read(_ANCHOR)

        self.assertEqual(second_report["refused"], [])
        self.assertEqual(second.column_names, first.column_names)
        self.assertEqual(second.schema, first.schema)
        self.assertEqual(
            second.column("market_ret5_qqq").to_pylist(),
            first.column("market_ret5_qqq").to_pylist(),
        )


class TestDryRunWritesNothing(unittest.TestCase):
    def setUp(self):
        self.env = _Env()
        self.addCleanup(self.env.cleanup)

    def test_the_default_run_reports_the_plan_and_touches_no_file(self):
        run_backfill = mod.run_backfill

        before = {d: p.read_bytes() for d, p in self.env.paths.items()}
        report = run_backfill(briefs_dir=self.env.briefs, grouped_root=self.env.store)

        self.assertTrue(report["dry_run"])
        self.assertEqual(report["written"], 0)
        self.assertEqual(len(report["dates"]), 2)
        self.assertEqual(report["refused"], [])
        for date, blob in before.items():
            self.assertEqual(self.env.paths[date].read_bytes(), blob, msg=str(date))

    def test_the_plan_names_the_values_it_would_write(self):
        run_backfill = mod.run_backfill

        report = run_backfill(briefs_dir=self.env.briefs, grouped_root=self.env.store)
        by_date = {row["date"]: row for row in report["dates"]}
        self.assertAlmostEqual(
            by_date[_ANCHOR.isoformat()]["market_ret5_qqq"],
            _EXPECTED_ANCHOR["market_ret5_qqq"],
            places=12,
        )
        self.assertEqual(by_date[_ANCHOR.isoformat()]["rows"], 3)


class TestTheRefusalPathFires(unittest.TestCase):
    def setUp(self):
        self.env = _Env()
        self.addCleanup(self.env.cleanup)

    def test_a_corrupted_column_refuses_the_whole_run_and_replaces_nothing(self):
        # Corrupt ONE pre-existing cell on ONE of the two dates. The run must
        # refuse BOTH — a partial landing is what makes the additivity licence
        # unverifiable afterwards.
        before = {d: p.read_bytes() for d, p in self.env.paths.items()}
        original = mod.augment

        def _corrupting(table, values, *, date):
            out = original(table, values, date=date)
            if date == _ANCHOR:
                bad = pa.array(["CORRUPTED"] * out.num_rows, type=pa.large_string())
                out = out.set_column(out.schema.get_field_index("rationale"), "rationale", bad)
            return out

        mod.augment = _corrupting
        try:
            with self.assertRaises(mod.NotAdditiveError) as ctx:
                mod.run_backfill(
                    briefs_dir=self.env.briefs, grouped_root=self.env.store, write=True
                )
        finally:
            mod.augment = original

        self.assertIn("rationale", str(ctx.exception))
        self.assertIn(_ANCHOR.isoformat(), str(ctx.exception))
        for date, blob in before.items():
            self.assertEqual(self.env.paths[date].read_bytes(), blob, msg=str(date))

    def test_a_dropped_column_is_refused(self):
        original = mod.augment

        def _dropping(table, values, *, date):
            return original(table, values, date=date).drop_columns(["rank_in_day"])

        mod.augment = _dropping
        try:
            with self.assertRaises(mod.NotAdditiveError) as ctx:
                mod.run_backfill(
                    briefs_dir=self.env.briefs, grouped_root=self.env.store, write=True
                )
        finally:
            mod.augment = original
        self.assertIn("rank_in_day", str(ctx.exception))

    def test_a_changed_arrow_type_is_refused_even_with_equal_values(self):
        # The pandas-round-trip defect in isolation: same values, different
        # Arrow type. A values-only check would pass this.
        original = mod.augment

        def _retyping(table, values, *, date):
            out = original(table, values, date=date)
            idx = out.schema.get_field_index("theme")
            narrowed = out.column(idx).cast(pa.string())
            return out.set_column(idx, pa.field("theme", pa.string()), narrowed)

        mod.augment = _retyping
        try:
            with self.assertRaises(mod.NotAdditiveError) as ctx:
                mod.run_backfill(
                    briefs_dir=self.env.briefs, grouped_root=self.env.store, write=True
                )
        finally:
            mod.augment = original
        self.assertIn("theme", str(ctx.exception))

    def test_a_dropped_row_is_refused(self):
        original = mod.augment

        def _slicing(table, values, *, date):
            return original(table, values, date=date).slice(0, 2)

        mod.augment = _slicing
        try:
            with self.assertRaises(mod.NotAdditiveError):
                mod.run_backfill(
                    briefs_dir=self.env.briefs, grouped_root=self.env.store, write=True
                )
        finally:
            mod.augment = original


class TestTheStaleStoreRefusal(unittest.TestCase):
    """A store that does not reach the brief dates must NOT stamp a frozen trio."""

    def test_a_date_past_the_staleness_bound_is_refused_not_stamped(self):
        run_backfill = mod.run_backfill

        env = _Env()
        self.addCleanup(env.cleanup)
        # A brief date far past the newest stored session: the enricher answers
        # NaN, and the backfill must call that a refusal rather than write a
        # column of nulls over a published file.
        far = dt.date(2026, 6, 15)
        _write_brief(env.briefs, far, env.table)

        with self.assertRaises(Exception) as ctx:
            run_backfill(briefs_dir=env.briefs, grouped_root=env.store, write=True)
        self.assertIn(far.isoformat(), str(ctx.exception))
        # And nothing landed on the two good dates either.
        for date in (_SECOND, _ANCHOR):
            self.assertNotIn("market_ret5_spy", env.read(date).column_names)

    def test_allow_missing_lets_an_operator_stamp_nulls_deliberately(self):
        run_backfill = mod.run_backfill

        env = _Env()
        self.addCleanup(env.cleanup)
        far = dt.date(2026, 6, 15)
        _write_brief(env.briefs, far, env.table)

        report = run_backfill(
            briefs_dir=env.briefs,
            grouped_root=env.store,
            write=True,
            allow_missing=True,
        )
        self.assertEqual(report["refused"], [])
        self.assertEqual(report["written"], 3)
        got = pq.read_table(env.briefs / f"{far.isoformat()}.parquet")
        self.assertTrue(all(v is None for v in got.column("market_ret5_spy").to_pylist()))


class TestTheCommandLineSurface(unittest.TestCase):
    def setUp(self):
        self.env = _Env()
        self.addCleanup(self.env.cleanup)

    def _argv(self, *extra: str) -> list[str]:
        return [
            "--briefs-dir",
            str(self.env.briefs),
            "--grouped-root",
            str(self.env.store),
            *extra,
        ]

    def test_json_mode_puts_exactly_one_value_on_stdout_and_exits_zero(self):
        import contextlib
        import io
        import json

        main = mod.main

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(self._argv("--format", "json"))

        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())  # one value, or this raises
        self.assertEqual(payload["schema"], "alphalens.backfill.market_ret5/v1")
        self.assertTrue(payload["dry_run"])

    def test_a_refusal_exits_non_zero_with_an_empty_stdout(self):
        import contextlib
        import io

        main = mod.main

        _write_brief(self.env.briefs, dt.date(2026, 6, 15), self.env.table)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(self._argv("--write", "--format", "json"))

        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("2026-06-15", err.getvalue())

    def test_write_and_dry_run_together_are_a_usage_error(self):
        import contextlib
        import io

        main = mod.main

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(self._argv("--write", "--dry-run"))
        self.assertEqual(code, 2)


class TestARerunCannotSilentlyReplaceAPublishedTrio(unittest.TestCase):
    """The family was exempt from the value check, which made the one failure the
    guard exists to prevent invisible: a re-run on a host whose grouped store does
    not reach the dates would overwrite good values with a frozen trio and report
    success. That is the #1524 shape — a stale input stamped over a correct one."""

    def setUp(self):
        self.env = _Env()
        self.addCleanup(self.env.cleanup)

    def _published_once(self):
        run_backfill = mod.run_backfill

        run_backfill(briefs_dir=self.env.briefs, grouped_root=self.env.store, write=True)

    def test_a_rerun_computing_a_different_trio_is_refused(self):
        self._published_once()
        first = self.env.read(_ANCHOR).column("market_ret5_spy").to_pylist()[0]

        # Second run, same dates, but the arithmetic now answers differently.
        original = mod.five_session_index_returns
        self.addCleanup(lambda: setattr(mod, "five_session_index_returns", original))
        mod.five_session_index_returns = lambda *a, **k: {
            "market_ret5_spy": -0.4242,
            "market_ret5_qqq": -0.4242,
            "market_ret5_iwm": -0.4242,
        }

        with self.assertRaises(mod.NotAdditiveError) as caught:
            mod.run_backfill(briefs_dir=self.env.briefs, grouped_root=self.env.store, write=True)
        self.assertIn("REPLACE", str(caught.exception))
        self.assertIn("--overwrite", str(caught.exception))
        # And the published value is untouched.
        self.assertEqual(self.env.read(_ANCHOR).column("market_ret5_spy").to_pylist()[0], first)

    def test_overwrite_permits_it_deliberately(self):
        self._published_once()
        original = mod.five_session_index_returns
        self.addCleanup(lambda: setattr(mod, "five_session_index_returns", original))
        mod.five_session_index_returns = lambda *a, **k: {
            "market_ret5_spy": -0.4242,
            "market_ret5_qqq": -0.4242,
            "market_ret5_iwm": -0.4242,
        }

        report = mod.run_backfill(
            briefs_dir=self.env.briefs,
            grouped_root=self.env.store,
            write=True,
            overwrite=True,
        )
        self.assertEqual(report["refused"], [])
        self.assertAlmostEqual(
            self.env.read(_ANCHOR).column("market_ret5_spy").to_pylist()[0], -0.4242
        )


class TestThePublishedFileModeSurvives(unittest.TestCase):
    """88 of the 112 published brief parquets measured on 2026-10-30 are 0600.
    ``os.replace`` installs the TEMP file's mode on the published path, so without
    carrying the mode across the run silently widens them to the process umask."""

    def setUp(self):
        self.env = _Env()
        self.addCleanup(self.env.cleanup)

    def test_a_0600_parquet_is_still_0600_afterwards(self):
        import os
        import stat

        run_backfill = mod.run_backfill

        path = self.env.briefs / f"{_ANCHOR.isoformat()}.parquet"
        os.chmod(path, 0o600)
        before = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(before, 0o600)

        run_backfill(briefs_dir=self.env.briefs, grouped_root=self.env.store, write=True)

        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
