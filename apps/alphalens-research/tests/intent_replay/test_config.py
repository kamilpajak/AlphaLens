"""Unit tests for ``intent_replay/config.py`` — the STATED run configuration
(spec sections 2.1, 4.1, 5.2 and 5.4).

The rule under test is that every value the replay needs is stated by the
caller: a missing key is ``config_incomplete``, a stated value that cannot be
used is ``config_invalid``, and nothing is ever filled in on the caller's
behalf. The evidence for "nothing is filled in" is behavioural — an empty
mapping names every key, removing any leaf names exactly that leaf, and no
dataclass field carries a default — not a claim about the module's source.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import math
import unittest
from collections.abc import Iterator, Mapping
from typing import Any

from intent_replay.config import (
    BPS,
    CONFIG_INCOMPLETE_CODE,
    CONFIG_INCOMPLETE_REASONS,
    CONFIG_INVALID_CODE,
    CONFIG_INVALID_REASONS,
    CONFIG_KEYS,
    EPOCH_MS_UTC,
    FRACTION,
    ConfigError,
    Costs,
    Quantity,
    RunConfig,
    Translated,
)

# The config block of spec section 5.2, verbatim, with the entry deadline as
# the provenance object section 4.1 requires and ``oco`` stated false.
CANONICAL: Mapping[str, Any] = {
    "entry_deadline": {
        "kind": "order_ttl_sessions",
        "value": 1760976000000,
        "unit": "epoch_ms_utc",
        "source": "spec.order_ttl_days",
        "formula": "session_close_utc(advance_trading_sessions(2026-09-24, 7, XNYS))",
    },
    "walk_start": {
        "kind": "day1_session_open",
        "value": 1758547800000,
        "unit": "epoch_ms_utc",
        "source": "meta.source + meta.trade_date",
        "formula": "session_open_utc(2026-09-23); source=manual counts trade_date itself as day 1",
    },
    "entry_trail_bps": 50,
    "ceiling_price": None,
    "time_stop_t": None,
    "oco": False,
    "costs": {
        "commission_rate": {"value": 0.0008, "unit": "fraction"},
        "min_commission": {"value": 1.0, "unit": "USD"},
        "min_commission_applies": True,
        "fx_applies": False,
        "exit_edge_min_bps": {"value": 5.0, "unit": "bps"},
    },
}

TOP_LEVEL_KEYS = (
    "entry_deadline",
    "walk_start",
    "entry_trail_bps",
    "ceiling_price",
    "time_stop_t",
    "oco",
    "costs",
)

OPTIONAL_SCALARS = ("entry_trail_bps", "ceiling_price", "time_stop_t")
EPOCH_PATHS = ("walk_start.value", "entry_deadline.value", "time_stop_t")


def _canonical() -> dict[str, Any]:
    return copy.deepcopy(dict(CANONICAL))


def _all_stated() -> dict[str, Any]:
    """The variant with every optional scalar stated and the trail OFF."""
    data = _canonical()
    data["entry_trail_bps"] = None
    data["ceiling_price"] = 120.5
    data["time_stop_t"] = 1761000000000
    return data


def _paths(node: Any, prefix: str = "") -> Iterator[str]:
    """Every dotted key path of a nested mapping, containers included."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            yield path
            yield from _paths(value, path)


def _without(data: dict[str, Any], path: str) -> dict[str, Any]:
    result = copy.deepcopy(data)
    *parents, leaf = path.split(".")
    node = result
    for part in parents:
        node = node[part]
    del node[leaf]
    return result


