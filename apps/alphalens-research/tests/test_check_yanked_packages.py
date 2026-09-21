"""Unit tests for the yanked-dependency gate (#1507).

Everything here is hermetic — ``tests/_net_guard.py`` forbids the suite from
touching the network, so the PyPI round trip is exercised only by the opt-in
probe in ``tests/live/``. What is tested here is the part that decides:
which pins get queried, how one payload is read, and what verdict a set of
results produces.

The load-bearing case is ``TestTheGateCanSeeTheRealLock``. A ``verdict()``-only
positive control proves the verdict function can say "yanked"; it says nothing
about whether anything ever reaches it. If ``registry_pins`` silently returned
an empty list — a key rename in uv's lock format would do it — every run would
report clean forever and every other test here would still pass.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import check_yanked_packages as gate

_REPO_ROOT = Path(__file__).resolve().parents[3]

_PYPI = "https://pypi.org/simple"


def _lock(*packages: dict) -> dict:
    return {"version": 1, "package": list(packages)}


def _registry(name: str, version: str, index: str = _PYPI) -> dict:
    return {"name": name, "version": version, "source": {"registry": index}}


class TestWhichPinsGetQueried(unittest.TestCase):
    def test_registry_entries_are_pinned_by_name_and_version(self):
        pins = gate.registry_pins(_lock(_registry("pandas", "3.0.6")))
        self.assertEqual(pins, [gate.Pin("pandas", "3.0.6")])

    def test_git_and_editable_entries_are_dropped(self):
        # They have no PyPI record at all: the git dep carries a version string
        # that means nothing to the index, and the five workspace members are
        # this repo. The existing pip-audit step drops the same two classes.
        pins = gate.registry_pins(
            _lock(
                _registry("pandas", "3.0.6"),
                {
                    "name": "phase-robust-backtesting",
                    "version": "0.3.0",
                    "source": {"git": "https://…"},
                },
                {
                    "name": "alphalens-pipeline",
                    "version": "0.1.0",
                    "source": {"editable": "apps/alphalens-pipeline"},
                },
            )
        )
        self.assertEqual(pins, [gate.Pin("pandas", "3.0.6")])

    def test_an_index_other_than_pypi_is_refused_not_skipped(self):
        # Silently skipping would mean a private-index package is never checked
        # while the run still reports clean — the exact shape this gate exists
        # to prevent. The lock has one index today; that must stay a decision.
        with self.assertRaises(gate.UnexpectedIndexError):
            gate.registry_pins(
                _lock(_registry("internal", "1.0", index="https://pypi.example.com/simple"))
            )

    def test_the_helper_can_return_nothing(self):
        # Positive control for the real-lock test below: the count it asserts
        # is only evidence if this function is capable of returning empty.
        self.assertEqual(gate.registry_pins(_lock()), [])


class TestTheGateCanSeeTheRealLock(unittest.TestCase):
    """The whole path from lockfile bytes to pins, over the repo's own lock.

    File IO only, no network. This is what keeps the rest of the suite honest.
    """

    def test_the_repo_lock_yields_a_plausible_pin_set(self):
        import tomllib

        with (_REPO_ROOT / "uv.lock").open("rb") as handle:
            pins = gate.registry_pins(tomllib.load(handle))

        # ~136 registry entries today. The band is wide on purpose — this
        # asserts "the parser still sees the lock", not a frozen dependency
        # count that every bump would have to update.
        self.assertGreater(len(pins), 50)
        names = {pin.name for pin in pins}
        self.assertIn("pandas", names)
        self.assertNotIn("alphalens-pipeline", names)  # editable member


class TestReadingOnePayload(unittest.TestCase):
    def test_a_yanked_release_carries_its_reason(self):
        state = gate.yank_state_from_payload(
            {
                "info": {
                    "yanked": True,
                    "yanked_reason": "Reported segfaults with datetime-related functionality",
                }
            }
        )
        self.assertEqual(
            state, (gate.YANKED, "Reported segfaults with datetime-related functionality")
        )

    def test_a_clean_release_is_clean(self):
        self.assertEqual(
            gate.yank_state_from_payload({"info": {"yanked": False, "yanked_reason": None}}),
            (gate.CLEAN, None),
        )

    def test_a_yank_with_no_stated_reason_is_still_a_yank(self):
        # PyPI allows an empty reason; reading that as "clean" would hide it.
        state, reason = gate.yank_state_from_payload(
            {"info": {"yanked": True, "yanked_reason": None}}
        )
        self.assertEqual(state, gate.YANKED)
        self.assertIsNotNone(reason)

    def test_a_payload_missing_the_key_is_permanent_not_transient(self):
        # Mirrors run_probes: an unexpected break is a real failure. Treating a
        # reshaped payload as a retryable blip would let the gate go quietly
        # blind the day PyPI changes its JSON.
        for payload in ({}, {"info": {}}, {"info": {"yanked": "true"}}):
            with self.subTest(payload=payload), self.assertRaises(gate.PermanentFetchError):
                gate.yank_state_from_payload(payload)


class TestTheVerdict(unittest.TestCase):
    def _clean(self, n: int) -> list[gate.PinResult]:
        return [gate.PinResult(gate.Pin(f"pkg{i}", "1.0"), gate.CLEAN, None) for i in range(n)]

    def test_every_pin_checked_and_clean_is_zero(self):
        report = gate.verdict(self._clean(135))
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.checked, 135)

    def test_one_yanked_pin_is_one_and_names_the_reason(self):
        # Positive control: the detector must be able to say non-zero.
        results = [
            *self._clean(134),
            gate.PinResult(gate.Pin("pandas", "3.0.4"), gate.YANKED, "segfaults"),
        ]
        report = gate.verdict(results)
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.yanked, [("pandas", "3.0.4", "segfaults")])

    def test_a_single_unchecked_pin_is_seven_not_zero(self):
        # THE rule. run_probes tolerates a minority of transients because one
        # flaky call proves nothing about a vendor contract. This is a
        # completeness check over a fixed set: one tolerated 429 is one package
        # nobody looked at, and the yank could be sitting in exactly that one.
        results = [
            *self._clean(135),
            gate.PinResult(gate.Pin("lonely", "1.0"), gate.UNCHECKED, "HTTP 429"),
        ]
        report = gate.verdict(results)
        self.assertEqual(report.exit_code, 7)
        self.assertEqual(report.unchecked, [("lonely", "1.0", "HTTP 429")])

    def test_a_yank_outranks_an_incomplete_run(self):
        # Both states present: the actionable one wins the exit code, and the
        # unchecked pins still have to appear in the report.
        results = [
            gate.PinResult(gate.Pin("pandas", "3.0.4"), gate.YANKED, "segfaults"),
            gate.PinResult(gate.Pin("lonely", "1.0"), gate.UNCHECKED, "timeout"),
        ]
        report = gate.verdict(results)
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(len(report.unchecked), 1)


class TestTheReportTheWorkflowReads(unittest.TestCase):
    """The glue between the script and the workflow's ``jq``, pinned here.

    The workflow builds an issue body out of these fields. Without this test a
    new log line or a renamed key reshapes the issue silently.
    """

    def test_the_json_document_carries_the_fields_the_workflow_reads(self):
        report = gate.verdict(
            [
                gate.PinResult(gate.Pin("pandas", "3.0.4"), gate.YANKED, "segfaults"),
                gate.PinResult(gate.Pin("ok", "1.0"), gate.CLEAN, None),
            ]
        )
        doc = report.as_json()
        self.assertEqual(doc["exit_code"], 1)
        self.assertEqual(doc["checked"], 2)
        self.assertEqual(
            doc["yanked"], [{"name": "pandas", "version": "3.0.4", "reason": "segfaults"}]
        )
        self.assertEqual(doc["unchecked"], [])
        json.dumps(doc)  # must survive the round trip the workflow does

    def test_main_writes_that_document_where_it_is_told(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "uv.lock"
            lock.write_text(
                'version = 1\n[[package]]\nname = "x"\nversion = "1.0"\nsource = { editable = "apps/x" }\n'
            )
            out = Path(tmp) / "report.json"

            code = gate.main(["--lock", str(lock), "--report-json", str(out)])

            self.assertEqual(code, 0)  # only an editable entry: nothing to query
            self.assertEqual(json.loads(out.read_text())["checked"], 0)


if __name__ == "__main__":
    unittest.main()
