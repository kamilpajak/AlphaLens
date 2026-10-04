"""``alphalens broker trades`` on the command line (#1701, memo §2, §6, §7, §9 31-35).

The builder is covered in ``test_broker_trades.py``; this file holds what the
CLI adds: option parsing and its ``usage`` refusals, the env resolution, the
capability check, the failure contract, the published schema, the human
rendering, and the read-only guarantee measured on the files themselves.

The harness is the JSON-contract one (``_BrokerCliCase``): an isolated home and
both gateways faked. The LIVE journals of 2026-10-03 are installed into the
instance the test reads.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest import mock

import jsonschema
from broker_contract.contract import BrokerTransientError

from tests.brokers.automanager.test_broker_json_contract_cli import (
    _BrokerCliCase,
    _ContractFakeBroker,
    _strict_json,
)
from tests.brokers.automanager.test_broker_trades_schema import PUBLISHED
from tests.brokers.automanager.trades_fixture import FakeFillHistory, install_journals

_ALLOW_ORDERS = "ALPHALENS_BROKER_ALLOW_ORDERS"


class _RecordingFillHistory(FakeFillHistory):
    """Remembers the order rail the process had when the venue was read."""

    allow_orders_seen: list[str | None]

    def list_fill_history(self, since: Any, until: Any) -> Any:
        import os

        self.allow_orders_seen.append(os.environ.get(_ALLOW_ORDERS))
        return super().list_fill_history(since, until)


class _TradesCliCase(_BrokerCliCase):
    def setUp(self) -> None:
        super().setUp()
        recording = _RecordingFillHistory()
        recording.allow_orders_seen = []
        self.broker = recording  # type: ignore[assignment]

    def trades(self, *argv: str, env: str = "live"):
        return self.invoke(["trades", "--env", env, *argv])

    def json_trades(self, *argv: str, env: str = "live") -> dict[str, Any]:
        result = self.trades(*argv, "--format", "json", env=env)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(result.stdout.splitlines()), 1, "exactly one JSON line")
        return _strict_json(result.stdout)

    def failure(self, result: Any) -> dict[str, Any]:
        self.assertEqual(result.stdout, "", "a refusal leaves stdout empty")
        return json.loads(result.stderr.strip().splitlines()[-1])


class BrokerMode(_TradesCliCase):
    """Tests 31 and 32: the fake capability, one JSON line, schema-valid."""

    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home, "live")

    def test_one_line_that_validates_against_the_published_schema(self) -> None:
        payload = self.json_trades("--all")
        schema = json.loads(PUBLISHED.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(payload)
        self.assertEqual(payload["schema"], "alphalens.broker.trades/v1")
        self.assertEqual(payload["env"], "live")
        self.assertEqual(payload["mode"], "broker")
        self.assertEqual(payload["counts"]["all"]["total"], 35)
        self.assertEqual(payload["counts"]["all"]["closed"], 13)

    def test_live_reads_run_with_the_order_rail_shut(self) -> None:
        self.json_trades("--all")
        self.assertEqual(self.broker.allow_orders_seen, ["0"])  # type: ignore[attr-defined]

    def test_the_golden_pick_through_the_cli(self) -> None:
        payload = self.json_trades("--pick", "VST:2026-09-21")
        (record,) = payload["trades"]
        self.assertEqual(record["state"], "closed")
        self.assertEqual(record["exits"][0]["reason"], "trailed_stop")
        self.assertAlmostEqual(record["outcome"]["r_multiple"]["value"], 0.3834, places=4)

    def test_filters(self) -> None:
        payload = self.json_trades("--all", "--since", "2026-09-21", "--state", "closed")
        dates = {r["trade_date"] for r in payload["trades"]}
        self.assertTrue(dates and all(d >= "2026-09-21" for d in dates), dates)
        self.assertEqual({r["state"] for r in payload["trades"]}, {"closed"})
        self.assertGreater(
            payload["counts"]["all"]["total"], payload["counts"]["selected"]["total"]
        )
        tickers = {r["ticker"] for r in self.json_trades("--ticker", "lulu")["trades"]}
        self.assertEqual(tickers, {"LULU"})

    def test_the_default_limit_is_200_and_a_smaller_one_is_announced(self) -> None:
        self.assertFalse(self.json_trades()["truncated"])
        result = self.trades("--limit", "2", "--format", "json")
        self.assertEqual(result.exit_code, 0, result.output)
        payload = _strict_json(result.stdout)
        self.assertTrue(payload["truncated"])
        self.assertEqual(len(payload["trades"]), 2)
        self.assertEqual(payload["counts"]["selected"]["total"], 35)
        self.assertIn("showing 2 of 35", result.stderr)

    def test_the_command_writes_nothing(self) -> None:
        before = _tree_digest(self.home / ".alphalens")
        self.json_trades("--all")
        self.assertEqual(_tree_digest(self.home / ".alphalens"), before)


class Offline(_TradesCliCase):
    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home, "live")

    def test_offline_builds_no_broker(self) -> None:
        with mock.patch(
            "alphalens_cli.commands.broker._cli_broker",
            side_effect=AssertionError("offline must not build a broker"),
        ):
            payload = self.json_trades("--offline", "--all")
        self.assertEqual(payload["mode"], "offline")
        self.assertEqual(payload["sources"]["venue.audit"]["status"], "offline")

    def test_offline_needs_no_composed_unit(self) -> None:
        # `status --offline` precedent: composition is best-effort offline.
        with mock.patch(
            "alphalens_pipeline.brokers.automanager.unit_env.compose_live_environment",
            side_effect=__import__(
                "alphalens_pipeline.brokers.automanager.unit_env", fromlist=["UnitEnvError"]
            ).UnitEnvError("no user manager"),
        ):
            result = self.trades("--offline", "--format", "json")
        self.assertEqual(result.exit_code, 0, result.output)


class Refusals(_TradesCliCase):
    """Tests 33 and 34, and the §6 table."""

    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home, "live")

    def test_usage(self) -> None:
        cases = {
            "bad since": ["--since", "2026-13-01"],
            "bad state": ["--state", "won"],
            "limit zero": ["--limit", "0"],
            "limit and all": ["--limit", "5", "--all"],
            "bad pick": ["--pick", "not a key"],
            "bad format": [],
        }
        for name, argv in cases.items():
            with self.subTest(case=name):
                fmt = ["--format", "yaml"] if name == "bad format" else ["--format", "json"]
                result = self.trades(*argv, *fmt)
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertEqual(result.stdout, "")
                if name != "bad format":  # an unknown format is refused in prose
                    self.assertEqual(self.failure(result)["code"], "usage")

    def test_a_broker_without_the_capability(self) -> None:
        self.broker = _ContractFakeBroker()  # type: ignore[assignment]
        result = self.trades("--format", "json")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(self.failure(result)["code"], "broker_unsupported")

    def test_a_transient_broker_error_is_retryable(self) -> None:
        with mock.patch.object(
            FakeFillHistory, "list_fill_history", side_effect=BrokerTransientError("gateway down")
        ):
            result = self.trades("--format", "json")
        self.assertEqual(result.exit_code, 7, result.output)
        failure = self.failure(result)
        self.assertEqual(failure["code"], "broker_transient")
        self.assertTrue(failure["retryable"])

    def test_legacy_flat_state(self) -> None:
        (self.home / ".alphalens" / "broker_orders" / "submissions.jsonl").write_text(
            "", encoding="utf-8"
        )
        result = self.trades("--offline", "--format", "json")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(self.failure(result)["code"], "state_layout")


class Human(_TradesCliCase):
    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home, "live")

    def test_one_block_per_pick_with_entries_and_exits(self) -> None:
        result = self.trades("--pick", "VST:2026-09-21")
        self.assertEqual(result.exit_code, 0, result.output)
        out = result.stdout
        self.assertIn("VST:2026-09-21", out)
        self.assertIn("closed", out)
        self.assertIn("trailed_stop", out)
        self.assertIn("135.11", out)
        self.assertIn("138.66", out)
        self.assertIn("0.3834", out)

    def test_warnings_go_to_stderr(self) -> None:
        result = self.trades("--pick", "UBER:2026-09-08")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("exit_qty_exceeds_pick", result.stderr)
        self.assertNotIn("exit_qty_exceeds_pick", result.stdout)

    def test_unattributed_fills_are_listed(self) -> None:
        result = self.trades("--pick", "UBER:2026-09-08")
        self.assertIn("manual_open", result.stdout)


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
