"""The recorder must refuse to overwrite ANY artifact of an existing recording.

``scripts/record_golden_map.py`` exists to capture a characterization golden,
and a characterization golden is only worth keeping if the new execution can be
diffed against the approved one. The overwrite guard is what makes that true:
it forces a re-baseline to bump ``current_recording`` instead of writing over
the version already on disk.

A recording is FOUR artifacts, not one — the LLM cassette, the candidates
parquet, the projection and the provenance document. A guard that watched only
the cassette directory would let a version whose cassette was deleted (or was
never written, e.g. a capture that died before the LLM call) be silently
overwritten in its golden directory, which is where the approved projection and
the provenance live.

These tests drive the guard against a THROWAWAY fixture tree
(``map_fixtures.FIXTURES`` redirected at a temp dir), so nothing here reads or
writes the committed fixtures.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import record_golden_map

from tests.golden import map_fixtures
from tests.golden.map_fixtures import MapFixture

_FIXTURE = MapFixture(
    name="guard_probe",
    theme="quantum_computing",
    asof=dt.date(2026, 4, 14),
    window_dates=("2026-04-14",),
    current_recording="v1",
    dirname="guard_probe",
)


class TestRecorderOverwriteGuard(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="record_guard_")
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(map_fixtures, "FIXTURES", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, path: Path, payload: str = "{}") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload)

    def test_empty_version_is_accepted_and_the_cassette_dir_is_created(self):
        # The control: without this, every assertion below could pass because
        # the guard refuses unconditionally.
        llm_dir = record_golden_map._guard_recording_dir(_FIXTURE)
        self.assertEqual(llm_dir, _FIXTURE.llm_cassette_dir())
        self.assertTrue(llm_dir.is_dir())

    def test_refuses_when_the_cassette_is_already_recorded(self):
        self._write(_FIXTURE.llm_cassette_dir() / "abc123.json")
        with self.assertRaises(SystemExit) as ctx:
            record_golden_map._guard_recording_dir(_FIXTURE)
        self.assertIn("v1", str(ctx.exception))

    def test_refuses_when_the_golden_projection_is_already_published(self):
        # No cassette on disk, so a cassette-only guard would wave this through
        # and _write_golden would overwrite the approved projection.
        self._write(_FIXTURE.golden_dir() / "projection.json", json.dumps({"row_count": 1}))
        with self.assertRaises(SystemExit) as ctx:
            record_golden_map._guard_recording_dir(_FIXTURE)
        self.assertIn("projection.json", str(ctx.exception))

    def test_refuses_when_the_provenance_document_is_already_published(self):
        self._write(_FIXTURE.golden_dir() / "provenance.json", json.dumps({"recording": "v1"}))
        with self.assertRaises(SystemExit) as ctx:
            record_golden_map._guard_recording_dir(_FIXTURE)
        self.assertIn("provenance.json", str(ctx.exception))

    def test_refuses_when_the_candidates_parquet_is_already_published(self):
        self._write(_FIXTURE.golden_dir() / f"{_FIXTURE.asof.isoformat()}.parquet", "not-a-parquet")
        with self.assertRaises(SystemExit) as ctx:
            record_golden_map._guard_recording_dir(_FIXTURE)
        self.assertIn("2026-04-14.parquet", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