def _with(data: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    result = copy.deepcopy(data)
    *parents, leaf = path.split(".")
    node = result
    for part in parents:
        node = node[part]
    node[leaf] = value
    return result


def _refusal(exc: ConfigError) -> tuple[str, str | None]:
    failure = exc.failure
    assert failure.retryable is False, "a refused configuration is never retryable"
    return failure.code, failure.details.get("reason")


class CanonicalExampleTest(unittest.TestCase):
    def test_the_spec_example_parses_to_the_stated_values(self) -> None:
        config = RunConfig.from_jsonable(CANONICAL)
        self.assertEqual(
            config.walk_start,
            Translated(
                kind="day1_session_open",
                value=1758547800000,
                unit="epoch_ms_utc",
                source="meta.source + meta.trade_date",
                formula=(
                    "session_open_utc(2026-09-23); source=manual counts trade_date itself as day 1"
                ),
            ),
        )
        self.assertEqual(config.entry_deadline.value, 1760976000000)
        self.assertEqual(config.entry_trail_bps, 50)
        self.assertIsNone(config.ceiling_price)
        self.assertIsNone(config.time_stop_t)
        self.assertFalse(config.oco)
        self.assertEqual(
            config.costs,
            Costs(
                commission_rate=Quantity(0.0008, "fraction"),
                min_commission=Quantity(1.0, "USD"),
                min_commission_applies=True,
                fx_applies=False,
                exit_edge_min_bps=Quantity(5.0, "bps"),
            ),
        )

    def test_the_all_stated_variant_parses(self) -> None:
        config = RunConfig.from_jsonable(_all_stated())
        self.assertIsNone(config.entry_trail_bps)
        self.assertEqual(config.ceiling_price, 120.5)
        self.assertEqual(config.time_stop_t, 1761000000000)

    def test_to_jsonable_is_the_spec_block(self) -> None:
        # Dict equality first, then the rendered text: dict equality treats
        # 1 == 1.0 and would not see an int rendered where a float was stated.
        rendered = RunConfig.from_jsonable(CANONICAL).to_jsonable()
        self.assertEqual(rendered, dict(CANONICAL))
        self.assertEqual(
            json.dumps(rendered, sort_keys=True, allow_nan=False),
            json.dumps(CANONICAL, sort_keys=True, allow_nan=False),
        )

    def test_the_block_keys_are_the_seven_of_the_spec(self) -> None:
        self.assertEqual(CONFIG_KEYS, TOP_LEVEL_KEYS)
        rendered = RunConfig.from_jsonable(CANONICAL).to_jsonable()
        self.assertEqual(tuple(rendered), TOP_LEVEL_KEYS)
        self.assertEqual(
            tuple(rendered["costs"]),
            (
                "commission_rate",
                "min_commission",
                "min_commission_applies",
                "fx_applies",
                "exit_edge_min_bps",
            ),
        )

    def test_round_trip_is_the_identity(self) -> None:
        for label, data in {"canonical": _canonical(), "all stated": _all_stated()}.items():
            with self.subTest(label):
                config = RunConfig.from_jsonable(data)
                self.assertEqual(RunConfig.from_jsonable(config.to_jsonable()), config)

    def test_the_unit_constants_are_the_published_strings(self) -> None:
        self.assertEqual((EPOCH_MS_UTC, FRACTION, BPS), ("epoch_ms_utc", "fraction", "bps"))

    def test_the_config_is_frozen_and_slotted(self) -> None:
        config = RunConfig.from_jsonable(CANONICAL)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.oco = True  # type: ignore[misc]
        for instance in (config, config.costs, config.walk_start, config.costs.commission_rate):
            with self.subTest(type(instance).__name__):
                self.assertFalse(hasattr(instance, "__dict__"))


class NoDefaultsTest(unittest.TestCase):
    def test_no_field_of_any_config_class_has_a_default(self) -> None:
        for cls in (RunConfig, Costs, Translated, Quantity):
            for field in dataclasses.fields(cls):
                with self.subTest(f"{cls.__name__}.{field.name}"):
                    self.assertIs(field.default, dataclasses.MISSING)
                    self.assertIs(field.default_factory, dataclasses.MISSING)

    def test_an_empty_config_names_all_seven_keys(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            RunConfig.from_jsonable({})
        self.assertEqual(_refusal(ctx.exception), (CONFIG_INCOMPLETE_CODE, "missing_key"))
        self.assertEqual(ctx.exception.failure.details["keys"], sorted(TOP_LEVEL_KEYS))

    def test_removing_any_key_names_exactly_that_key(self) -> None:
        # Containers and leaves alike: 7 top-level, 5 + 5 provenance, 5 costs,
        # 6 quantity leaves. A missing key is ONE fact, so exactly one
        # violation; a value rule must not run on an absent key.
        paths = list(_paths(CANONICAL))
        self.assertEqual(len(paths), 28)
        for path in paths:
            with self.subTest(path):
                with self.assertRaises(ConfigError) as ctx:
                    RunConfig.from_jsonable(_without(_canonical(), path))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INCOMPLETE_CODE, "missing_key"))
                details = ctx.exception.failure.details
                self.assertEqual(details["keys"], [path])
                self.assertEqual(details["violations"], [{"key": path, "reason": "missing_key"}])

    def test_a_missing_key_wins_over_an_invalid_value(self) -> None:
        # The caller completes the block first, then hears about values.
        data = _with(_without(_canonical(), "oco"), "ceiling_price", 0)
        with self.assertRaises(ConfigError) as ctx:
            RunConfig.from_jsonable(data)
        self.assertEqual(_refusal(ctx.exception), (CONFIG_INCOMPLETE_CODE, "missing_key"))
        self.assertEqual(ctx.exception.failure.details["keys"], ["oco"])

    def test_a_misspelt_key_reads_as_the_intended_key_missing(self) -> None:
        # ``entry_trail_bp`` must not silently switch entry trailing off: the
        # caller believes the distance was stated.
        data = _without(_canonical(), "entry_trail_bps")
        data["entry_trail_bp"] = 50
        with self.assertRaises(ConfigError) as ctx:
            RunConfig.from_jsonable(data)
        self.assertEqual(_refusal(ctx.exception), (CONFIG_INCOMPLETE_CODE, "missing_key"))
        self.assertEqual(ctx.exception.failure.details["keys"], ["entry_trail_bps"])


