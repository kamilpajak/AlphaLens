"""L3 golden-master replay of the map-themes stage (test-strategy Phase 3b).

Drives the REAL ``orchestrator.map_themes`` deterministically and offline over
one theme (``quantum_computing`` @ 2026-05-24). The six external surfaces are
controlled at their natural seams — the Pro LLM by a cassette, the other five by
:func:`tests.golden.map_fixtures.frozen_surfaces` — so the REAL parse/gate logic
runs while nothing leaves the machine.

WHAT THIS TEST IS
-----------------
It is a CHARACTERIZATION test, not a specification test. It answers exactly one
question: *did this execution differ from the approved one?* It never answers
*is this execution correct?* Concretely:

* A failure means the pipeline's behaviour on this one frozen input moved. That
  is a prompt to go and look, not a verdict that the new behaviour is wrong.
* Passing proves the deterministic downstream machinery — parsing, the four
  gates, the schema, the plumbing — still handles this recorded response the
  approved way. It proves NOTHING about whether the model picks the right
  companies: the cassette is ONE sample of a stochastic generator (the same
  prompt and event, run 15 times, returned one target ticker 6/15 times).
* Re-baselining is therefore legitimate and expected, but never casual. The
  cassette miss stays fail-loud, the old recording is preserved beside the new
  one under its own version directory, and the request/projection diff is
  written up for review — see ``map_fixtures.CURRENT_RECORDING`` and
  ``docs/research/golden_map_rebaseline_2026_08_03.md``.

Assert SIDE EFFECTS, not exit codes: a verification regression flips the gate
verdicts in the projection; a schema drift shows in ``columns``. Cassette /
fixture miss is fail-loud — re-record with
``scripts/record_golden_map.py --llm-only``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.thematic.mapping import orchestrator

from tests.golden.map_fixtures import (
    ASOF,
    FIXTURES,
    THEME,
    frozen_surfaces,
    golden_projection_path,
    llm_cassette_dir,
)
from tests.golden.projection import map_themes_projection
from tests.golden.replay_client import ReplayOpenRouter

_GOLDEN = golden_projection_path()


def _replay_map(out_dir: Path) -> pd.DataFrame:
    if not _GOLDEN.exists() or not any((FIXTURES / "cassettes_vendor").glob("*.json")):
        raise FileNotFoundError(
            f"golden fixtures missing under {FIXTURES} (expected {_GOLDEN}) — run "
            "scripts/record_golden_map.py --llm-only to re-baseline the current "
            "recording, or without the flag for a full six-surface re-capture"
        )
    pro = ReplayOpenRouter(llm_cassette_dir())
    with frozen_surfaces(pro_client=pro):
        return orchestrator.map_themes(
            themes=[THEME],
            asof=ASOF,
            api_key="replay",
            polygon_api_key="replay",  # forces the patched PolygonClient branch
            output_dir=out_dir,
            market_cap_range=orchestrator.DEFAULT_MCAP_RANGE,
        )


class TestGoldenMapCharacterization(unittest.TestCase):
    def test_replay_matches_golden_projection(self):
        with tempfile.TemporaryDirectory() as td:
            df = _replay_map(Path(td))
        got = map_themes_projection(df)
        golden = json.loads(_GOLDEN.read_text())
        self.assertEqual(got, golden)

    def test_kept_candidates_pass_tenk_and_press(self):
        # The two verification surfaces with recorded/frozen external data
        # (Polygon press cassette + frozen 10-K text) must both fire and pass.
        # The kept set is pinned exactly: v2 proposed IONQ/RGTI/QBTS and only
        # RGTI is inside the 500M-10B bracket (v1 also kept QUBT, which v2 did
        # not propose at all). Method renamed from ..._both_candidates_... with
        # the v2 re-baseline; the assertions are unchanged.
        with tempfile.TemporaryDirectory() as td:
            df = _replay_map(Path(td))
        self.assertEqual(sorted(df["ticker"]), ["RGTI"])
        for _, row in df.iterrows():
            self.assertIn("tenk", row["gates_passed"])
            self.assertIn("press", row["gates_passed"])

    def test_insider_gate_runs_over_frozen_form4(self):
        # The Cohen-Malloy classifier runs over the trimmed Form-4 fixture and
        # returns a DECISIVE verdict (pass or fail), never "unknown" — proving
        # the frozen partitions actually fed the classifier. (Golden: the kept
        # row fails the insider gate, so n_gates_unknown == 0.)
        with tempfile.TemporaryDirectory() as td:
            df = _replay_map(Path(td))
        # Without this the test is vacuous: a replay that returns no rows at all
        # (e.g. a cassette key miss after a prompt change) iterates zero times
        # and reports green while asserting nothing.
        self.assertFalse(df.empty, "replay produced no rows - the loop below would assert nothing")
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
            self.assertTrue((Path(td) / f"{ASOF.isoformat()}.parquet").exists())


if __name__ == "__main__":
    unittest.main()
