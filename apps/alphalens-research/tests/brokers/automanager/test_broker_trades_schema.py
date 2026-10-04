"""The published JSON Schema of ``alphalens.broker.trades/v1`` (#1701, memo §3).

The schema is GENERATED from the builder's own vocabulary tuples, so an enum
value cannot be added to the builder and forgotten in the published file
(§3.0: "the published JSON Schema lists the enum values of its release"). The
committed file must equal a fresh generation, the builder's output on the LIVE
fixture must validate in all three modes, and a strict variant (no undeclared
key anywhere) must validate too, so the file describes every field the command
emits rather than a subset.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

import jsonschema
from alphalens_pipeline.brokers.automanager import trades, trades_schema
from alphalens_pipeline.brokers.automanager.trades import TradesFilters, build_trades

from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase
from tests.brokers.automanager.trades_fixture import NOW, FakeFillHistory, install_journals

_REPO = Path(__file__).resolve().parents[5]
PUBLISHED = _REPO / "apps" / "alphalens-broker-contract" / "docs" / "broker-trades-v1.schema.json"


def _envelope(report: trades.TradesReport, env: str = "live") -> dict[str, Any]:
    """What the CLI prints: schema and env first, then the body."""
    return {"schema": trades.TRADES_SCHEMA_ID, "env": env, **report.body()}


class PublishedFileIsTheGeneration(unittest.TestCase):
    def test_the_committed_file_equals_a_fresh_generation(self) -> None:
        self.assertTrue(PUBLISHED.is_file(), f"missing {PUBLISHED}")
        self.assertEqual(
            PUBLISHED.read_text(encoding="utf-8"),
            trades_schema.render(),
            "regenerate: python -m alphalens_pipeline.brokers.automanager.trades_schema "
            f"--write {PUBLISHED}",
        )

    def test_it_is_a_valid_2020_12_schema(self) -> None:
        jsonschema.Draft202012Validator.check_schema(trades_schema.build_schema())
        jsonschema.Draft202012Validator.check_schema(trades_schema.build_schema(strict=True))

    def test_the_published_file_is_open_to_new_optional_fields(self) -> None:
        # §3.0: v1 may grow optional fields; a consumer validating a later v1
        # body with this file must not fail on them.
        self.assertNotIn('"additionalProperties": false', json.dumps(trades_schema.build_schema()))
        self.assertIn(
            '"additionalProperties": false', json.dumps(trades_schema.build_schema(strict=True))
        )


class EnumsAreTheBuildersVocabularies(unittest.TestCase):
    def setUp(self) -> None:
        self.defs = trades_schema.build_schema()["$defs"]

    def test_each_vocabulary(self) -> None:
        cases = {
            "source": (self.defs["Source"]["enum"], trades.SOURCES),
            "null_reason": (self.defs["NullReason"]["enum"], trades.NULL_REASONS),
            "exit reason": (self.defs["ExitReason"]["enum"], trades.EXIT_REASONS),
            "attribution": (self.defs["Attribution"]["enum"], trades.ATTRIBUTIONS),
            "state": (self.defs["State"]["enum"], trades.STATES),
            "state_reason": (self.defs["StateReason"]["enum"], trades.STATE_REASONS),
            "warning": (self.defs["WarningCode"]["enum"], trades.WARNING_CODES),
            "terminal": (self.defs["Terminal"]["enum"], trades.TERMINALS),
            "path": (self.defs["TierPath"]["enum"], trades.TIER_PATHS),
            "source status": (self.defs["SourceStatus"]["enum"], trades.SOURCE_STATUSES),
            "source reason": (self.defs["SourceReason"]["enum"], trades.SOURCE_REASONS),
        }
        for name, (published, builder) in cases.items():
            with self.subTest(vocabulary=name):
                self.assertEqual(list(published), list(builder))

    def test_the_schema_id_is_the_envelope_id(self) -> None:
        schema = trades_schema.build_schema()
        self.assertEqual(schema["properties"]["schema"]["const"], "alphalens.broker.trades/v1")
        self.assertEqual(trades.TRADES_SCHEMA_ID, "alphalens.broker.trades/v1")


class OutputValidates(IsolatedHomeTestCase):
    """Test 31: the builder's real output validates, strict and open."""

    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home)

    def _validate(self, body: dict[str, Any]) -> None:
        for strict in (False, True):
            validator = jsonschema.Draft202012Validator(trades_schema.build_schema(strict=strict))
            errors = sorted(validator.iter_errors(body), key=lambda e: list(e.absolute_path))
            self.assertEqual(
                [f"{list(e.absolute_path)}: {e.message[:200]}" for e in errors[:5]],
                [],
                f"strict={strict}",
            )

    def test_broker_mode(self) -> None:
        report = build_trades(
            "live", broker=FakeFillHistory(), filters=TradesFilters(limit=None), now=NOW
        )
        self._validate(_envelope(report))

    def test_sim_without_reports(self) -> None:
        report = build_trades(
            "live", broker=FakeFillHistory(sim=True), filters=TradesFilters(limit=None), now=NOW
        )
        self._validate(_envelope(report, env="sim"))

    def test_offline(self) -> None:
        report = build_trades("live", broker=None, filters=TradesFilters(limit=None), now=NOW)
        self._validate(_envelope(report))

    def test_the_strict_variant_refuses_an_undeclared_key(self) -> None:
        # A check that cannot fail tests nothing: the strict variant must be
        # able to say no.
        report = build_trades("live", broker=None, filters=TradesFilters(limit=1), now=NOW)
        body = _envelope(report)
        body["trades"][0]["entries"][0]["surprise"] = 1
        validator = jsonschema.Draft202012Validator(trades_schema.build_schema(strict=True))
        self.assertTrue(any(True for _ in validator.iter_errors(body)))

    def test_a_value_with_no_null_reason_is_refused(self) -> None:
        report = build_trades("live", broker=None, filters=TradesFilters(limit=1), now=NOW)
        body = _envelope(report)
        body["trades"][0]["plan_armed_at"]["value"] = None
        body["trades"][0]["plan_armed_at"]["null_reason"] = None
        validator = jsonschema.Draft202012Validator(trades_schema.build_schema())
        self.assertTrue(any(True for _ in validator.iter_errors(body)))


if __name__ == "__main__":
    unittest.main()
