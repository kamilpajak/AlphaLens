"""Tests over the frozen catalyst-role case set (``tests/fixtures/catalyst_role_cases``).

The case set is the yardstick for the role instrument in
``scripts/classify_catalyst_roles.py``. A yardstick that can drift is not a
yardstick, so everything here is offline and hermetic: it reads one JSON file
and the script's own constants, never a parquet cache, never the network.

Three properties carry the weight:

1. **Anti-rot** - the fixture's anchor cases and the script's ``ANCHORS`` tuple
   must be the same set, compared both directions. Editing one without the
   other silently splits the gate from the payloads it gates on, which is the
   failure a frozen case set exists to prevent.
2. **Blindness on real payloads** - every case renders through
   ``build_role_prompt`` under both framings without leaking a pipeline verdict
   field. The existing prompt tests prove this on one synthetic row; this
   proves it on the payload shapes the instrument actually meets (empty
   ``primary_entities``, empty ``second_order_implications``, non-ASCII
   headlines).
3. **Non-vacuity** - a truncated or empty fixture must not make the file pass
   by iterating over nothing, and the blindness checker must be shown to fire
   on a prompt that does leak.

The live evaluation that spends money on the real classifier lives in
``tests/live/test_catalyst_role_eval_live.py``.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from scripts.classify_catalyst_roles import ANCHORS, FRAMINGS, ROLES, build_role_prompt

CASES_PATH = Path(__file__).resolve().parent / "fixtures" / "catalyst_role_cases" / "cases.json"

# The pipeline's own verdict fields. The instrument is blind to every one of
# them by design - rendering any of them would turn "is there a transmission
# channel" into "does the pipeline agree with itself".
EXCLUDED_FIELDS: tuple[str, ...] = (
    "layer4_weighted_score",
    "rank_in_day",
    "llm_confidence",
    "rationale",
    "gates_passed_str",
)

# Verdict values injected by the tests only, never by the fixture. They are
# deliberately unmistakable: a bare ``2`` or ``0.85`` collides with a date or
# with an unrelated number in the prompt, so a substring hit on one of these
# can only mean a real leak.
VERDICT_PROBE_VALUES: dict[str, Any] = {
    "layer4_weighted_score": 424242,
    "rank_in_day": 987654,
    "llm_confidence": 0.9192939,
    "rationale": "VERDICTLEAKCANARY second-order beneficiary of the same theme",
    "gates_passed_str": "VERDICTLEAKCANARYGATES",
}

_REQUIRED_TOP_LEVEL_KEYS = ("case_set_version", "extracted_on", "purpose", "sources", "cases")
_REQUIRED_CASE_KEYS = ("case_id", "anchor", "expected_role", "provenance", "event")
_REQUIRED_EVENT_KEYS = (
    "ticker",
    "brief_date",
    "theme",
    "company_name",
    "sector_name",
    "industry_name",
    "source_event_title",
    "source_event_url",
    "catalyst_event_type",
    "sentiment",
    "primary_entities",
    "second_order_implications",
)
# A case with an empty headline or an empty theme cannot measure anything -
# there is nothing for the instrument to read. Entity lists are allowed to be
# empty (an entity-less event is a real, common shape) but must still be lists.
_NON_EMPTY_EVENT_KEYS = tuple(
    key
    for key in _REQUIRED_EVENT_KEYS
    if key not in ("primary_entities", "second_order_implications")
)
_LIST_EVENT_KEYS = ("primary_entities", "second_order_implications")

# The fixture was frozen with six anchors. Fewer means it was truncated, and a
# truncated fixture must fail rather than pass every loop vacuously.
_MIN_ANCHOR_CASES = 6


def load_case_set() -> dict:
    """Read the frozen case set. The only file this module touches."""
    return json.loads(CASES_PATH.read_text())


def load_cases() -> list[dict]:
    return load_case_set()["cases"]


def anchor_cases() -> list[dict]:
    return [case for case in load_cases() if case.get("anchor")]


def contested_cases() -> list[dict]:
    return [case for case in load_cases() if case.get("contested")]


def anchor_key(case: dict) -> tuple[str, str, str]:
    """The (ticker, brief_date, expected_role) triple the anchor gate keys on."""
    return (case["event"]["ticker"], case["event"]["brief_date"], case["expected_role"])


def blindness_violations(prompt: str) -> list[str]:
    """Every verdict field NAME or injected verdict VALUE found in ``prompt``.

    Names are matched case-insensitively; values are matched as rendered by
    ``str()``, which is how an accidental f-string interpolation would surface.
    Empty return means the prompt is blind.
    """
    lowered = prompt.lower()
    violations = [name for name in EXCLUDED_FIELDS if name in lowered]
    violations += [
        f"{name}={value}" for name, value in VERDICT_PROBE_VALUES.items() if str(value) in prompt
    ]
    return violations


def _row_with_verdict_fields(case: dict) -> dict:
    """The case's event payload plus the verdict fields the instrument must ignore.

    The fixture deliberately stores none of these, so the test supplies them:
    a blindness assertion over a payload that never carried the fields in the
    first place proves nothing.
    """
    return {**case["event"], **VERDICT_PROBE_VALUES}


class TestCaseSetSchema(unittest.TestCase):
    def test_top_level_keys_are_present(self):
        case_set = load_case_set()
        for key in _REQUIRED_TOP_LEVEL_KEYS:
            self.assertIn(key, case_set)
        self.assertTrue(case_set["sources"], "sources must name where the payloads came from")

    def test_every_case_carries_the_required_keys(self):
        for case in load_cases():
            with self.subTest(case=case.get("case_id")):
                for key in _REQUIRED_CASE_KEYS:
                    self.assertIn(key, case)
                self.assertTrue(case["provenance"], "a case must name where its payload came from")

    def test_every_event_field_is_populated(self):
        for case in load_cases():
            event = case["event"]
            for key in _REQUIRED_EVENT_KEYS:
                with self.subTest(case=case["case_id"], field=key):
                    self.assertIn(key, event)
            for key in _NON_EMPTY_EVENT_KEYS:
                with self.subTest(case=case["case_id"], field=key):
                    self.assertIsInstance(event[key], str)
                    self.assertTrue(
                        event[key].strip(),
                        f"{case['case_id']} has an empty {key} - nothing for the instrument to read",
                    )

    def test_entity_lists_are_lists_of_strings(self):
        for case in load_cases():
            for key in _LIST_EVENT_KEYS:
                with self.subTest(case=case["case_id"], field=key):
                    value = case["event"][key]
                    self.assertIsInstance(value, list)
                    for item in value:
                        self.assertIsInstance(item, str)

    def test_case_id_matches_its_ticker_and_brief_date(self):
        for case in load_cases():
            with self.subTest(case=case["case_id"]):
                expected = f"{case['event']['ticker']}@{case['event']['brief_date']}"
                self.assertEqual(case["case_id"], expected)

    def test_no_case_stores_a_pipeline_verdict_field(self):
        declared = load_case_set()["excluded_fields"]["fields"]
        self.assertEqual(sorted(declared), sorted(EXCLUDED_FIELDS))
        for case in load_cases():
            for banned in EXCLUDED_FIELDS:
                with self.subTest(case=case["case_id"], field=banned):
                    self.assertNotIn(banned, case["event"])
                    if (
                        banned != "rationale"
                    ):  # a case's own rationale is documentation, not a verdict
                        self.assertNotIn(banned, case)


class TestExpectedRoles(unittest.TestCase):
    def test_every_anchor_expects_a_role_inside_the_taxonomy(self):
        cases = anchor_cases()
        self.assertTrue(cases)
        for case in cases:
            with self.subTest(case=case["case_id"]):
                self.assertIn(case["expected_role"], ROLES)


class TestNoDuplicateCases(unittest.TestCase):
    """Two cases on the same (ticker, brief_date) would give the anchor gate two
    answers for one key - ``anchor_report`` indexes on exactly that pair."""

    def test_no_duplicate_ticker_and_brief_date(self):
        keys = [(c["event"]["ticker"], c["event"]["brief_date"]) for c in load_cases()]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        self.assertEqual(duplicates, [], f"duplicate (ticker, brief_date): {duplicates}")

    def test_no_duplicate_case_id(self):
        ids = [c["case_id"] for c in load_cases()]
        self.assertEqual(len(ids), len(set(ids)))


class TestAnchorSetMatchesTheScript(unittest.TestCase):
    """Anti-rot. The fixture holds the payloads; ``ANCHORS`` holds the gate. If
    they drift apart the gate silently starts grading cases that are no longer
    the ones frozen, so both directions of the difference are asserted."""

    def test_fixture_anchors_and_script_anchors_are_the_same_set(self):
        in_fixture = {anchor_key(case) for case in anchor_cases()}
        in_script = {(a["ticker"], a["brief_date"], a["expected_role"]) for a in ANCHORS}

        self.assertEqual(
            in_fixture - in_script,
            set(),
            "anchor cases in the fixture that ANCHORS does not gate on - "
            "add them to classify_catalyst_roles.ANCHORS or drop the anchor flag",
        )
        self.assertEqual(
            in_script - in_fixture,
            set(),
            "ANCHORS entries with no frozen payload in the fixture - "
            "add the case to cases.json or remove the anchor",
        )
        self.assertEqual(in_fixture, in_script)

    def test_every_anchor_appears_exactly_once_in_each_side(self):
        fixture_pairs = [(c["event"]["ticker"], c["event"]["brief_date"]) for c in anchor_cases()]
        script_pairs = [(a["ticker"], a["brief_date"]) for a in ANCHORS]
        self.assertEqual(len(fixture_pairs), len(set(fixture_pairs)))
        self.assertEqual(len(script_pairs), len(set(script_pairs)))
        self.assertEqual(len(fixture_pairs), len(script_pairs))


class TestContestedCases(unittest.TestCase):
    """A contested case is tracked, never graded: the strict and permissive
    rubrics disagree on it by construction, so asserting a role would be
    inventing an answer."""

    def test_contested_cases_carry_no_expected_role_and_a_stated_reason(self):
        cases = contested_cases()
        self.assertTrue(cases, "the case set tracks at least one contested case")
        for case in cases:
            with self.subTest(case=case["case_id"]):
                self.assertIsNone(case["expected_role"])
                self.assertTrue(case.get("contested_reason", "").strip())

    def test_contested_cases_are_excluded_from_the_anchor_set(self):
        contested_ids = {case["case_id"] for case in contested_cases()}
        anchor_ids = {case["case_id"] for case in anchor_cases()}
        self.assertEqual(contested_ids & anchor_ids, set())
        for case in contested_cases():
            self.assertFalse(case["anchor"])

    def test_no_contested_case_leaks_into_the_script_anchor_gate(self):
        gated = {(a["ticker"], a["brief_date"]) for a in ANCHORS}
        for case in contested_cases():
            key = (case["event"]["ticker"], case["event"]["brief_date"])
            self.assertNotIn(key, gated)


class TestPromptBlindnessOnFrozenPayloads(unittest.TestCase):
    """Blindness proven on the payload shapes the instrument actually meets -
    entity-less events, empty implication lists, non-ASCII headlines - rather
    than on one hand-written row."""

    def test_every_case_renders_under_every_framing(self):
        cases = load_cases()
        self.assertTrue(cases)
        for case in cases:
            for framing in FRAMINGS:
                with self.subTest(case=case["case_id"], framing=framing):
                    prompt = build_role_prompt(_row_with_verdict_fields(case), framing=framing)
                    self.assertTrue(prompt.strip())

    def test_no_rendered_prompt_carries_a_pipeline_verdict_field(self):
        for case in load_cases():
            for framing in FRAMINGS:
                with self.subTest(case=case["case_id"], framing=framing):
                    prompt = build_role_prompt(_row_with_verdict_fields(case), framing=framing)
                    self.assertEqual(
                        blindness_violations(prompt),
                        [],
                        f"{case['case_id']} leaked a pipeline verdict field under {framing}",
                    )

    def test_every_rendered_prompt_carries_the_facts_needed_to_judge_a_channel(self):
        """Guards the blindness assertion above from passing on an empty prompt:
        the payload has to reach the prompt for its absence of verdict fields to
        mean anything."""
        for case in load_cases():
            event = case["event"]
            for framing in FRAMINGS:
                with self.subTest(case=case["case_id"], framing=framing):
                    prompt = build_role_prompt(_row_with_verdict_fields(case), framing=framing)
                    for needed in (
                        event["ticker"],
                        event["company_name"],
                        event["theme"],
                        event["source_event_title"],
                        event["catalyst_event_type"],
                    ):
                        self.assertIn(needed, prompt)
                    for entity in event["primary_entities"]:
                        self.assertIn(entity, prompt)
                    for implication in event["second_order_implications"]:
                        self.assertIn(implication, prompt)
                    for role in ROLES:
                        self.assertIn(role, prompt)


class TestBlindnessCheckerPositiveControl(unittest.TestCase):
    """Positive control for the blindness assertion above. Without it the check
    could rot to vacuous - a checker that never fires passes every prompt."""

    def test_checker_fires_on_a_leaked_field_name(self):
        leaky = "COMPANY\n  Ticker: LYFT\n  layer4_weighted_score: 2\n"
        self.assertIn("layer4_weighted_score", blindness_violations(leaky))

    def test_checker_fires_on_a_leaked_verdict_value_without_its_field_name(self):
        leaky = f"EVENT\n  Prior read: {VERDICT_PROBE_VALUES['rationale']}\n"
        violations = blindness_violations(leaky)
        self.assertTrue(violations)
        self.assertTrue(any(v.startswith("rationale=") for v in violations))

    def test_checker_fires_once_per_leaked_field(self):
        leaky = "\n".join(
            [f"{name}: {value}" for name, value in VERDICT_PROBE_VALUES.items()],
        )
        violations = blindness_violations(leaky)
        for name in EXCLUDED_FIELDS:
            self.assertIn(name, violations)

    def test_checker_is_silent_on_a_real_rendered_prompt(self):
        case = anchor_cases()[0]
        prompt = build_role_prompt(_row_with_verdict_fields(case), framing="strict")
        self.assertEqual(blindness_violations(prompt), [])


class TestCaseSetIsNotVacuous(unittest.TestCase):
    """An empty or truncated fixture would make every loop above pass by
    iterating zero times."""

    def test_at_least_the_six_frozen_anchor_cases_are_present(self):
        self.assertGreaterEqual(len(anchor_cases()), _MIN_ANCHOR_CASES)

    def test_the_case_set_holds_more_than_its_anchors(self):
        self.assertGreater(len(load_cases()), len(anchor_cases()))

    def test_the_anchors_span_more_than_one_role(self):
        """One anchor role would make the gate a coin flip that only checks the
        instrument's favourite answer."""
        self.assertGreaterEqual(len({case["expected_role"] for case in anchor_cases()}), 3)


if __name__ == "__main__":
    unittest.main()
