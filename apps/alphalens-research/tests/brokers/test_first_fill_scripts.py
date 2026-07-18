"""Hermetic tests for the attended first-fill experiment driver scripts (G2).

The scripts under ``apps/alphalens-research/scripts/first_fill/`` are the
attended-session drivers for the Saxo SIM first-fill experiment
(``docs/research/saxo_first_fill_experiment_2026_07_18.md``). Contract pinned
here, with NO network (fakes stand in for SaxoBroker / SaxoClient):

- every placement goes through ``Broker.place_bracket_order`` (never raw HTTP)
  and is journaled via ``build_submission_record``/``append_submission_record``
  so ``alphalens broker reconcile`` stays the verdict engine — Python-API
  placements are otherwise invisible to it;
- every raw payload is dumped to the session scratch dir as numbered JSON;
- ``step_a_entry --naked`` builds the childless qty-10 probe (stop=None,
  tp=None); ``step_c_close`` defaults to the naked SELL close whose journal
  record must reconcile to r=None (stop=None, honest-None by design);
- placement failure still journals (note-only record) and exits non-zero.

The scripts import their shared ``_common`` module by script-dir convention
(``sys.path[0]`` when run as a file), so the tests load them the same way.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.contract import (
    BracketOrderRequest,
    InstrumentRef,
    OrderRejectedError,
    OrderState,
    OrderStatus,
    PlacedOrder,
)

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "first_fill"


def _load(name: str):
    """Import a first_fill script the way ``python <file>`` would (dir on path)."""
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    return importlib.import_module(name)


def _instrument() -> InstrumentRef:
    return InstrumentRef(
        ticker="KO",
        exchange_mic="XNYS",
        asset_type="Stock",
        broker_instrument_id="307",
        broker_symbol="ko:xnys",
    )


class _FakeBroker:
    """Duck-typed Broker: records placements, cans open-order snapshots."""

    def __init__(
        self,
        *,
        open_order_snapshots: list[set[str]] | None = None,
        place_error: Exception | None = None,
    ):
        self.placed_requests: list[BracketOrderRequest] = []
        self.resolve_calls: list[tuple[str, str]] = []
        self.open_order_snapshots = open_order_snapshots if open_order_snapshots else [set()]
        self.place_error = place_error

    def resolve_instrument(self, ticker: str, exchange_mic: str = "XNYS") -> InstrumentRef:
        self.resolve_calls.append((ticker, exchange_mic))
        return _instrument()

    def place_bracket_order(self, request: BracketOrderRequest) -> PlacedOrder:
        if self.place_error is not None:
            raise self.place_error
        self.placed_requests.append(request)
        return PlacedOrder(entry_order_id="E-1", exit_order_ids=("T-2", "S-3"))

    def list_open_orders(self) -> list[OrderState]:
        snapshot = (
            self.open_order_snapshots.pop(0)
            if len(self.open_order_snapshots) > 1
            else self.open_order_snapshots[0]
        )
        return [
            OrderState(
                order_id=order_id,
                status=OrderStatus.WORKING,
                instrument=None,
                filled_quantity=0.0,
                raw_status="Working",
            )
            for order_id in sorted(snapshot)
        ]


class _FakeClient:
    """Duck-typed SaxoClient for the read-only scripts."""

    def __init__(self, *, activities: dict[str, Any] | None = None):
        self.activities = activities or {"__count": 0, "Data": []}
        self.activity_calls: list[dict[str, Any]] = []
        self.get_json_calls: list[str] = []

    def get_client_info(self) -> dict[str, Any]:
        return {"ClientKey": "CK-1"}

    def get_order_activities(
        self,
        client_key: str,
        *,
        order_id: str | None = None,
        entry_type: str = "Last",
        from_datetime: str | None = None,
        top: int | None = None,
    ) -> dict[str, Any]:
        self.activity_calls.append(
            {"client_key": client_key, "order_id": order_id, "entry_type": entry_type}
        )
        return dict(self.activities)

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.get_json_calls.append(path)
        return {"ClientKey": "CK-1", "PositionNettingProfile": "FifoEndOfDay"}


class _ScratchCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.scratch = Path(self._tmp.name) / "scratch"
        self.journal = Path(self._tmp.name) / "submissions.jsonl"

    def _journal_records(self) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in self.journal.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _run(self, module, argv: list[str], **kwargs: Any) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = module.main(argv, **kwargs)
        return rc, stdout.getvalue()


class TestStepAEntry(_ScratchCase):
    def _argv(self, *extra: str) -> list[str]:
        return [
            "--qty",
            "2",
            "--entry",
            "70.55",
            "--stop",
            "68.43",
            "--tp",
            "72.67",
            "--brief-date",
            "2026-07-18",
            "--scratch",
            str(self.scratch),
            "--journal",
            str(self.journal),
            *extra,
        ]

    def test_places_bracket_journals_and_dumps(self):
        module = _load("step_a_entry")
        broker = _FakeBroker()

        rc, out = self._run(module, self._argv("--no-wait"), broker=broker)

        self.assertEqual(rc, 0)
        self.assertEqual(broker.resolve_calls, [("KO", "XNYS")])
        (request,) = broker.placed_requests
        self.assertEqual((request.side, request.quantity, request.entry_limit), ("BUY", 2, 70.55))
        self.assertEqual((request.stop_loss, request.take_profit), (68.43, 72.67))
        self.assertEqual(request.entry_ttl_days, 1)
        uuid.UUID(request.client_request_id)  # must be a real uuid4 token

        (record,) = self._journal_records()
        self.assertEqual((record["brief_date"], record["ticker"]), ("2026-07-18", "KO"))
        # Poolability: the experiment's journal rows must carry the SAME
        # config-version stamp as production submits, or reconcile-era
        # analyses would silently pool rows across decomposer generations.
        from alphalens_pipeline.brokers.execution import execution_config_version

        self.assertEqual(record["execution_config_version"], execution_config_version())
        self.assertEqual((record["mic"], record["uic"]), ("XNYS", "307"))
        (bracket,) = record["brackets"]
        self.assertEqual(bracket["entry_order_id"], "E-1")
        self.assertEqual(bracket["exit_order_ids"], ["T-2", "S-3"])
        self.assertEqual(
            (bracket["qty"], bracket["entry"], bracket["stop"], bracket["tp"], bracket["ttl"]),
            (2, 70.55, 68.43, 72.67, 1),
        )
        self.assertEqual(bracket["client_request_id"], request.client_request_id)
        self.assertIn("first-fill experiment phase A", record["note"])

        dump = json.loads((self.scratch / "10_entry_place.json").read_text(encoding="utf-8"))
        self.assertEqual(dump["placed"]["entry_order_id"], "E-1")
        self.assertEqual(dump["request"]["client_request_id"], request.client_request_id)
        self.assertIn("ts_utc", dump)
        self.assertIn("E-1", out)

    def test_naked_flag_forces_childless_probe(self):
        module = _load("step_a_entry")
        broker = _FakeBroker()

        rc, _ = self._run(
            module,
            [
                "--qty",
                "10",
                "--entry",
                "70.48",
                "--naked",
                "--brief-date",
                "2026-07-18",
                "--scratch",
                str(self.scratch),
                "--journal",
                str(self.journal),
                "--no-wait",
            ],
            broker=broker,
        )

        self.assertEqual(rc, 0)
        (request,) = broker.placed_requests
        self.assertIsNone(request.stop_loss)
        self.assertIsNone(request.take_profit)

    def test_naked_flag_conflicts_with_stop_or_tp(self):
        module = _load("step_a_entry")
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                module.main(self._argv("--naked"), broker=_FakeBroker())

    def test_wait_polls_until_entry_absent(self):
        module = _load("step_a_entry")
        broker = _FakeBroker(open_order_snapshots=[{"E-1"}, {"E-1"}, set()])
        sleeps: list[float] = []

        rc, out = self._run(module, self._argv(), broker=broker, sleep=sleeps.append)

        self.assertEqual(rc, 0)
        self.assertEqual(sleeps, [3.0, 3.0])
        self.assertIn("left the open-orders view", out)

    def test_placement_failure_still_journals_and_exits_nonzero(self):
        module = _load("step_a_entry")
        broker = _FakeBroker(place_error=OrderRejectedError("precheck rejected"))

        rc, out = self._run(module, self._argv("--no-wait"), broker=broker)

        self.assertEqual(rc, 1)
        (record,) = self._journal_records()
        self.assertEqual(record["brackets"], [])
        self.assertIn("precheck rejected", record["note"])
        self.assertIn("precheck rejected", out)


class TestStepCClose(_ScratchCase):
    def _argv(self, *extra: str) -> list[str]:
        return [
            "--qty",
            "2",
            "--limit",
            "69.85",
            "--brief-date",
            "2026-07-18",
            "--scratch",
            str(self.scratch),
            "--journal",
            str(self.journal),
            *extra,
        ]

    def test_naked_sell_close_is_the_default(self):
        module = _load("step_c_close")
        broker = _FakeBroker()

        rc, _ = self._run(module, self._argv("--no-wait"), broker=broker)

        self.assertEqual(rc, 0)
        (request,) = broker.placed_requests
        self.assertEqual((request.side, request.quantity, request.entry_limit), ("SELL", 2, 69.85))
        self.assertIsNone(request.stop_loss, "the close is naked: stop=None")
        self.assertIsNone(request.take_profit, "the close is naked: tp=None")
        self.assertEqual(request.entry_ttl_days, 1)

        (record,) = self._journal_records()
        self.assertIn("stop=None", record["note"], "journal note must flag the honest r=None")
        (bracket,) = record["brackets"]
        self.assertIsNone(bracket["stop"])
        self.assertIsNone(bracket["tp"])
        self.assertTrue((self.scratch / "30_close_place.json").exists())

    def test_contingency_stop_only_close_accepts_stop(self):
        # Fallback for a rejected None/None body: SELL + stop ABOVE the entry
        # (valid SELL geometry); the orphan child is cancelled post-fill.
        module = _load("step_c_close")
        broker = _FakeBroker()

        rc, _ = self._run(module, self._argv("--stop", "72.67", "--no-wait"), broker=broker)

        self.assertEqual(rc, 0)
        (request,) = broker.placed_requests
        self.assertEqual(request.stop_loss, 72.67)
        self.assertIsNone(request.take_profit)


class TestDumpActivities(_ScratchCase):
    def test_all_flag_requests_entry_type_all_and_dumps(self):
        module = _load("dump_activities")
        payload = {
            "__count": 1,
            "Data": [
                {
                    "OrderId": "E-1",
                    "LogId": 7,
                    "Status": "FinalFill",
                    "SubStatus": "Confirmed",
                    "FilledAmount": 2,
                }
            ],
        }
        client = _FakeClient(activities=payload)

        rc, out = self._run(
            module,
            ["E-1", "--all", "--scratch", str(self.scratch), "--out-name", "11_entry_activities"],
            client=client,
        )

        self.assertEqual(rc, 0)
        (call,) = client.activity_calls
        self.assertEqual(call, {"client_key": "CK-1", "order_id": "E-1", "entry_type": "All"})
        dumped = json.loads((self.scratch / "11_entry_activities.json").read_text(encoding="utf-8"))
        self.assertEqual(dumped["Data"][0]["Status"], "FinalFill")
        self.assertIn("FinalFill", out)

    def test_default_is_entry_type_last(self):
        module = _load("dump_activities")
        client = _FakeClient()

        rc, _ = self._run(module, ["E-9", "--scratch", str(self.scratch)], client=client)

        self.assertEqual(rc, 0)
        self.assertEqual(client.activity_calls[0]["entry_type"], "Last")
        self.assertTrue((self.scratch / "activities_E-9_last.json").exists())


class TestCommonHelpers(_ScratchCase):
    def test_scratch_dir_honors_the_scratch_env_var(self):
        module = _load("_common")
        target = Path(self._tmp.name) / "env_scratch"
        with mock.patch.dict("os.environ", {module.SCRATCH_ENV: str(target)}):
            resolved = module.scratch_dir(None)
        self.assertEqual(resolved, target)
        self.assertTrue(target.is_dir(), "scratch_dir must create the directory")

    def test_poll_returns_false_after_the_timeout_budget(self):
        module = _load("_common")
        broker = _FakeBroker(open_order_snapshots=[{"E-1"}])
        clock = iter([0.0, 100.0])
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            filled = module.poll_until_entry_absent(
                broker,
                "E-1",
                timeout_s=60.0,
                sleep=lambda _s: None,
                now=lambda: next(clock),
            )

        self.assertFalse(filled)
        self.assertIn("STILL WORKING", stdout.getvalue())


class TestReadNettingProfile(_ScratchCase):
    def test_reads_client_profile_and_echoes_netting(self):
        module = _load("read_netting_profile")
        client = _FakeClient()

        rc, out = self._run(module, ["--scratch", str(self.scratch)], client=client)

        self.assertEqual(rc, 0)
        self.assertEqual(client.get_json_calls, ["/port/v1/clients/CK-1"])
        dumped = json.loads((self.scratch / "01_client_profile.json").read_text(encoding="utf-8"))
        self.assertEqual(dumped["PositionNettingProfile"], "FifoEndOfDay")
        self.assertIn("PositionNettingProfile: FifoEndOfDay", out)


if __name__ == "__main__":
    unittest.main()
