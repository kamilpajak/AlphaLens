"""Every map-themes recording must carry a complete, truthful provenance file.

The characterization test
(:mod:`tests.golden.test_golden_map_characterization`) can only say whether an
execution moved. It cannot say what the approved execution WAS recorded from —
which event, which prompt version, which model, which sampling, against which
frozen evidence, on what day, approved by whom. That record is
``golden/<version>/provenance.json``, and this module is what stops it from
being absent, incomplete, or contradicted by the artifacts it describes.

Three checks, each with a POSITIVE CONTROL that proves it still fires. A guard
over committed fixtures is otherwise free to rot into a vacuous pass — the
fixtures rarely change, so a checker that silently stopped checking would look
exactly like a checker that keeps passing.

* COMPLETENESS — every recording of every fixture has the file and every
  required field is present and non-empty
  (:func:`~tests.golden.map_provenance.missing_fields`).
* CONSISTENCY — the file does not contradict the artifacts. Model, sampling and
  cassette key are re-read from the recorded cassette; the event is re-resolved
  from the fixture's frozen event window; the theme / asof / version come from
  the fixture descriptor (:func:`~tests.golden.map_provenance.audit_recording`).
* SURFACE DIGESTS — for the CURRENT recording, the manifest lists exactly the
  frozen surface files on disk and every sha256 still matches
  (:func:`~tests.golden.map_provenance.audit_surfaces`).

Why digests are audited for the current recording only: the frozen surfaces are
SHARED across a fixture's recording versions. A superseded recording's manifest
is a historical record of what those files were when it was captured, and a
deliberate full re-capture is allowed to move them. Diffing an old manifest
against a new one is how a reviewer sees which surfaces moved — a check that
forced them to be equal forever would forbid the re-capture instead of
documenting it.
"""

from __future__ import annotations

import copy
import unittest

from tests.golden.map_fixtures import MAP_FIXTURES
from tests.golden.map_provenance import (
    REQUIRED_FIELDS,
    audit_recording,
    audit_surfaces,
    load_provenance,
    missing_fields,
    provenance_path,
    recording_versions,
)

_FIRST = MAP_FIXTURES[0]


def _set_field(doc: dict, dotted: str, value: object) -> dict:
    """Copy of ``doc`` with the dotted path set to ``value`` (for the controls)."""
    out = copy.deepcopy(doc)
    node = out
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value
    return out


def _drop_field(doc: dict, dotted: str) -> dict:
    """Copy of ``doc`` with the dotted path removed entirely."""
    out = copy.deepcopy(doc)
    node = out
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node[part]
    del node[parts[-1]]
    return out


class TestMapFixtureProvenance(unittest.TestCase):
    def test_every_recording_carries_a_complete_provenance_file(self):
        for fixture in MAP_FIXTURES:
            versions = recording_versions(fixture)
            # Without this a fixture whose golden tree failed to glob would pass
            # the loop below by iterating zero times.
            self.assertTrue(versions, f"{fixture.name} has no recording versions on disk")
            for version in versions:
                with self.subTest(fixture=fixture.name, recording=version):
                    path = provenance_path(fixture, version)
                    self.assertTrue(
                        path.exists(),
                        f"{path} is missing — every recording must state what it was "
                        "recorded from, or the approved execution is unattributable",
                    )
                    self.assertEqual(missing_fields(load_provenance(fixture, version)), [])

    def test_completeness_check_reports_each_required_field(self):
        # POSITIVE CONTROL. Drops, then blanks, one required field at a time in
        # a real complete document and requires the checker to name exactly it.
        doc = load_provenance(_FIRST, _FIRST.current_recording)
        self.assertEqual(missing_fields(doc), [], "the control needs a complete document")
        for dotted in REQUIRED_FIELDS:
            with self.subTest(field=dotted):
                self.assertEqual(missing_fields(_drop_field(doc, dotted)), [dotted])
                self.assertEqual(missing_fields(_set_field(doc, dotted, None)), [dotted])

    def test_every_recording_agrees_with_its_cassette_event_and_descriptor(self):
        for fixture in MAP_FIXTURES:
            for version in recording_versions(fixture):
                with self.subTest(fixture=fixture.name, recording=version):
                    doc = load_provenance(fixture, version)
                    self.assertEqual(audit_recording(fixture, version, doc), [])

    def test_consistency_check_reports_a_provenance_that_contradicts_the_artifacts(self):
        # POSITIVE CONTROL. Each tamper is a way the file could quietly lie
        # about what was recorded.
        doc = load_provenance(_FIRST, _FIRST.current_recording)
        self.assertEqual(audit_recording(_FIRST, _FIRST.current_recording, doc), [])
        tampers = (
            ("cassette_key", "0" * 64),
            ("model", "deepseek/deepseek-v4-flash"),
            ("sampling.temperature", 0.7),
            ("sampling.max_tokens", 4000),
            ("prompt.prompt_sha", "deadbeefcafe"),
            ("prompt.mapper_config_version", '{"schema":"x","prompt_sha":"y","schema_sha":"z"}'),
            ("prompt.system_message_sha", "deadbeefcafe"),
            ("event.event_id", "some-other-event"),
            ("event.headline", "A headline this fixture never carried"),
            ("event.theme", "not_the_fixture_theme"),
            ("event.asof", "1999-01-01"),
            ("fixture", "not_this_fixture"),
            ("recording", "v99"),
            ("approved_by", "Somebody Else"),
        )
        for dotted, value in tampers:
            with self.subTest(field=dotted):
                tampered = _set_field(doc, dotted, value)
                problems = audit_recording(_FIRST, _FIRST.current_recording, tampered)
                self.assertTrue(
                    any(dotted in problem for problem in problems),
                    f"tampering {dotted} was not reported: {problems}",
                )

    def test_current_recording_manifest_matches_the_frozen_files_on_disk(self):
        for fixture in MAP_FIXTURES:
            with self.subTest(fixture=fixture.name):
                doc = load_provenance(fixture, fixture.current_recording)
                self.assertEqual(audit_surfaces(fixture, doc), [])

    def test_surface_audit_reports_a_tampered_or_incomplete_manifest(self):
        # POSITIVE CONTROL for the three ways the manifest can stop matching
        # the fixture tree: a wrong digest, a file left out, a file that is not
        # there at all.
        doc = load_provenance(_FIRST, _FIRST.current_recording)
        listed = sorted(doc["frozen_surfaces"])
        self.assertTrue(listed, "the control needs a non-empty manifest")

        wrong_digest = copy.deepcopy(doc)
        wrong_digest["frozen_surfaces"][listed[0]] = "0" * 64
        self.assertTrue(
            any(listed[0] in problem for problem in audit_surfaces(_FIRST, wrong_digest))
        )

        unlisted = copy.deepcopy(doc)
        del unlisted["frozen_surfaces"][listed[0]]
        self.assertTrue(any(listed[0] in problem for problem in audit_surfaces(_FIRST, unlisted)))

        phantom = copy.deepcopy(doc)
        phantom["frozen_surfaces"]["events/1970-01-01.parquet"] = "0" * 64
        self.assertTrue(any("1970-01-01" in problem for problem in audit_surfaces(_FIRST, phantom)))


if __name__ == "__main__":
    unittest.main()
