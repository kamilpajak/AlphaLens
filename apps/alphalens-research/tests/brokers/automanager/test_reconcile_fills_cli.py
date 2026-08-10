"""CLI tests for ``alphalens broker reconcile-fills`` (build-seq 1b-ii).

The command joins each fired TP tranche (``tranche_fired`` telemetry line) to
its ACTUAL broker fill OFFLINE and writes the execution-quality parquet. It is
STRICTLY READ-ONLY against the broker (resolve + parquet write; no
place/cancel/amend).

The broker is injected by patching ``get_default_broker`` at its source module
(the CLI lazy-imports inside the command body). The journal is a REAL temp
JSONL file that ``control_loop._standalone_stop_journal_path`` is pointed at,
so the actual ``_iter_standalone_stop_journal`` reader is exercised
end-to-end.
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pandas as pd
from broker_contract.contract import OrderState, OrderStatus
from typer.testing import CliRunner


class _FakeResolverBroker:
    """Maps ``sell_order_id`` to a canned :class:`OrderState`; READ-ONLY.

    Deliberately exposes NO place/cancel/amend surface — a call to one would
    raise ``AttributeError`` and fail the read-only guarantee loudly.
    """

    name = "fake"

    def __init__(self, outcomes: dict[str, OrderState]):
        self._outcomes = outcomes
        self.resolved: list[str] = []

    def resolve_order_outcome(self, order_id: str) -> OrderState:
        self.resolved.append(order_id)
        return self._outcomes[order_id]


def _order_state(order_id: str, status: OrderStatus, *, filled=0.0, price=None) -> OrderState:
    return OrderState(
        order_id=order_id,
        status=status,
        instrument=None,
        filled_quantity=filled,
        raw_status=status.value,
        avg_fill_price=price,
    )


def _fired_line(uic: int, tag: str, *, sell_order_id: str | None, decision_bid: float) -> dict:
    telemetry: dict = {
        "decision_bid": decision_bid,
        "decision_ask": decision_bid + 0.10,
        "decision_mid": decision_bid + 0.05,
        "spread_abs": 0.10,
        "target_price": decision_bid + 1.0,
        "qty": 2,
        "event_time": "2026-07-21T14:30:00Z",
        "source": "yfinance",
    }
    if sell_order_id is not None:
        telemetry["sell_order_id"] = sell_order_id
    return {"kind": "tranche_fired", "uic": uic, "tag": tag, "telemetry": telemetry}


# A filled tranche below the decision bid (adverse -> positive slippage), a
# still-working (pending) tranche, an unresolved (UNKNOWN) tranche, plus two
# lines the reconciler must SKIP: a non-fired "planned" line and a fired line
# with no sell_order_id join key.
_FILL_PRICE = 81.5
_DECISION_BID = 82.0
_JOURNAL_LINES = [
    {"kind": "planned", "uic": 1, "client_request_id": "E-1", "stop_price": 40.0},
    _fired_line(11, "tp0", sell_order_id="S-FILL", decision_bid=_DECISION_BID),
    _fired_line(22, "tp0", sell_order_id="S-PEND", decision_bid=70.0),
    _fired_line(33, "tp0", sell_order_id="S-UNK", decision_bid=55.0),
    _fired_line(44, "tp0", sell_order_id=None, decision_bid=99.0),
]

_OUTCOMES = {
    "S-FILL": _order_state("S-FILL", OrderStatus.FILLED, filled=2.0, price=_FILL_PRICE),
    "S-PEND": _order_state("S-PEND", OrderStatus.WORKING),
    "S-UNK": _order_state("S-UNK", OrderStatus.UNKNOWN),
}

# 0.5 abs / 82.0 * 1e4 = ~60.98 bps.
_EXPECTED_SLIPPAGE_BPS = (_DECISION_BID - _FILL_PRICE) / _DECISION_BID * 1e4


class _Harness:
    """Writes a real temp journal + points the reader at it; injects the broker."""

    def __init__(self, case: unittest.TestCase, *, lines: list[dict] | None = None):
        self._tmp = TemporaryDirectory()
        case.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.journal_path = self.tmp_dir / "standalone_stops.jsonl"
        rows = _JOURNAL_LINES if lines is None else lines
        with self.journal_path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        self.out_path = self.tmp_dir / "tranche_fills.parquet"
        self.broker = _FakeResolverBroker(_OUTCOMES)

        patches = [
            mock.patch(
                "alphalens_pipeline.brokers.automanager.control_loop._standalone_stop_journal_path",
                lambda: self.journal_path,
            ),
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker",
                return_value=self.broker,
            ),
        ]
        for patch in patches:
            patch.start()
            case.addCleanup(patch.stop)


class TestReconcileFillsCommand(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_broker_without_resolution_capability_fails_cleanly(self):
        # A broker lacking resolve_order_outcome must yield a clean _fail (not a
        # raw AttributeError) and write NO parquet — the guard runs before it.
        with TemporaryDirectory() as tmp:
            journal = Path(tmp) / "standalone_stops.jsonl"
            journal.write_text("", encoding="utf-8")
            out = Path(tmp) / "tranche_fills.parquet"

            class _NoResolveBroker:
                name = "noresolve"

            with (
                mock.patch(
                    "alphalens_pipeline.brokers.automanager.control_loop."
                    "_standalone_stop_journal_path",
                    lambda: journal,
                ),
                mock.patch(
                    "alphalens_pipeline.brokers.registry.get_default_broker",
                    return_value=_NoResolveBroker(),
                ),
            ):
                from alphalens_cli.commands.broker import broker_app

                result = self.runner.invoke(broker_app, ["reconcile-fills", "--out", str(out)])

        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(out.exists(), "no parquet written when the capability is absent")

    def test_writes_parquet_and_prints_counts(self):
        harness = _Harness(self)
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(broker_app, ["reconcile-fills", "--out", str(harness.out_path)])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertTrue(harness.out_path.exists(), "parquet must be written")
        frame = pd.read_parquet(harness.out_path)
        # Three reconciled fires (planned + no-join-key lines skipped).
        self.assertEqual(len(frame), 3)
        self.assertEqual(set(frame["sell_order_id"]), {"S-FILL", "S-PEND", "S-UNK"})
        # Counts in the human summary.
        self.assertIn("3 fire(s)", result.output)
        self.assertIn("1 filled", result.output)
        self.assertIn("1 pending", result.output)
        self.assertIn("1 unresolved", result.output)
        # Mean slippage over the single priced fill.
        self.assertIn("mean slippage", result.output)
        self.assertIn(f"{_EXPECTED_SLIPPAGE_BPS:.2f}", result.output)

    def test_json_output_is_parseable_and_still_writes_parquet(self):
        harness = _Harness(self)
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(
            broker_app, ["reconcile-fills", "--out", str(harness.out_path), "--json"]
        )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertTrue(harness.out_path.exists())
        payload = json.loads(result.output)
        self.assertEqual(len(payload), 3)
        by_id = {row["sell_order_id"]: row for row in payload}
        self.assertEqual(by_id["S-FILL"]["fill_status"], "filled")
        self.assertAlmostEqual(by_id["S-FILL"]["fill_price"], _FILL_PRICE)
        self.assertEqual(by_id["S-PEND"]["fill_status"], "pending")
        self.assertEqual(by_id["S-UNK"]["fill_status"], "unresolved")

    def test_empty_journal_writes_empty_parquet_and_exits_zero(self):
        harness = _Harness(self, lines=[])
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(broker_app, ["reconcile-fills", "--out", str(harness.out_path)])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertTrue(harness.out_path.exists())
        self.assertEqual(len(pd.read_parquet(harness.out_path)), 0)
        self.assertIn("0 fire(s)", result.output)
        self.assertIn("n/a", result.output)

    def test_resolver_was_called_once_per_join_key(self):
        harness = _Harness(self)
        from alphalens_cli.commands.broker import broker_app

        self.runner.invoke(broker_app, ["reconcile-fills", "--out", str(harness.out_path)])

        # Exactly the three join-key-bearing fired lines, nothing else.
        self.assertEqual(sorted(harness.broker.resolved), ["S-FILL", "S-PEND", "S-UNK"])


class TestReconcileFillsIsReadOnly(unittest.TestCase):
    def test_command_touches_no_order_mutating_surface(self):
        """Static guard: the command body never calls a place/cancel/amend
        surface — reconcile-fills is STRICTLY READ-ONLY (resolve + parquet)."""
        import alphalens_cli.commands.broker as broker_module

        module_file = broker_module.__file__
        assert module_file is not None
        source = Path(module_file).read_text(encoding="utf-8")
        tree = ast.parse(source)
        func = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "reconcile_fills_command"
        )
        attr_names = {n.attr for n in ast.walk(func) if isinstance(n, ast.Attribute)}
        forbidden = {
            "place_order",
            "place_market_order",
            "cancel_order",
            "amend_order",
            "submit",
            "place_bracket",
        }
        offenders = sorted(forbidden & attr_names)
        self.assertEqual(offenders, [], "reconcile-fills must never touch a write surface")


if __name__ == "__main__":
    unittest.main()
