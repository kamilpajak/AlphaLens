"""CLI tests for ``alphalens broker`` P2 order commands (submit / orders / cancel).

The submit command is DRY-RUN BY DEFAULT (bracket table + precheck, nothing
sent); ``--execute`` additionally requires an interactive confirmation
(``--yes`` skips it — the first confirmation pattern in alphalens_cli).
Broker + brief loading are patched at their source modules (the CLI
lazy-imports inside command bodies, so source-module patches are picked up at
call time).
"""

from __future__ import annotations

import ast
import datetime as dt
import unittest
import uuid
from pathlib import Path
from unittest import mock

from alphalens_pipeline.brokers.contract import (
    AccountSnapshot,
    BracketOrderRequest,
    BrokerError,
    InstrumentNotFoundError,
    InstrumentRef,
    OrderState,
    OrderStatus,
    PlacedOrder,
)
from alphalens_pipeline.paper.brief_loader import CandidateBrief
from typer.testing import CliRunner

_TRADE_SETUP = {
    "schema_version": "1.1.0",
    "status": "OK",
    "asof_close": 55.0,
    "atr": 1.5,
    "disaster_stop": 40.0,
    "suggested_size_pct": 3.0,
    "order_ttl_days": 5,
    "entry_tiers": [{"limit": 50.0, "alloc_pct": 100.0, "atr_distance": 1.0, "tag": "t0"}],
    "tp_tranches": [{"target": 60.0, "tranche_pct": 100.0, "r_multiple": 1.0, "tag": "tp0"}],
    "builder_config_version": "setup-v1-test",
}

_BRIEF_DATE = dt.date(2026, 7, 16)


def _candidate(ticker: str = "KO") -> CandidateBrief:
    return CandidateBrief(
        brief_date=_BRIEF_DATE,
        ticker=ticker,
        theme="test-theme",
        verified=True,
        suggested_size_pct=3.0,
        trade_setup=dict(_TRADE_SETUP),
        n_gates_passed=3,
        n_gates_failed=0,
        layer4_weighted_score=1.0,
        scorer_config_version="scorer-v1-test",
    )


def _instrument() -> InstrumentRef:
    return InstrumentRef(
        ticker="KO",
        exchange_mic="XNYS",
        asset_type="Stock",
        broker_instrument_id="307",
        broker_symbol="ko:xnys",
    )


class _CliFakeBroker:
    name = "fake"

    def __init__(self):
        self.place_calls: list[BracketOrderRequest] = []
        self.precheck_calls: list[BracketOrderRequest] = []
        self.cancel_calls: list[str] = []
        self.place_error: BrokerError | None = None

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            account_id="AK-1",
            currency="USD",
            cash=90_000.0,
            total_value=100_000.0,
            margin_available=None,
            asof=dt.datetime.now(dt.UTC),
        )

    def get_positions(self):
        return []

    def resolve_instrument(self, ticker: str, exchange_mic: str = "XNYS") -> InstrumentRef:
        if exchange_mic != "XNYS":
            raise InstrumentNotFoundError(f"no ({ticker}, {exchange_mic})")
        return _instrument()

    def precheck_bracket_order(self, request: BracketOrderRequest) -> dict:
        self.precheck_calls.append(request)
        return {"PreCheckResult": "Ok", "EstimatedCashRequired": 3_000.0}

    def place_bracket_order(self, request: BracketOrderRequest) -> PlacedOrder:
        if self.place_error is not None:
            raise self.place_error
        self.place_calls.append(request)
        seq = len(self.place_calls)
        return PlacedOrder(entry_order_id=f"E-{seq}", exit_order_ids=(f"T-{seq}", f"S-{seq}"))

    def get_order(self, order_id: str) -> OrderState:
        return OrderState(order_id, OrderStatus.WORKING, None, 0.0, "Working")

    def list_open_orders(self) -> list[OrderState]:
        return [OrderState("E-1", OrderStatus.WORKING, _instrument(), 0.0, "Working")]

    def cancel_order(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)


