"""Tests for `alphalens cache refresh-vix` (Track A v2 PR-2).

The refresh command pulls VIXCLS through the canonical FREDClient, takes the
last non-null observation, and writes the tiny JSON VIX cache atomically. Since
#1524 it refreshes the SHARED FRED parquet rather than a throwaway directory —
the temp-dir workaround existed only because the client had no cache expiry. The FRED fetch is injected here so the tests never hit the
network.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_cli.commands import cache as cache_cmd
from alphalens_cli.main import app
from alphalens_feedback import regime
from typer.testing import CliRunner

UTC = dt.UTC
_NOW = dt.datetime(2026, 6, 1, 13, 30, tzinfo=UTC)


def _series(pairs: list[tuple[str, float]]) -> pd.Series:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in pairs], name="date")
    return pd.Series([v for _, v in pairs], index=idx, name="VIXCLS")


class TestRefreshVixCache(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "vix_regime_cache.json"

    def tearDown(self):
        self._td.cleanup()

    def test_writes_cache_from_injected_fred_series(self):
        series = _series([("2026-05-28", 17.1), ("2026-05-29", 18.42)])
        cache_cmd.refresh_vix_cache(self.path, fred_fetch=lambda: series, now=_NOW)
        written = json.loads(self.path.read_text())
        self.assertEqual(written["observation_date"], "2026-05-29")
        self.assertEqual(written["vix"], 18.42)
        self.assertEqual(written["series"], "VIXCLS")
        # fetched_at parses + matches the injected now
        self.assertEqual(dt.datetime.fromisoformat(written["fetched_at"]), _NOW)

    def test_picks_last_non_null_observation(self):
        # A trailing NaN (FRED sentinel ".") must be dropped so we stamp the
        # last REAL close, not NaN.
        series = _series(
            [("2026-05-28", 17.1), ("2026-05-29", 18.42), ("2026-05-30", float("nan"))]
        )
        cache_cmd.refresh_vix_cache(self.path, fred_fetch=lambda: series, now=_NOW)
        written = json.loads(self.path.read_text())
        self.assertEqual(written["observation_date"], "2026-05-29")
        self.assertEqual(written["vix"], 18.42)

    def test_written_cache_is_readable_by_get_cached_vix(self):
        # End-to-end: writer + reader agree on the format.
        series = _series([("2026-05-29", 22.0)])
        cache_cmd.refresh_vix_cache(self.path, fred_fetch=lambda: series, now=_NOW)
        self.assertEqual(regime.get_cached_vix(self.path, now=_NOW), 22.0)
        self.assertEqual(regime.classify_vix(regime.get_cached_vix(self.path, now=_NOW)), "mid")

    def test_written_cache_is_stale_after_96h(self):
        # Round-trip the writer + reader across the staleness boundary: a cache
        # written now reads back None once 96h have elapsed (-> "unknown").
        series = _series([("2026-05-29", 22.0)])
        cache_cmd.refresh_vix_cache(self.path, fred_fetch=lambda: series, now=_NOW)
        stale_now = _NOW + dt.timedelta(seconds=96 * 3600 + 1)
        self.assertIsNone(regime.get_cached_vix(self.path, now=stale_now))

    def test_atomic_write_leaves_no_tmp_file(self):
        series = _series([("2026-05-29", 18.0)])
        cache_cmd.refresh_vix_cache(self.path, fred_fetch=lambda: series, now=_NOW)
        # only the final file remains in the dir (no .tmp leftover)
        siblings = [p.name for p in self.path.parent.iterdir()]
        self.assertEqual(siblings, ["vix_regime_cache.json"])


class TestRefreshVixCommand(unittest.TestCase):
    """CLI wrapper coverage — invoked directly, FRED fetch monkeypatched."""

    def setUp(self):
        self.runner = CliRunner()
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "vix_regime_cache.json"

    def tearDown(self):
        self._td.cleanup()

    def test_cli_refresh_vix_writes_cache(self):
        series = _series([("2026-05-29", 16.3)])
        # Patch the module-level FRED fetch so the command never hits network.
        orig = cache_cmd._fetch_vixcls
        cache_cmd._fetch_vixcls = lambda: series
        try:
            result = self.runner.invoke(
                app, ["cache", "refresh-vix", "--cache-path", str(self.path)]
            )
        finally:
            cache_cmd._fetch_vixcls = orig
        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertTrue(self.path.exists())
        written = json.loads(self.path.read_text())
        self.assertEqual(written["vix"], 16.3)
        self.assertIn("VIXCLS", result.stdout)


class TestTheRefreshUsesTheSharedCache(unittest.TestCase):
    """#1524 retired the throwaway-temp-dir workaround.

    `_fetch_vixcls` used to fetch into a `TemporaryDirectory` precisely because
    the client "returned an existing parquet forever", and its docstring said so
    in as many words. Now that the client honours `through`, keeping it would
    leave a comment asserting a defect that no longer exists — and refreshing the
    SHARED parquet is what lets one fetch serve `market_state` too.
    """

    def test_it_uses_the_shared_cache_and_asks_for_a_current_series(self):
        from unittest import mock

        captured: dict = {}

        class _Client:
            def fetch_series(self, series_id, *, through=None):
                captured["series_id"] = series_id
                captured["through"] = through
                return _series([("2026-09-22", 14.21)])

        def _from_env(*, cache_dir=None):
            captured["cache_dir"] = cache_dir
            return _Client()

        with mock.patch("alphalens_pipeline.data.macro.fred_client.FREDClient.from_env", _from_env):
            cache_cmd._fetch_vixcls()

        self.assertEqual(captured["series_id"], "VIXCLS")
        # None means the client's default, i.e. the SHARED ~/.alphalens/macro dir.
        self.assertIsNone(captured["cache_dir"])
        self.assertIsNotNone(captured["through"], "the refresh must demand a current series")

    def test_no_throwaway_directory_is_opened(self):
        # The refutation control. Without it the assertions above would still
        # pass if someone reintroduced a temp dir and merely defaulted the
        # cache_dir argument, which is exactly the shape of the old workaround.
        #
        # Walked as an AST, not grepped: the docstring names TemporaryDirectory
        # to explain what was removed, and a substring check cannot tell a
        # mention from a call. A grep here failed on its own explanation.
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(cache_cmd._fetch_vixcls)))
        opened = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "TemporaryDirectory"
        ]
        self.assertEqual(opened, [])

    def test_the_ast_control_can_refute(self):
        # Control for the control: the walk above must actually find the call it
        # is looking for when one is present.
        import ast

        tree = ast.parse("with tempfile.TemporaryDirectory() as tmp:\n    pass\n")
        found = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "TemporaryDirectory"
        ]
        self.assertEqual(len(found), 1)


if __name__ == "__main__":
    unittest.main()
