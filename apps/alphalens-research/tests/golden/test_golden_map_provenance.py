"""Every map-themes recording must carry a complete, truthful provenance file.

The characterization test
(:mod:`tests.golden.test_golden_map_characterization`) can only say whether an
execution moved. It cannot say what the approved execution WAS recorded from —
which event, which prompt version, which model, which sampling, against which
frozen evidence, on what day, approved by whom. That record is
``golden/<version>/provenance.json``, and this module is what stops it from
being absent, incomplete, or contradicted by the artifacts it describes.

Four checks, each with a POSITIVE CONTROL that proves it still fires. A guard
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
* DISCLOSURE — a frozen surface that was hand-authored is declared as such, and
  a recording whose config token no artifact can pin says so in ``notes``.
  Neither is derivable from the files, so both would otherwise survive only in
  a memo that the next capture forgets.

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

from tests.golden.map_fixtures import MAP_FIXTURES, NVDA_ISING_2026_04_14
from tests.golden.map_provenance import (
    PROVENANCE_SCHEMA_VERSION,
    PROVENANCE_SCHEMA_VERSION_STAGE_A_ONLY,
    REQUIRED_FIELDS,
    audit_recording,
    audit_surfaces,
    load_provenance,
    missing_fields,
    provenance_path,
    recording_versions,
    split_cassette_records,
    stage_b_block,
    stamped_config_version,
    surface_manifest,
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
            ("seeded_surfaces", {"news/1970-01-01.parquet": "invented"}),
        )
        for dotted, value in tampers:
            with self.subTest(field=dotted):
                tampered = _set_field(doc, dotted, value)
                problems = audit_recording(_FIRST, _FIRST.current_recording, tampered)
                self.assertTrue(
                    any(dotted in problem for problem in problems),
                    f"tampering {dotted} was not reported: {problems}",
                )

    def test_a_recording_whose_token_cannot_be_pinned_says_so(self):
        # The three prompt fingerprints are checked against the document's own
        # ``mapper_config_version``, so the only thing that pins that token to
        # an artifact is the ``mapper_config_version`` column in the recording's
        # parquet. A recording that predates the column has no such pin, and a
        # wrong-but-internally-consistent token would pass every other check.
        # It must therefore carry the disclosure in ``notes``; without it the
        # limitation is invisible to a reader.
        unpinnable = [
            (fixture, version)
            for fixture in MAP_FIXTURES
            for version in recording_versions(fixture)
            if stamped_config_version(fixture, version) is None
        ]
        self.assertTrue(unpinnable, "no unpinnable recording on disk — the control is vacuous")
        for fixture, version in unpinnable:
            with self.subTest(fixture=fixture.name, recording=version):
                doc = load_provenance(fixture, version)
                self.assertEqual(audit_recording(fixture, version, doc), [])
                problems = audit_recording(fixture, version, _drop_field(doc, "notes"))
                self.assertTrue(
                    any("notes" in problem for problem in problems),
                    f"an undisclosed unpinnable token was not reported: {problems}",
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


class TestSeededSurfacesAreDeclared(unittest.TestCase):
    """A frozen surface that was HAND-AUTHORED must say so, in the document.

    Not every surface of a fixture can be captured from a live vendor or a real
    on-disk store. Where one was written by hand, the recording is still a
    faithful capture of the pipeline's behaviour over that input — but a reader
    who assumes every value came from production would over-read the result.
    The declaration lives on the fixture descriptor and is copied into the
    document by the recorder, so a re-record cannot silently drop it the way a
    hand-added prose note would.
    """

    def test_every_recording_lists_the_fixtures_hand_authored_surfaces(self):
        for fixture in MAP_FIXTURES:
            expected = dict(fixture.seeded_surfaces)
            for version in recording_versions(fixture):
                with self.subTest(fixture=fixture.name, recording=version):
                    doc = load_provenance(fixture, version)
                    self.assertEqual(doc.get("seeded_surfaces") or {}, expected)

    def test_declared_paths_are_real_frozen_surfaces(self):
        # A declaration naming a path that is not a frozen surface would be
        # decoration; it has to point at a file the replay actually reads.
        for fixture in MAP_FIXTURES:
            manifest = surface_manifest(fixture)
            for path, why in fixture.seeded_surfaces:
                with self.subTest(fixture=fixture.name, surface=path):
                    self.assertIn(path, manifest)
                    self.assertTrue(why.strip(), "a seeded surface must say why")

    def test_the_ising_fixture_declares_its_hand_authored_catalyst_window(self):
        # The one fixture whose event/news rows were written by hand rather
        # than ingested. Pinned by name so the declaration cannot be dropped
        # while the rows stay synthetic.
        declared = {path for path, _ in NVDA_ISING_2026_04_14.seeded_surfaces}
        self.assertIn("events/2026-04-14.parquet", declared)
        self.assertIn("news/2026-04-14.parquet", declared)

    def test_audit_reports_an_undeclared_seeded_surface(self):
        # POSITIVE CONTROL: the check must fire in BOTH directions - a document
        # that invents a declaration, and one that drops a real one.
        seeded = NVDA_ISING_2026_04_14
        version = seeded.current_recording
        doc = load_provenance(seeded, version)
        self.assertEqual(audit_recording(seeded, version, doc), [])
        for tampered in (
            _drop_field(doc, "seeded_surfaces"),
            _set_field(doc, "seeded_surfaces", {}),
        ):
            problems = audit_recording(seeded, version, tampered)
            self.assertTrue(
                any("seeded_surfaces" in problem for problem in problems),
                f"a dropped seeded-surface declaration was not reported: {problems}",
            )


class TestStageBIsDocumented(unittest.TestCase):
    """map-themes stopped being a ONE-CALL stage on 2026-08-19.

    The proposal call is now followed by a per-candidate channel assessment at
    a different model config, so a recording holds one stage-A cassette plus one
    per assessed candidate. Two things must not go unrecorded: the assessment
    stage's own model and sampling (otherwise the document describes half of the
    execution it pins), and the fact that the k identical draws of one candidate
    collapse to ONE cassette file — which makes the replayed
    ``channel_support_dispersion`` an artefact rather than a measurement.
    """

    def test_the_stage_a_cassette_is_the_proposal_call(self):
        for fixture in MAP_FIXTURES:
            for version in recording_versions(fixture):
                with self.subTest(fixture=fixture.name, recording=version):
                    stage_a, _stage_b = split_cassette_records(fixture, version)
                    system_message = stage_a["config"]["system_message"]
                    self.assertIn("candidates", system_message)
                    self.assertNotIn("channel_support_status", system_message)

    def test_a_recording_with_stage_b_cassettes_carries_the_block(self):
        described = 0
        for fixture in MAP_FIXTURES:
            for version in recording_versions(fixture):
                block = stage_b_block(fixture, version)
                if block is None:
                    continue
                described += 1
                with self.subTest(fixture=fixture.name, recording=version):
                    doc = load_provenance(fixture, version)
                    self.assertEqual(doc.get("stage_b"), block)
                    self.assertEqual(doc["schema_version"], PROVENANCE_SCHEMA_VERSION)
                    self.assertTrue(block["cassette_keys"])
                    self.assertIn("vote_collapse_note", block)
                    # The two stages are different requests, not the same one
                    # counted twice: their rendered schemas must differ.
                    stage_a, _ = split_cassette_records(fixture, version)
                    self.assertNotEqual(
                        block["system_message_sha"],
                        doc["prompt"]["system_message_sha"],
                    )
                    self.assertNotEqual(
                        block["sampling"]["max_tokens"], stage_a["config"]["max_tokens"]
                    )
        self.assertTrue(described, "no recording carries a stage-B cassette — the check is vacuous")

    def test_a_pre_stage_b_recording_keeps_its_own_schema_version(self):
        # Back-stamping an older recording to the new schema with an empty
        # stage_b block would assert a stage it never ran.
        older = 0
        for fixture in MAP_FIXTURES:
            for version in recording_versions(fixture):
                if stage_b_block(fixture, version) is not None:
                    continue
                older += 1
                with self.subTest(fixture=fixture.name, recording=version):
                    doc = load_provenance(fixture, version)
                    self.assertIsNone(doc.get("stage_b"))
                    self.assertEqual(doc["schema_version"], PROVENANCE_SCHEMA_VERSION_STAGE_A_ONLY)
        self.assertTrue(older, "no pre-stage-B recording on disk — the check is vacuous")

    def test_audit_reports_a_stage_b_block_that_contradicts_the_cassettes(self):
        # POSITIVE CONTROL, both directions: a document that misstates the
        # assessment config, and one that drops the block entirely.
        target = next(
            (f, v)
            for f in MAP_FIXTURES
            for v in recording_versions(f)
            if stage_b_block(f, v) is not None
        )
        fixture, version = target
        doc = load_provenance(fixture, version)
        self.assertEqual(audit_recording(fixture, version, doc), [])
        for tampered in (
            _drop_field(doc, "stage_b"),
            _set_field(doc, "stage_b.model", "deepseek/deepseek-v4-flash"),
            _set_field(doc, "stage_b.sampling", {"temperature": 0.7, "max_tokens": 1500}),
            _set_field(doc, "stage_b.cassette_keys", ["0" * 64]),
        ):
            problems = audit_recording(fixture, version, tampered)
            self.assertTrue(
                any("stage_b" in problem for problem in problems),
                f"a tampered stage_b block was not reported: {problems}",
            )


if __name__ == "__main__":
    unittest.main()