class NullTest(unittest.TestCase):
    def test_null_for_a_non_nullable_key_is_wrong_type(self) -> None:
        # Present-but-null is unreachable by the removal test above; it is a
        # stated value of the wrong type, never "missing" and never accepted.
        for path in (
            "walk_start",
            "entry_deadline",
            "costs",
            "oco",
            "walk_start.value",
            "costs.commission_rate.value",
            "costs.fx_applies",
        ):
            with self.subTest(path):
                with self.assertRaises(ConfigError) as ctx:
                    RunConfig.from_jsonable(_with(_canonical(), path, None))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "wrong_type"))
                self.assertEqual(ctx.exception.failure.details["keys"], [path])

    def test_a_null_entry_deadline_is_refused(self) -> None:
        # Decided 2026-09-25 with the PR 3 plan: the deadline translates
        # ``spec.order_ttl_days``, which every decoded document carries, so a
        # null would translate a present path into nothing with no formula.
        with self.assertRaises(ConfigError) as ctx:
            RunConfig.from_jsonable(_with(_canonical(), "entry_deadline", None))
        self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "wrong_type"))

    def test_the_three_optional_scalars_accept_null(self) -> None:
        for path in OPTIONAL_SCALARS:
            with self.subTest(path):
                config = RunConfig.from_jsonable(_with(_all_stated(), path, None))
                self.assertIsNone(getattr(config, path))


class UnknownKeyTest(unittest.TestCase):
    def test_an_unknown_key_is_refused_at_every_level(self) -> None:
        # The codec drops an unknown key with a warning, which is why the door
        # needs its fixed-point gate; the configuration refuses instead.
        for path in (
            "surprise",
            "walk_start.formulae",
            "costs.commision_rate",
            "costs.commission_rate.units",
        ):
            with self.subTest(path):
                with self.assertRaises(ConfigError) as ctx:
                    RunConfig.from_jsonable(_with(_canonical(), path, 1))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "unknown_key"))
                self.assertEqual(ctx.exception.failure.details["keys"], [path])