class _SubmitHarness:
    """Patches registry/brief-loader/submission-log at their source modules."""

    def __init__(self, case: unittest.TestCase, *, candidates: list[CandidateBrief] | None = None):
        self.broker = _CliFakeBroker()
        self.appended: list[dict] = []

        def _fake_append(record: dict, *, path: Path | None = None) -> Path:
            self.appended.append(record)
            return path or Path("/tmp/submissions-test.jsonl")

        rows = candidates if candidates is not None else [_candidate()]
        patches = [
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker",
                return_value=self.broker,
            ),
            mock.patch("alphalens_pipeline.paper.brief_loader.load_brief", return_value=rows),
            mock.patch(
                "alphalens_pipeline.brokers.submission_log.append_submission_record",
                side_effect=_fake_append,
            ),
        ]
        for patch in patches:
            patch.start()
            case.addCleanup(patch.stop)


_SUBMIT_ARGS = ["submit", "KO", "--date", "2026-07-16", "--equity", "100000"]


class TestSubmitDryRun(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_dry_run_prints_table_prechecks_and_sends_nothing(self):
        harness = _SubmitHarness(self)
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(broker_app, _SUBMIT_ARGS)

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("DRY-RUN", result.output)
        self.assertIn("client_request_id", result.output, "bracket table header expected")
        # 3% of 100k at 50 = 60 shares on the single tier.
        self.assertIn("60", result.output)
        self.assertEqual(len(harness.broker.precheck_calls), 1, "dry-run STILL prechecks")
        self.assertEqual(harness.broker.place_calls, [], "dry-run must send NOTHING")
        self.assertEqual(harness.appended, [], "dry-run must not journal")

    def test_unknown_ticker_fails_cleanly(self):
        _SubmitHarness(self, candidates=[_candidate("OTHER")])
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(broker_app, _SUBMIT_ARGS)

        self.assertNotEqual(result.exit_code, 0)


class TestSubmitExecute(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_execute_without_confirmation_aborts(self):
        harness = _SubmitHarness(self)
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(broker_app, [*_SUBMIT_ARGS, "--execute"], input="n\n")

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(harness.broker.place_calls, [], "declined confirm must send NOTHING")
        self.assertEqual(harness.appended, [])

    def test_execute_yes_places_prints_ids_and_token_and_journals(self):
        harness = _SubmitHarness(self)
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(broker_app, [*_SUBMIT_ARGS, "--execute", "--yes"])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(len(harness.broker.place_calls), 1)
        self.assertIn("placed entry=E-1", result.output)
        self.assertIn("T-1", result.output)
        self.assertIn("execution_config_version execution-v1-", result.output)

        (record,) = harness.appended
        self.assertEqual(record["ticker"], "KO")
        self.assertEqual(record["mic"], "XNYS")
        self.assertEqual(record["uic"], "307")
        self.assertTrue(record["execution_config_version"].startswith("execution-v1-"))
        (bracket,) = record["brackets"]
        self.assertEqual(bracket["entry_order_id"], "E-1")
        self.assertEqual(bracket["qty"], 60)
        request = harness.broker.place_calls[0]
        self.assertEqual(bracket["client_request_id"], request.client_request_id)
        uuid.UUID(request.client_request_id)

    def test_execute_failure_journals_partial_run_and_fails_loudly(self):
        harness = _SubmitHarness(self)
        harness.broker.place_error = BrokerError("Saxo rejected bracket")
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(broker_app, [*_SUBMIT_ARGS, "--execute", "--yes"])

        self.assertNotEqual(result.exit_code, 0)
        (record,) = harness.appended
        self.assertIn("placement stopped after 0/1", record["note"])


class TestOrdersAndCancel(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_orders_lists_open_orders(self):
        harness = _SubmitHarness(self)
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(broker_app, ["orders"])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("E-1", result.output)
        self.assertIn("WORKING", result.output)
        self.assertEqual(harness.broker.place_calls, [])

    def test_cancel_happy_path(self):
        harness = _SubmitHarness(self)
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(broker_app, ["cancel", "E-1"])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(harness.broker.cancel_calls, ["E-1"])
        self.assertIn("cascade", result.output)


class TestCliImportsStayLazy(unittest.TestCase):
    def test_no_top_level_brokers_import_in_command_module(self):
        """The +913ms lazy-CLI doctrine: brokers imports live in command bodies."""
        import alphalens_cli.commands.broker as broker_cmd

        tree = ast.parse(Path(broker_cmd.__file__).read_text())
        top_level_modules: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level_modules.append(node.module)
        offenders = [m for m in top_level_modules if m.startswith("alphalens_pipeline")]
        self.assertEqual(offenders, [], "brokers/pipeline imports must stay inside command bodies")


if __name__ == "__main__":
    unittest.main()
