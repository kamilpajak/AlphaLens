"""Hermetic tests for the pure-predicate safety gate.

check is a pure function of inputs + two rails read at call time (KILL file,
ALLOW_ORDERS). Writes nothing — even the daily-loss branch RETURNS Refuse. One
refusal branch per test; first failing rail wins.
"""

from __future__ import annotations

import os
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.brokers.automanager.safety import (
    ALLOW_ORDERS_ENV,
    DAILY_LOSS_LIMIT_R_ENV,
    MAX_OPEN_ENV,
    Allow,
    BrokerView,
    JournalView,
    Refuse,
    check,
)


@dataclass
class _StubSession:
    alive: bool


_PICK = object()  # check()'s `pick` arg is vestigial — never referenced in the body
_CLEAR_JOURNAL = JournalView(open_bracket_count=0, realized_r_today=0.0)
_CLEAR_BROKER = BrokerView(open_position_count=0, equity=1_000.0)


class SafetyGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.kill = Path(self._tmp.name) / "KILL"
        # A GLOBAL kill path (D3, ADR 0016) — never written to by default, so
        # every existing check() call below stays hermetic (pinned to this
        # test's own temp dir, never the real ~/.alphalens/broker_orders/KILL
        # default).
        self.global_kill = Path(self._tmp.name) / "GLOBAL_KILL"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "1"}, clear=False)
    def test_all_rails_clear_allows(self) -> None:
        d = check(
            _PICK,
            _CLEAR_JOURNAL,
            _CLEAR_BROKER,
            _StubSession(alive=True),
            kill_path=self.kill,
            global_kill_path=self.global_kill,
        )
        self.assertIsInstance(d, Allow)

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "1"}, clear=False)
    def test_kill_file_present_refuses_first(self) -> None:
        self.kill.write_text("stop", encoding="utf-8")
        d = check(
            _PICK,
            _CLEAR_JOURNAL,
            _CLEAR_BROKER,
            _StubSession(alive=True),
            kill_path=self.kill,
            global_kill_path=self.global_kill,
        )
        self.assertIsInstance(d, Refuse)
        self.assertIn("KILL", d.reason)
        # An emergency PAUSE must never permanently retire the queue.
        self.assertFalse(d.terminal)

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "1"}, clear=False)
    def test_global_kill_file_present_refuses_even_without_instance_kill(self) -> None:
        """D3 (ADR 0016): the GLOBAL kill gates placement in addition to the
        per-instance kill — defense in depth."""
        self.global_kill.write_text("halt everything", encoding="utf-8")
        d = check(
            _PICK,
            _CLEAR_JOURNAL,
            _CLEAR_BROKER,
            _StubSession(alive=True),
            kill_path=self.kill,
            global_kill_path=self.global_kill,
        )
        self.assertIsInstance(d, Refuse)
        self.assertIn("GLOBAL KILL", d.reason)
        self.assertFalse(d.terminal)

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "1"}, clear=False)
    def test_dead_chain_refuses(self) -> None:
        d = check(
            _PICK,
            _CLEAR_JOURNAL,
            _CLEAR_BROKER,
            _StubSession(alive=False),
            kill_path=self.kill,
            global_kill_path=self.global_kill,
        )
        self.assertIsInstance(d, Refuse)
        self.assertIn("chain", d.reason.lower())
        # Auth self-heals via `broker auth` — transient, never terminal.
        self.assertFalse(d.terminal)

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "0"}, clear=False)
    def test_allow_orders_not_set_refuses(self) -> None:
        d = check(
            _PICK,
            _CLEAR_JOURNAL,
            _CLEAR_BROKER,
            _StubSession(alive=True),
            kill_path=self.kill,
            global_kill_path=self.global_kill,
        )
        self.assertIsInstance(d, Refuse)
        self.assertIn(ALLOW_ORDERS_ENV, d.reason)
        # Master-arm off = documented inert observation mode. Marking it
        # terminal would retire the WHOLE armed queue on the daemon's first
        # tick — the pick must stay armed until the operator arms orders.
        self.assertFalse(d.terminal)

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "1", MAX_OPEN_ENV: "2"}, clear=False)
    def test_max_open_cap_refuses_terminally(self) -> None:
        journal = JournalView(open_bracket_count=1, realized_r_today=0.0)
        broker = BrokerView(open_position_count=1, equity=1_000.0)
        d = check(
            _PICK,
            journal,
            broker,
            _StubSession(alive=True),
            kill_path=self.kill,
            global_kill_path=self.global_kill,
        )
        self.assertIsInstance(d, Refuse)
        self.assertIn("MAX_OPEN", d.reason)
        # Capacity refusal is terminal: retrying every tick would self-place a
        # stale brief signal once capacity frees.
        self.assertTrue(d.terminal)

    @mock.patch.dict(
        os.environ, {ALLOW_ORDERS_ENV: "1", DAILY_LOSS_LIMIT_R_ENV: "3.0"}, clear=False
    )
    def test_daily_loss_limit_refuses_without_side_effects(self) -> None:
        journal = JournalView(open_bracket_count=0, realized_r_today=-3.5)
        d = check(
            _PICK,
            journal,
            _CLEAR_BROKER,
            _StubSession(alive=True),
            kill_path=self.kill,
            global_kill_path=self.global_kill,
        )
        self.assertIsInstance(d, Refuse)
        self.assertIn("loss", d.reason.lower())
        self.assertFalse(self.kill.exists())
        # Day-scoped lockout: the pick may place tomorrow, so not terminal.
        self.assertFalse(d.terminal)

    def test_refuse_default_is_non_terminal(self) -> None:
        # Fail-safe default: a Refuse constructed without an explicit flag must
        # never retire a pick.
        self.assertFalse(Refuse(reason="anything").terminal)

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "1"}, clear=False)
    def test_defaults_resolve_via_the_state_paths_seam_when_omitted(self) -> None:
        """Neither kill_path nor global_kill_path passed -> both fall back
        through state_paths.{kill_file_path,global_kill_file_path}() — proven
        against a fresh, empty temp home so neither can accidentally exist."""
        with TemporaryDirectory() as home_dir:
            with mock.patch("pathlib.Path.home", return_value=Path(home_dir)):
                d = check(_PICK, _CLEAR_JOURNAL, _CLEAR_BROKER, _StubSession(alive=True))
        self.assertIsInstance(d, Allow)


if __name__ == "__main__":
    unittest.main()