class TypeTest(unittest.TestCase):
    def test_a_value_of_the_wrong_type_is_refused(self) -> None:
        cases = {
            "int for bool": ("oco", 0),
            "bool for int": ("entry_trail_bps", True),
            "float for epoch ms": ("walk_start.value", 1.7585478e12),
            "float for time stop": ("time_stop_t", 1.0),
            "string for price": ("ceiling_price", "120"),
            "string for bool": ("costs.fx_applies", "no"),
            "int for kind": ("walk_start.kind", 5),
            "bool for cost": ("costs.commission_rate.value", True),
            "list for costs": ("costs", []),
            "string for quantity": ("costs.min_commission", "1 USD"),
        }
        for label, (path, value) in cases.items():
            with self.subTest(label):
                with self.assertRaises(ConfigError) as ctx:
                    RunConfig.from_jsonable(_with(_all_stated(), path, value))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "wrong_type"))
                self.assertEqual(ctx.exception.failure.details["keys"], [path])

    def test_a_root_that_is_not_an_object_is_refused(self) -> None:
        for value in ([], "config", None):
            with self.subTest(repr(value)):
                with self.assertRaises(ConfigError) as ctx:
                    RunConfig.from_jsonable(value)  # type: ignore[arg-type]
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "wrong_type"))
                self.assertEqual(ctx.exception.failure.details["keys"], ["<root>"])

    def test_an_integer_price_is_accepted(self) -> None:
        config = RunConfig.from_jsonable(_with(_all_stated(), "ceiling_price", 120))
        self.assertEqual(config.ceiling_price, 120)

    def test_a_huge_integer_epoch_is_accepted_without_a_traceback(self) -> None:
        # ``json.loads`` yields an int of any size; ``math.isfinite`` on one
        # wider than a float raises OverflowError, so ints must skip it.
        huge = 10**400
        config = RunConfig.from_jsonable(_with(_canonical(), "walk_start.value", huge))
        self.assertEqual(config.walk_start.value, huge)
        self.assertEqual(RunConfig.from_jsonable(config.to_jsonable()), config)

    def test_a_zero_epoch_is_accepted_as_stated(self) -> None:
        # The spec states no sign rule; a walk_start the bars do not cover is
        # refused downstream by window_too_short, not here.
        for path in EPOCH_PATHS:
            with self.subTest(path):
                RunConfig.from_jsonable(_with(_all_stated(), path, 0))


