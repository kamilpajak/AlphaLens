"""L3 golden-master replay of the map-themes stage (test-strategy Phase 3b).

Drives the REAL ``orchestrator.map_themes`` deterministically and offline over
one theme (``quantum_computing`` @ 2026-05-24, → RGTI + QUBT). The six external
surfaces are controlled at their natural seams so the REAL parse/gate logic
runs:

* Pro LLM       → ``ReplayOpenRouter`` cassette (real DeepSeek bytes)
* Polygon press → ``VendorCassette`` (real ``get_news_range`` payload), injected
  by patching ``orchestrator.PolygonClient``
* SEC 10-K      → frozen on-disk 10-K text cache (``tenk_cache/``) read by the
  real grep gate; no SEC client call fires (cache hit precedes CIK resolution)
* yfinance mcap → frozen ``{ticker: mcap}`` map (no client to cassette)
* Form-4        → trimmed hive parquet, real Cohen-Malloy classifier runs over it
* catalyst      → frozen events/news window, real resolver runs over it

Assert SIDE EFFECTS, not exit codes: a verification regression flips the gate
verdicts in the projection; a schema drift shows in ``columns``. Cassette /
fixture miss is fail-loud — re-record with ``scripts/record_golden_map.py``.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.thematic.mapping import catalyst_resolver, orchestrator
from alphalens_pipeline.thematic.verification import insider, mcap_filter, recent_press, tenk_grep

from tests.golden.projection import map_themes_projection
from tests.golden.replay_client import ReplayOpenRouter
from tests.golden.vendor_cassette import VendorCassette

_THEME = "quantum_computing"
_ASOF = dt.date(2026, 5, 24)
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "map_day"
_GOLDEN = _FIXTURES / "golden" / "projection.json"

# Genuine module functions captured at import, BEFORE any patch — the partials
# below wrap the REAL logic with a redirected dir/cache, not a patched stand-in.
_REAL_FIND = catalyst_resolver.find_trigger_event
_REAL_FWU = recent_press.fetch_window_universe
_REAL_HTIRP = recent_press.has_theme_in_recent_press
_REAL_TENK = tenk_grep.has_theme_keywords_in_10k
_REAL_INSIDER = insider.has_opportunistic_buy


def _replay_map(out_dir: Path) -> pd.DataFrame:
    if not _GOLDEN.exists() or not any((_FIXTURES / "cassettes_vendor").glob("*.json")):
        raise FileNotFoundError(
            f"golden fixtures missing under {_FIXTURES} — run "
            "scripts/record_golden_map.py (one-time live capture) to record them"
        )
    pro = ReplayOpenRouter(_FIXTURES / "cassettes_llm")
    vendor = VendorCassette(_FIXTURES / "cassettes_vendor")
    mcap_map = {k.upper(): v for k, v in json.loads((_FIXTURES / "mcap.json").read_text()).items()}

    # Fresh empty press cache so the Polygon get_news_range call fires (served
    # by the cassette) and no write lands in ~/.alphalens; TemporaryDirectory
    # cleans it on exit (no /tmp leak across runs).
    with tempfile.TemporaryDirectory(prefix="press_replay_") as press_tmp_str:
        press_tmp = Path(press_tmp_str)
        with (
            mock.patch.object(orchestrator, "_init_pro_client", lambda api_key: pro),
            mock.patch.object(orchestrator, "PolygonClient", lambda *a, **k: vendor),
            mock.patch.object(orchestrator, "get_default_polygon_client", lambda: vendor),
            mock.patch.object(
                catalyst_resolver,
                "find_trigger_event",
                functools.partial(
                    _REAL_FIND, events_dir=_FIXTURES / "events", news_dir=_FIXTURES / "news"
                ),
            ),
            mock.patch.object(
                recent_press,
                "fetch_window_universe",
                functools.partial(_REAL_FWU, cache_dir=press_tmp),
            ),
            mock.patch.object(
                recent_press,
                "has_theme_in_recent_press",
                functools.partial(_REAL_HTIRP, cache_dir=press_tmp),
            ),
            mock.patch.object(
                tenk_grep,
                "has_theme_keywords_in_10k",
                functools.partial(_REAL_TENK, cache_dir=_FIXTURES / "tenk_cache"),
            ),
            mock.patch.object(
                insider,
                "has_opportunistic_buy",
                functools.partial(_REAL_INSIDER, form4_root=_FIXTURES / "form4_parquet"),
            ),
            mock.patch.object(
                mcap_filter, "fetch_mcap", lambda ticker, *, asof=None: mcap_map.get(ticker.upper())
            ),
        ):
            return orchestrator.map_themes(
                themes=[_THEME],
                asof=_ASOF,
                api_key="replay",
                polygon_api_key="replay",  # forces the patched PolygonClient branch
                output_dir=out_dir,
                market_cap_range=orchestrator.DEFAULT_MCAP_RANGE,
            )


class TestGoldenMapReplay(unittest.TestCase):
    def test_replay_matches_golden_projection(self):
        with tempfile.TemporaryDirectory() as td:
            df = _replay_map(Path(td))
        got = map_themes_projection(df)
        golden = json.loads(_GOLDEN.read_text())
        self.assertEqual(got, golden)

    def test_both_candidates_pass_tenk_and_press(self):
        # The two verification surfaces with recorded/frozen external data
        # (Polygon press cassette + frozen 10-K text) must both fire and pass.
        with tempfile.TemporaryDirectory() as td:
            df = _replay_map(Path(td))
        self.assertEqual(sorted(df["ticker"]), ["QUBT", "RGTI"])
        for _, row in df.iterrows():
            self.assertIn("tenk", row["gates_passed"])
            self.assertIn("press", row["gates_passed"])

    def test_insider_gate_runs_over_frozen_form4(self):
        # The Cohen-Malloy classifier runs over the trimmed Form-4 fixture and
        # returns a DECISIVE verdict (pass or fail), never "unknown" — proving
        # the frozen partitions actually fed the classifier. (Golden: both fail
        # the insider gate, so n_gates_unknown == 0.)
        with tempfile.TemporaryDirectory() as td:
            df = _replay_map(Path(td))
        for _, row in df.iterrows():
            self.assertEqual(row["n_gates_unknown"], 0)
            self.assertIn("insider", row["gates_failed"])

    def test_replay_is_deterministic(self):
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            a = map_themes_projection(_replay_map(Path(td1)))
            b = map_themes_projection(_replay_map(Path(td2)))
        self.assertEqual(a, b)

    def test_candidates_parquet_written(self):
        with tempfile.TemporaryDirectory() as td:
            _replay_map(Path(td))
            self.assertTrue((Path(td) / f"{_ASOF.isoformat()}.parquet").exists())


if __name__ == "__main__":
    unittest.main()
