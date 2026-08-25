"""The LIVE systemd config in this repository must describe a daemon that can
actually boot — and, on its own, one that cannot trade.

WHY THIS TEST EXISTS. Until 2026-08-25 the repository's LIVE unit declared five
values the VPS did not run, and four more that ran only from an untracked
drop-in (issue #1121). Reading `deploy/systemd/` gave a confidently wrong answer
about how much money was at risk. Tracking the drop-in fixes the lie once; this
test is what stops it being re-introduced by an edit, because it reads the same
files a deploy copies and runs the real `assert_live_rails()` over them.

WHAT IT CANNOT DO. It compares the repository against itself. Nothing here can
see the VPS, so a hand edit made directly on the host is still invisible — that
is a separate, host-side drift check. What this pins is that what we SHIP is
bootable, disarmed by default, and inside the soak bounds.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from alphalens_pipeline.brokers.automanager.live_rails import assert_live_rails
from broker_contract.contract import BrokerCapabilityError

REPO_ROOT = Path(__file__).resolve().parents[3]
_UNIT = REPO_ROOT / "deploy" / "systemd" / "alphalens-broker-manager-live.service"
_DROPIN_DIR = REPO_ROOT / "deploy" / "systemd" / "alphalens-broker-manager-live.service.d"

_ALLOW_ORDERS = "ALPHALENS_BROKER_ALLOW_ORDERS"
_SIZING_EQUITY = "ALPHALENS_BROKER_SIZING_EQUITY"


def _environment_assignments(text: str) -> dict[str, str]:
    """The ``Environment=`` assignments in one unit or drop-in file.

    systemd's ``Environment=`` takes a space-separated LIST of assignments, so
    one line may carry several; this splits on whitespace rather than reading
    the line as a single pair. That is only sound because a value containing a
    space would have to be QUOTED, and
    :func:`test_every_environment_line_is_the_simple_form` refuses quoting.

    Still deliberately narrow: no quoting, no line continuations, no bare
    ``Environment=`` reset. Anything this parser cannot read honestly, that
    same test refuses outright rather than letting it be misread quietly.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Environment="):
            continue
        for assignment in stripped[len("Environment=") :].split():
            key, _, value = assignment.partition("=")
            values[key.strip()] = value.strip()
    return values


def _dropin_files() -> list[Path]:
    # systemd applies drop-ins in lexical filename order, so sorted() is the
    # composition order, not merely a tidy one.
    return sorted(_DROPIN_DIR.glob("*.conf"))


def _base_environment() -> dict[str, str]:
    return _environment_assignments(_UNIT.read_text())


def _composed_environment() -> dict[str, str]:
    composed = _base_environment()
    for path in _dropin_files():
        composed.update(_environment_assignments(path.read_text()))
    return composed


class TestTheShippedLiveConfigBoots(unittest.TestCase):
    def test_the_composed_config_passes_the_live_boot_assert(self):
        # Base unit + every tracked drop-in, exactly as a deploy composes them.
        with mock.patch.dict("os.environ", _composed_environment(), clear=True):
            assert_live_rails()

    def test_the_base_unit_alone_also_boots(self):
        # A partial install (unit copied, drop-in directory forgotten) must fail
        # SAFE, not fail to start: all eight pins have to be present here too,
        # or the operator gets a dead daemon instead of a conservative one.
        with mock.patch.dict("os.environ", _base_environment(), clear=True):
            assert_live_rails()

    def test_the_gate_can_actually_fail(self):
        # A green result above means nothing unless this file can go red. One
        # typo'd digit in the declared frame — the exact shape #1121 left
        # unbounded — must be refused.
        broken = dict(_composed_environment(), **{_SIZING_EQUITY: "150000"})
        with mock.patch.dict("os.environ", broken, clear=True):
            with self.assertRaises(BrokerCapabilityError):
                assert_live_rails()


class TestTheTemplateIsDisarmed(unittest.TestCase):
    def test_the_base_unit_does_not_arm_placement(self):
        # This unit is a template in a public repository. Installing it must
        # never be the act that starts trading real money — arming is a
        # separate, deliberate drop-in.
        self.assertEqual(_base_environment().get(_ALLOW_ORDERS), "0")

    def test_arming_lives_in_exactly_one_drop_in(self):
        arming = [p.name for p in _dropin_files() if _ALLOW_ORDERS in p.read_text()]
        self.assertEqual(arming, ["10-allow-orders.conf"])


class TestTheDropInsAreReadableTheWayTheyAreApplied(unittest.TestCase):
    def test_every_environment_line_is_the_simple_form(self):
        # Guards the narrow parser above. systemd supports quoting, escaped
        # newlines and a bare `Environment=` reset; none of them appear in
        # these files today, and the parser would misread all three.
        for path in [_UNIT, *_dropin_files()]:
            for line in path.read_text().splitlines():
                stripped = line.strip()
                if not stripped.startswith("Environment="):
                    continue
                assignments = stripped[len("Environment=") :]
                with self.subTest(file=path.name, line=stripped):
                    self.assertNotIn('"', assignments, "quoted value is not parsed")
                    self.assertNotIn("'", assignments, "quoted value is not parsed")
                    self.assertFalse(assignments.endswith("\\"), "continuation is not parsed")
                    self.assertTrue(assignments.split(), "bare Environment= reset is not parsed")
                    for token in assignments.split():
                        self.assertIn("=", token, f"{token!r} is not an assignment")

    def test_several_assignments_on_one_line_are_read_as_several(self):
        # systemd's Environment= takes a space-separated LIST of assignments,
        # and this parser folded such a line into one key with a garbage value
        # while the guard above waved it through — so a future edit in that
        # form would have made this whole file green against a composition
        # systemd does not produce. No file uses the form today; the parser has
        # to survive one that does.
        parsed = _environment_assignments("Environment=FIRST=1 SECOND=2\n")
        self.assertEqual(parsed, {"FIRST": "1", "SECOND": "2"})

    def test_no_variable_is_set_by_two_drop_ins(self):
        # While that holds, filename order decides nothing. The SIM unit lost
        # this property and had to grow a `zz-` prefixed file to win an
        # ordering fight; the numeric prefixes here exist to keep it.
        seen: dict[str, str] = {}
        for path in _dropin_files():
            for key in _environment_assignments(path.read_text()):
                with self.subTest(variable=key):
                    self.assertNotIn(key, seen, f"also set by {seen.get(key)}")
                seen[key] = path.name


if __name__ == "__main__":
    unittest.main()
