"""L3 golden-master replay of the map-themes stage (test-strategy Phase 3b).

Drives the REAL ``orchestrator.map_themes`` deterministically and offline, once
per fixture in :data:`tests.golden.map_fixtures.MAP_FIXTURES`. The six external
surfaces are controlled at their natural seams — the Pro LLM by a cassette, the
other five by :func:`tests.golden.map_fixtures.frozen_surfaces` — so the REAL
parse/gate logic runs while nothing leaves the machine.

WHAT THIS TEST IS
-----------------
It is a CHARACTERIZATION test, not a specification test. It answers exactly one
question: *did this execution differ from the approved one?* It never answers
*is this execution correct?* Concretely:

* A failure means the pipeline's behaviour on one frozen input moved. That is a
  prompt to go and look, not a verdict that the new behaviour is wrong.
* Passing proves the deterministic downstream machinery — parsing, the three
  gates, the schema, the plumbing — still handles these recorded responses the
  approved way. It proves NOTHING about whether the model picks the right
  companies: each cassette is ONE sample of a stochastic generator (the same
  prompt and event, run 15 times, returned one target ticker 6/15 times).
* Re-baselining is therefore legitimate and expected, but never casual. The
  cassette miss stays fail-loud, an old recording is preserved beside the new
  one under its own version directory, and the request/projection diff is
  written up for review — see ``map_fixtures.MapFixture.current_recording``,
  ``docs/research/golden_map_rebaseline_2026_08_03.md`` and
  ``docs/research/golden_map_ising_fixture_2026_08_03.md``.

WHERE THE PER-FIXTURE EXPECTATIONS LIVE
---------------------------------------
Every fixture-specific verdict — which tickers survive, which gate passed or
failed on each of them, the mcap bucket, the schema — is pinned EXACTLY by that
fixture's committed ``golden/<version>/projection.json`` and compared whole in
:meth:`test_replay_matches_golden_projection`. The tests below therefore carry
no per-fixture literals; what they add on top is the cross-fixture INVARIANT
that must hold for every recording, present and future: the frozen surfaces
must actually feed the gates, so no verdict may come back ``unknown``.

Assert SIDE EFFECTS, not exit codes: a verification regression flips the gate
verdicts in the projection; a schema drift shows in ``columns``. Cassette /
fixture miss is fail-loud — re-record with ``scripts/record_golden_map.py``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.thematic.mapping import orchestrator

from tests.golden.map_fixtures import MAP_FIXTURES, MapFixture, frozen_surfaces
from tests.golden.projection import map_themes_projection
from tests.golden.replay_client import ReplayOpenRouter


def _replay_map(fixture: MapFixture, out_dir: Path) -> pd.DataFrame:
    golden = fixture.golden_projection_path()
    if not golden.exists() or not any(fixture.vendor_cassette_dir.glob("*.json")):
        raise FileNotFoundError(
            f"golden fixtures missing under {fixture.root} (expected {golden}) — run "
            f"scripts/record_golden_map.py --fixture {fixture.name} for a full "
            "six-surface capture, or with --llm-only to re-baseline just the "
            "cassette of the current recording"
        )
    pro = ReplayOpenRouter(fixture.llm_cassette_dir())
    with frozen_surfaces(fixture, pro_client=pro):
        return orchestrator.map_themes(
            themes=[fixture.theme],
            asof=fixture.asof,
            api_key="replay",
            polygon_api_key="replay",  # forces the patched PolygonClient branch
            output_dir=out_dir,
            market_cap_range=orchestrator.DEFAULT_MCAP_RANGE,
        )


class TestGoldenMapCharacterization(unittest.TestCase):
    def test_replay_matches_golden_projection(self):
        for fixture in MAP_FIXTURES:
            with self.subTest(fixture=fixture.name), tempfile.TemporaryDirectory() as td:
                golden = json.loads(fixture.golden_projection_path().read_text())
                # A zero-row recording is not an acceptable baseline: every
                # assertion here would then hold vacuously for a replay that
                # produced nothing (e.g. a cassette key miss swallowed).
                self.assertGreater(golden["row_count"], 0, f"{fixture.name} golden records no rows")
                got = map_themes_projection(_replay_map(fixture, Path(td)))
                self.assertEqual(got, golden)

    def test_frozen_surfaces_decide_every_gate(self):
        # Cross-fixture invariant. Each gate reads an external surface that is
        # frozen (10-K text cache, Polygon press cassette, Form-4 parquet), and
        # a surface that fails to load degrades to "unknown" rather than
        # erroring. So a fixture recorded against a missing 10-K, an untrimmed
        # cassette or an absent Form-4 partition would still produce a green
        # projection while proving nothing about the gate logic. Requiring
        # every gate to reach a decisive verdict is what rules that out.
        for fixture in MAP_FIXTURES:
            with self.subTest(fixture=fixture.name), tempfile.TemporaryDirectory() as td:
                df = _replay_map(fixture, Path(td))
                # Without this the test is vacuous: a replay that returns no
                # rows at all (e.g. a cassette key miss after a prompt change)
                # iterates zero times and reports green while asserting nothing.
                self.assertFalse(
                    df.empty, f"{fixture.name} replay produced no rows - the loop asserts nothing"
                )
                for _, row in df.iterrows():
                    self.assertEqual(row["n_gates_unknown"], 0, row["ticker"])
                    self.assertEqual(
                        row["n_gates_passed"] + row["n_gates_failed"],
                        len(orchestrator.GATE_NAMES),
                        row["ticker"],
                    )
                    # The keep contract: a row only reaches the parquet when at
                    # least one gate passed (``keep_unverified=False``).
                    self.assertTrue(row["verified"], row["ticker"])
                    self.assertGreaterEqual(row["n_gates_passed"], 1, row["ticker"])

    def test_replay_is_deterministic(self):
        for fixture in MAP_FIXTURES:
            with (
                self.subTest(fixture=fixture.name),
                tempfile.TemporaryDirectory() as td1,
                tempfile.TemporaryDirectory() as td2,
            ):
                a = map_themes_projection(_replay_map(fixture, Path(td1)))
                b = map_themes_projection(_replay_map(fixture, Path(td2)))
                self.assertEqual(a, b)

    def test_candidates_parquet_written(self):
        for fixture in MAP_FIXTURES:
            with self.subTest(fixture=fixture.name), tempfile.TemporaryDirectory() as td:
                _replay_map(fixture, Path(td))
                self.assertTrue((Path(td) / f"{fixture.asof.isoformat()}.parquet").exists())


if __name__ == "__main__":
    unittest.main()