class NumericRulesTest(unittest.TestCase):
    def test_a_non_finite_number_is_refused(self) -> None:
        for path in (
            "ceiling_price",
            "costs.commission_rate.value",
            "costs.exit_edge_min_bps.value",
        ):
            for value in (math.nan, math.inf, -math.inf):
                with self.subTest(f"{path}={value}"):
                    with self.assertRaises(ConfigError) as ctx:
                        RunConfig.from_jsonable(_with(_all_stated(), path, value))
                    self.assertEqual(
                        _refusal(ctx.exception), (CONFIG_INVALID_CODE, "numeric_not_finite")
                    )
                    self.assertEqual(ctx.exception.failure.details["keys"], [path])

    def test_a_non_positive_trail_distance_is_refused(self) -> None:
        # The live flag reads 0 as OFF; here OFF is stated as null, so a 0 is
        # ambiguous and the message says which form to use.
        for value in (0, -1):
            with self.subTest(value):
                with self.assertRaises(ConfigError) as ctx:
                    RunConfig.from_jsonable(_with(_canonical(), "entry_trail_bps", value))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "not_positive"))
                self.assertIn("null", str(ctx.exception))

    def test_a_non_positive_ceiling_is_refused(self) -> None:
        for value in (0, -1.5):
            with self.subTest(value):
                with self.assertRaises(ConfigError) as ctx:
                    RunConfig.from_jsonable(_with(_canonical(), "ceiling_price", value))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "not_positive"))

    def test_a_negative_cost_is_refused_and_zero_is_not(self) -> None:
        for path in (
            "costs.commission_rate.value",
            "costs.min_commission.value",
            "costs.exit_edge_min_bps.value",
        ):
            with self.subTest(path):
                with self.assertRaises(ConfigError) as ctx:
                    RunConfig.from_jsonable(_with(_canonical(), path, -0.1))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "negative"))
                RunConfig.from_jsonable(_with(_canonical(), path, 0))

    def test_a_unit_other_than_the_published_one_is_refused(self) -> None:
        cases = {
            "walk_start.unit": "epoch_s",
            "entry_deadline.unit": "s",
            "costs.commission_rate.unit": "bps",
            "costs.exit_edge_min_bps.unit": "fraction",
        }
        for path, value in cases.items():
            with self.subTest(path):
                with self.assertRaises(ConfigError) as ctx:
                    RunConfig.from_jsonable(_with(_canonical(), path, value))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "unit_mismatch"))
                self.assertEqual(ctx.exception.failure.details["keys"], [path])

    def test_an_empty_string_is_refused(self) -> None:
        for path in (
            "walk_start.kind",
            "walk_start.source",
            "entry_deadline.formula",
            "costs.min_commission.unit",
        ):
            with self.subTest(path):
                with self.assertRaises(ConfigError) as ctx:
                    RunConfig.from_jsonable(_with(_canonical(), path, ""))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "empty_string"))

    def test_the_min_commission_unit_is_any_currency_code(self) -> None:
        # The block does not see the document, so the code is not matched
        # against anything here (see the module docstring for what it must
        # match, and why that cannot be checked yet).
        config = RunConfig.from_jsonable(_with(_canonical(), "costs.min_commission.unit", "PLN"))
        self.assertEqual(config.costs.min_commission.unit, "PLN")


class OcoTest(unittest.TestCase):
    def test_oco_true_is_refused(self) -> None:
        # Decided 2026-09-25 with the PR 3 plan: v1 models no OCO pair, and the
        # block travels in the result, so an accepted true would describe a
        # policy the run did not apply.
        with self.assertRaises(ConfigError) as ctx:
            RunConfig.from_jsonable(_with(_canonical(), "oco", True))
        self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "oco_unsupported"))
        self.assertEqual(
            ctx.exception.failure.details["violations"],
            [{"key": "oco", "reason": "oco_unsupported", "expected": False}],
        )


class AggregationTest(unittest.TestCase):
    def test_every_invalid_value_is_reported_at_once(self) -> None:
        data = _with(_with(_canonical(), "oco", True), "ceiling_price", 0)
        data["costs"]["commission_rate"]["unit"] = "bps"
        with self.assertRaises(ConfigError) as ctx:
            RunConfig.from_jsonable(data)
        details = ctx.exception.failure.details
        self.assertEqual(details["keys"], ["ceiling_price", "costs.commission_rate.unit", "oco"])
        # ``reason`` is the FIRST violation in block order; the message names it.
        self.assertEqual(details["reason"], "not_positive")
        self.assertIn("ceiling_price", str(ctx.exception))
        self.assertEqual(
            [v["reason"] for v in details["violations"]],
            ["not_positive", "oco_unsupported", "unit_mismatch"],
        )


class ReasonVocabularyTest(unittest.TestCase):
    def test_the_vocabularies_are_closed(self) -> None:
        self.assertEqual(set(CONFIG_INCOMPLETE_REASONS), {"missing_key"})
        self.assertEqual(
            set(CONFIG_INVALID_REASONS),
            {
                "unknown_key",
                "wrong_type",
                "numeric_not_finite",
                "not_positive",
                "negative",
                "unit_mismatch",
                "empty_string",
                "oco_unsupported",
            },
        )


if __name__ == "__main__":
    unittest.main()
