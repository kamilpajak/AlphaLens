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
    CONFIG_INCOMPLETE_CODE,
    CONFIG_INCOMPLETE_REASONS,
    CONFIG_INVALID_CODE,
    CONFIG_INVALID_REASONS,
    CONFIG_KEYS,
    ConfigError,
    Costs,
    RunConfig,
)
from intent_replay.fx import Fx
from intent_replay.units import BPS, EPOCH_MS_UTC, FRACTION, Quantity, Translated

# The config block of spec section 5.2, with the entry deadline as the
# provenance object section 4.1 requires and ``oco`` stated false.
#
# It departs from the printed block in EXACTLY ONE key, and section 5.4 asks
# for that to be said rather than for the fixture to keep calling itself
# verbatim: the block states ``entry_trail_bps: 50`` and the fixture states
# null. The refusal that forced the departure is gone and the walk models the
# trail, so the reason has CHANGED rather than expired: this block is the
# baseline of the limit-ladder tests, and there are dozens of them. A trailing
# test states the distance itself, which also keeps the two entry policies
# visibly apart in every test that exercises one.
CANONICAL: Mapping[str, Any] = {
    "entry_deadline": {
        "kind": "order_ttl_sessions",
        "value": 1791230400000,
        "unit": "epoch_ms_utc",
        "source": "spec.order_ttl_days",
        "formula": "session_close_utc(advance_trading_sessions(2026-09-24, 7, XNYS))",
    },
    "walk_start": {
        "kind": "day1_session_open",
        "value": 1790170200000,
        "unit": "epoch_ms_utc",
        "source": "meta.source + meta.trade_date",
        "formula": "session_open_utc(2026-09-23); source=manual counts trade_date itself as day 1",
    },
    "entry_trail_bps": None,
    "ceiling_price": None,
    "time_stop_t": None,
    "oco": False,
    "fx": {
        "instrument_currency": {
            "kind": "venue_settlement_currency",
            "value": "USD",
            "unit": "iso_4217",
            "source": "instrument.mic",
            "formula": "XNYS settles in USD",
        },
        "mid_rate": {
            "kind": "fx_mid",
            "value": 1.08,
            "unit": "USD_per_EUR",
            "source": "ecb_reference_rate",
            "formula": "ECB euro foreign exchange reference rate, 2026-09-23 14:15 UTC",
        },
        "round_trip_cost_rate": {"value": 0.005, "unit": "fraction"},
        "sizing_buffer_pct": {"value": 1.0, "unit": "percent"},
    },
    "costs": {
        "commission_rate": {"value": 0.0008, "unit": "fraction"},
        "min_commission": {"value": 1.0, "unit": "USD"},
        "min_commission_applies": True,
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
    "fx",
    "costs",
)

OPTIONAL_SCALARS = ("entry_trail_bps", "ceiling_price", "time_stop_t")
EPOCH_PATHS = ("walk_start.value", "entry_deadline.value", "time_stop_t")


def _canonical() -> dict[str, Any]:
    return copy.deepcopy(dict(CANONICAL))


def _all_stated() -> dict[str, Any]:
    """The variant with every optional scalar stated — the trail included.

    It was misnamed while a stated distance was refused: it left the trail null
    and still called itself all-stated, so the one optional scalar with a
    refusal of its own was the one it never exercised."""
    data = _canonical()
    data["entry_trail_bps"] = 50
    data["ceiling_price"] = 120.5
    data["time_stop_t"] = 1790900000000
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


# The published templates all state a EUR budget on a KO/XNYS instrument, so
# the canonical block is the CROSS-currency case and the account code is EUR.
# It is a required keyword of ``from_jsonable`` rather than a block key: it is
# the document's ``spec.size.currency``, and a block able to state it would be
# able to disagree with the document it replays.
ACCOUNT_CURRENCY = "EUR"
INSTRUMENT_CURRENCY = "USD"


def _parse(data: Any, *, account_currency: str = ACCOUNT_CURRENCY) -> RunConfig:
    return RunConfig.from_jsonable(data, account_currency=account_currency)


def _refusal(exc: ConfigError) -> tuple[str, str | None]:
    failure = exc.failure
    assert failure.retryable is False, "a refused configuration is never retryable"
    return failure.code, failure.details.get("reason")


class CanonicalExampleTest(unittest.TestCase):
    def test_the_spec_example_parses_to_the_stated_values(self) -> None:
        config = _parse(CANONICAL)
        self.assertEqual(
            config.walk_start,
            Translated(
                kind="day1_session_open",
                value=1790170200000,
                unit="epoch_ms_utc",
                source="meta.source + meta.trade_date",
                formula=(
                    "session_open_utc(2026-09-23); source=manual counts trade_date itself as day 1"
                ),
            ),
        )
        self.assertEqual(config.entry_deadline.value, 1791230400000)
        self.assertIsNone(config.entry_trail_bps)
        self.assertIsNone(config.ceiling_price)
        self.assertIsNone(config.time_stop_t)
        self.assertFalse(config.oco)
        self.assertEqual(
            config.costs,
            Costs(
                commission_rate=Quantity(0.0008, "fraction"),
                min_commission=Quantity(1.0, "USD"),
                min_commission_applies=True,
                exit_edge_min_bps=Quantity(5.0, "bps"),
                fx=Fx(
                    account_currency="EUR",
                    instrument_currency="USD",
                    rate=1.08,
                    round_trip_cost_rate=0.005,
                    sizing_buffer_pct=1.0,
                ),
            ),
        )

    def test_the_canonical_epochs_equal_their_formulas(self) -> None:
        """The two translated values are stated beside the rule that produced
        them, and a reader checks the number against the rule (spec section 5.2).
        The check is done here once, without a calendar: the formulas name
        2026-09-23 (a Wednesday, XNYS open 13:30 UTC) and the seventh session
        after 2026-09-24, which is 2026-10-05 (close 20:00 UTC)."""
        import datetime as dt

        config = _parse(_canonical())
        as_utc = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC)  # noqa: E731
        self.assertEqual(
            as_utc(config.walk_start.value), dt.datetime(2026, 9, 23, 13, 30, tzinfo=dt.UTC)
        )
        self.assertEqual(
            as_utc(config.entry_deadline.value), dt.datetime(2026, 10, 5, 20, 0, tzinfo=dt.UTC)
        )

    def test_the_all_stated_variant_parses(self) -> None:
        config = _parse(_all_stated())
        self.assertEqual(config.entry_trail_bps, 50)
        self.assertEqual(config.ceiling_price, 120.5)
        self.assertEqual(config.time_stop_t, 1790900000000)

    def test_to_jsonable_is_the_spec_block(self) -> None:
        # Dict equality first, then the rendered text: dict equality treats
        # 1 == 1.0 and would not see an int rendered where a float was stated.
        rendered = _parse(CANONICAL).to_jsonable()
        self.assertEqual(rendered, dict(CANONICAL))
        self.assertEqual(
            json.dumps(rendered, sort_keys=True, allow_nan=False),
            json.dumps(CANONICAL, sort_keys=True, allow_nan=False),
        )

    def test_the_block_keys_are_the_eight_of_the_spec(self) -> None:
        # ``fx`` sits before ``costs``: it names the currency the minimum
        # commission is quoted in, so the conversion is the frame the costs are
        # read in rather than a cost of its own (section 5.2.1).
        self.assertEqual(CONFIG_KEYS, TOP_LEVEL_KEYS)
        rendered = _parse(CANONICAL).to_jsonable()
        self.assertEqual(tuple(rendered), TOP_LEVEL_KEYS)
        self.assertEqual(
            tuple(rendered["costs"]),
            (
                "commission_rate",
                "min_commission",
                "min_commission_applies",
                "exit_edge_min_bps",
            ),
        )

    def test_round_trip_is_the_identity(self) -> None:
        for label, data in {"canonical": _canonical(), "all stated": _all_stated()}.items():
            with self.subTest(label):
                config = _parse(data)
                self.assertEqual(_parse(config.to_jsonable()), config)

    def test_the_unit_constants_are_the_published_strings(self) -> None:
        self.assertEqual((EPOCH_MS_UTC, FRACTION, BPS), ("epoch_ms_utc", "fraction", "bps"))

    def test_the_config_is_frozen_and_slotted(self) -> None:
        config = _parse(CANONICAL)
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

    def test_an_empty_config_names_all_eight_keys(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _parse({})
        self.assertEqual(_refusal(ctx.exception), (CONFIG_INCOMPLETE_CODE, "missing_key"))
        self.assertEqual(ctx.exception.failure.details["keys"], sorted(TOP_LEVEL_KEYS))

    def test_removing_any_key_names_exactly_that_key(self) -> None:
        # Containers and leaves alike: 8 top-level, 5 + 5 for the two epoch
        # provenance objects, 18 for the fx block (four keys, two of them
        # provenance objects and two value/unit pairs) and 10 for costs. A
        # missing key is ONE fact, so exactly one violation; a value rule must
        # not run on an absent key.
        paths = list(_paths(CANONICAL))
        self.assertEqual(len(paths), 46)
        for path in paths:
            with self.subTest(path):
                with self.assertRaises(ConfigError) as ctx:
                    _parse(_without(_canonical(), path))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INCOMPLETE_CODE, "missing_key"))
                details = ctx.exception.failure.details
                self.assertEqual(details["keys"], [path])
                self.assertEqual(details["violations"], [{"key": path, "reason": "missing_key"}])

    def test_a_missing_key_wins_over_an_invalid_value(self) -> None:
        # The caller completes the block first, then hears about values.
        data = _with(_without(_canonical(), "oco"), "ceiling_price", 0)
        with self.assertRaises(ConfigError) as ctx:
            _parse(data)
        self.assertEqual(_refusal(ctx.exception), (CONFIG_INCOMPLETE_CODE, "missing_key"))
        self.assertEqual(ctx.exception.failure.details["keys"], ["oco"])

    def test_a_misspelt_key_reads_as_the_intended_key_missing(self) -> None:
        # ``entry_trail_bp`` must not silently switch entry trailing off: the
        # caller believes the distance was stated.
        data = _without(_canonical(), "entry_trail_bps")
        data["entry_trail_bp"] = 50
        with self.assertRaises(ConfigError) as ctx:
            _parse(data)
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
            "fx.sizing_buffer_pct",
        ):
            with self.subTest(path):
                with self.assertRaises(ConfigError) as ctx:
                    _parse(_with(_canonical(), path, None))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "wrong_type"))
                self.assertEqual(ctx.exception.failure.details["keys"], [path])

    def test_a_null_entry_deadline_is_refused(self) -> None:
        # Decided 2026-09-25 with the PR 3 plan: the deadline translates
        # ``spec.order_ttl_days``, which every decoded document carries, so a
        # null would translate a present path into nothing with no formula.
        with self.assertRaises(ConfigError) as ctx:
            _parse(_with(_canonical(), "entry_deadline", None))
        self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "wrong_type"))

    def test_the_three_optional_scalars_accept_null(self) -> None:
        for path in OPTIONAL_SCALARS:
            with self.subTest(path):
                config = _parse(_with(_all_stated(), path, None))
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
                    _parse(_with(_canonical(), path, 1))
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
            "string for bool": ("costs.min_commission_applies", "no"),
            "int for kind": ("walk_start.kind", 5),
            "bool for cost": ("costs.commission_rate.value", True),
            "list for costs": ("costs", []),
            "string for quantity": ("costs.min_commission", "1 USD"),
        }
        for label, (path, value) in cases.items():
            with self.subTest(label):
                with self.assertRaises(ConfigError) as ctx:
                    _parse(_with(_all_stated(), path, value))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "wrong_type"))
                self.assertEqual(ctx.exception.failure.details["keys"], [path])

    def test_a_root_that_is_not_an_object_is_refused(self) -> None:
        for value in ([], "config", None):
            with self.subTest(repr(value)):
                with self.assertRaises(ConfigError) as ctx:
                    _parse(value)  # type: ignore[arg-type]
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "wrong_type"))
                self.assertEqual(ctx.exception.failure.details["keys"], ["<root>"])

    def test_an_integer_price_is_accepted(self) -> None:
        config = _parse(_with(_all_stated(), "ceiling_price", 120))
        self.assertEqual(config.ceiling_price, 120)

    def test_a_huge_integer_epoch_is_accepted_without_a_traceback(self) -> None:
        # ``json.loads`` yields an int of any size; ``math.isfinite`` on one
        # wider than a float raises OverflowError, so ints must skip it.
        huge = 10**400
        config = _parse(_with(_canonical(), "walk_start.value", huge))
        self.assertEqual(config.walk_start.value, huge)
        self.assertEqual(_parse(config.to_jsonable()), config)

    def test_a_zero_epoch_is_accepted_as_stated(self) -> None:
        # The spec states no sign rule; a walk_start the bars do not cover is
        # refused downstream by window_too_short, not here.
        for path in EPOCH_PATHS:
            with self.subTest(path):
                _parse(_with(_all_stated(), path, 0))


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
                        _parse(_with(_all_stated(), path, value))
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
                    _parse(_with(_canonical(), "entry_trail_bps", value))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "not_positive"))
                self.assertIn("null", str(ctx.exception))

    def test_a_non_positive_ceiling_is_refused(self) -> None:
        for value in (0, -1.5):
            with self.subTest(value):
                with self.assertRaises(ConfigError) as ctx:
                    _parse(_with(_canonical(), "ceiling_price", value))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "not_positive"))

    def test_a_negative_cost_is_refused_and_zero_is_not(self) -> None:
        for path in (
            "costs.commission_rate.value",
            "costs.min_commission.value",
            "costs.exit_edge_min_bps.value",
        ):
            with self.subTest(path):
                with self.assertRaises(ConfigError) as ctx:
                    _parse(_with(_canonical(), path, -0.1))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "negative"))
                _parse(_with(_canonical(), path, 0))

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
                    _parse(_with(_canonical(), path, value))
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
                    _parse(_with(_canonical(), path, ""))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "empty_string"))


class OcoTest(unittest.TestCase):
    def test_oco_true_is_refused(self) -> None:
        # Decided 2026-09-25 with the PR 3 plan: v1 models no OCO pair, and the
        # block travels in the result, so an accepted true would describe a
        # policy the run did not apply.
        with self.assertRaises(ConfigError) as ctx:
            _parse(_with(_canonical(), "oco", True))
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
            _parse(data)
        details = ctx.exception.failure.details
        self.assertEqual(details["keys"], ["ceiling_price", "costs.commission_rate.unit", "oco"])
        # ``reason`` is the FIRST violation in block order; the message names it.
        self.assertEqual(details["reason"], "not_positive")
        self.assertIn("ceiling_price", str(ctx.exception))
        self.assertEqual(
            [v["reason"] for v in details["violations"]],
            ["not_positive", "oco_unsupported", "unit_mismatch"],
        )


class EntryTrailDistanceTest(unittest.TestCase):
    """A stated distance is a POLICY the walk applies, so the block carries it
    through. The temporary refusal that stood here answered a well-formed
    distance with ``entry_trail_not_modelled``; the walk now models the trail,
    so refusing would describe an entry ladder the run did not replay.

    The type and range checks are unchanged and still run first, which is what
    keeps ``true`` a ``wrong_type`` and ``0`` a ``not_positive`` rather than
    letting either reach the walk as a distance."""

    def test_a_well_formed_distance_is_carried_through(self) -> None:
        for value in (1, 50, 10_000):
            with self.subTest(value):
                config = _parse(_with(_canonical(), "entry_trail_bps", value))
                self.assertEqual(config.entry_trail_bps, value)

    def test_the_distance_survives_the_round_trip_the_envelope_echoes(self) -> None:
        # The block travels in the result (section 5.2), so a value the parser
        # accepts and the echo drops would publish a run nobody can reproduce.
        config = _parse(_with(_canonical(), "entry_trail_bps", 50))
        self.assertEqual(config.to_jsonable()["entry_trail_bps"], 50)

    def test_the_trail_stated_off_is_still_accepted(self) -> None:
        config = _parse(_with(_canonical(), "entry_trail_bps", None))
        self.assertIsNone(config.entry_trail_bps)

    def test_a_distance_too_wide_for_the_arithmetic_is_refused_not_a_traceback(self) -> None:
        # There is deliberately no UPPER bound, but "any integer" is only
        # publishable if the walk can carry every integer, and it cannot: the
        # distance is multiplied by a price, and an int wider than a float
        # raises OverflowError on the conversion. Before this the refusal was
        # unreachable because every stated distance was refused, so the value
        # never reached the arithmetic.
        for value in (10**400, 2**2000):
            with self.subTest(len(str(value))):
                with self.assertRaises(ConfigError) as ctx:
                    _parse(_with(_canonical(), "entry_trail_bps", value))
                self.assertEqual(
                    _refusal(ctx.exception), (CONFIG_INVALID_CODE, "numeric_not_finite")
                )

    def test_the_type_and_range_checks_still_run_first(self) -> None:
        # Section 5.4: ``true`` stays wrong_type and ``0`` stays not_positive.
        # The live flag reads 0 as OFF, so a 0 here is ambiguous and says which
        # form to use instead; that message outlives the removed refusal.
        for value, reason in ((True, "wrong_type"), (0, "not_positive"), (-1, "not_positive")):
            with self.subTest(repr(value)):
                with self.assertRaises(ConfigError) as ctx:
                    _parse(_with(_canonical(), "entry_trail_bps", value))
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, reason))


# --- #1592: the stated FX facts of section 5.2.1 -------------------------------

# The cross-currency facts CANONICAL states: a EUR budget on a USD instrument,
# which is the section 5 example's own document. ``mid_rate`` carries the
# section 5.1 provenance shape because it is the one value in the block nothing
# in the run can check, and its UNIT spells the direction, which IS checkable
# (section 5.2.1).
FX_CROSS: Mapping[str, Any] = CANONICAL["fx"]

# The same-currency block: ONE key, and the other three are refused rather than
# accepted and left inert.
FX_SAME: Mapping[str, Any] = {
    "instrument_currency": {
        "kind": "venue_settlement_currency",
        "value": "EUR",
        "unit": "iso_4217",
        "source": "instrument.mic",
        "formula": "the venue settles in the account's own currency",
    }
}

FX_CROSS_KEYS = (
    "instrument_currency",
    "mid_rate",
    "round_trip_cost_rate",
    "sizing_buffer_pct",
)


def _same_currency_block() -> dict[str, Any]:
    data = copy.deepcopy(dict(CANONICAL))
    data["fx"] = copy.deepcopy(dict(FX_SAME))
    data["costs"]["min_commission"]["unit"] = ACCOUNT_CURRENCY
    return data


# The same-currency variant: the PREIMAGE every pre-#1592 number is from. Both
# arms of ``Fx`` return their argument on this block, so no float operation
# runs, and a run against it reproduces every number the tool published before
# this issue -- exactly, not to within an ulp. ``test_walk`` is built on it for
# that reason, and the cross-currency rows there state the facts themselves.
SAME_CURRENCY: Mapping[str, Any] = _same_currency_block()


def _cross() -> dict[str, Any]:
    """The canonical block, which already states the cross-currency facts."""
    return _canonical()


def _same() -> dict[str, Any]:
    return copy.deepcopy(dict(SAME_CURRENCY))


class FxKeySetTest(unittest.TestCase):
    """The conditional key set: one key when the codes agree, four when they differ."""

    def test_the_cross_currency_block_parses_to_the_stated_facts(self) -> None:
        config = _parse(_cross(), account_currency=ACCOUNT_CURRENCY)
        self.assertEqual(config.fx.instrument_currency.value, "USD")
        self.assertEqual(config.fx.mid_rate.value, 1.08)
        self.assertEqual(config.fx.round_trip_cost_rate, Quantity(0.005, "fraction"))
        self.assertEqual(config.fx.sizing_buffer_pct, Quantity(1.0, "percent"))

    def test_applies_is_derived_and_never_stated(self) -> None:
        cross = _parse(_cross(), account_currency=ACCOUNT_CURRENCY)
        self.assertTrue(cross.costs.fx.applies)
        same = _parse(_same(), account_currency=ACCOUNT_CURRENCY)
        self.assertFalse(same.costs.fx.applies)

    def test_a_magnitude_stated_on_a_same_currency_run_is_an_unknown_key(self) -> None:
        for key in ("mid_rate", "round_trip_cost_rate", "sizing_buffer_pct"):
            with self.subTest(key):
                data = _same()
                data["fx"][key] = copy.deepcopy(FX_CROSS[key])
                with self.assertRaises(ConfigError) as ctx:
                    RunConfig.from_jsonable(data, account_currency=ACCOUNT_CURRENCY)
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "unknown_key"))
                self.assertEqual(ctx.exception.failure.details["keys"], [f"fx.{key}"])

    def test_a_magnitude_absent_on_a_cross_currency_run_is_a_missing_key(self) -> None:
        for key in ("mid_rate", "round_trip_cost_rate", "sizing_buffer_pct"):
            with self.subTest(key):
                with self.assertRaises(ConfigError) as ctx:
                    _parse(_without(_cross(), f"fx.{key}"), account_currency=ACCOUNT_CURRENCY)
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INCOMPLETE_CODE, "missing_key"))
                self.assertEqual(ctx.exception.failure.details["keys"], [f"fx.{key}"])


class FxDirectionTest(unittest.TestCase):
    """The unit is the only real guard against an inverted rate (section 8.1)."""

    def test_the_pair_unit_must_spell_instrument_per_account(self) -> None:
        for unit in ("EUR_per_USD", "USD/EUR", "USD_per_PLN", "fraction"):
            with self.subTest(unit):
                with self.assertRaises(ConfigError) as ctx:
                    _parse(
                        _with(_cross(), "fx.mid_rate.unit", unit),
                        account_currency=ACCOUNT_CURRENCY,
                    )
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "unit_mismatch"))
                self.assertEqual(
                    ctx.exception.failure.details["violations"][0]["expected"], "USD_per_EUR"
                )

    def test_a_non_positive_rate_is_refused(self) -> None:
        for value in (0.0, -1.08):
            with self.subTest(value):
                with self.assertRaises(ConfigError) as ctx:
                    _parse(
                        _with(_cross(), "fx.mid_rate.value", value),
                        account_currency=ACCOUNT_CURRENCY,
                    )
                self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "not_positive"))

    def test_the_instrument_code_must_be_a_three_letter_uppercase_code(self) -> None:
        # The door refuses a lower-case ``spec.size.currency``
        # (``validate_intent``), so the two codes ``applies`` compares are only
        # comparable if this side is held to the same shape.
        for value in ("usd", "US", "USDD", "US1"):
            with self.subTest(value):
                with self.assertRaises(ConfigError) as ctx:
                    _parse(
                        _with(_cross(), "fx.instrument_currency.value", value),
                        account_currency=ACCOUNT_CURRENCY,
                    )
                self.assertEqual(
                    _refusal(ctx.exception), (CONFIG_INVALID_CODE, "not_a_currency_code")
                )

    def test_the_instrument_currency_unit_is_the_published_token(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _parse(
                _with(_cross(), "fx.instrument_currency.unit", "currency"),
                account_currency=ACCOUNT_CURRENCY,
            )
        self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "unit_mismatch"))


class FxBufferBoundTest(unittest.TestCase):
    """``[0, 100)``, restated here because the contract has no such bound.

    Measured on the published fixture: at 100 the budget is 0.0, the rung's
    units are 0.0 and ``walk.py`` divides by them; at 150 the budget is -750.0
    and the run reports a booked profit of +450.0 on a short the document never
    declared, labelled ``no_fill``. Both are a traceback or a plausible lie
    where the tool owes a refusal.
    """

    def test_the_open_upper_bound_is_refused(self) -> None:
        for value in (100.0, 150.0, 1e9):
            with self.subTest(value):
                with self.assertRaises(ConfigError) as ctx:
                    _parse(
                        _with(_cross(), "fx.sizing_buffer_pct.value", value),
                        account_currency=ACCOUNT_CURRENCY,
                    )
                self.assertEqual(
                    _refusal(ctx.exception), (CONFIG_INVALID_CODE, "buffer_out_of_range")
                )

    def test_a_negative_buffer_is_refused_as_a_negative_cost(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _parse(
                _with(_cross(), "fx.sizing_buffer_pct.value", -1.0),
                account_currency=ACCOUNT_CURRENCY,
            )
        self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "negative"))

    def test_zero_is_a_stated_policy_and_is_accepted(self) -> None:
        config = _parse(
            _with(_cross(), "fx.sizing_buffer_pct.value", 0.0), account_currency=ACCOUNT_CURRENCY
        )
        self.assertEqual(config.costs.fx.sizing_notional(1500.0), 1500.0)


class FxChecksTheMinimumCommissionCurrencyTest(unittest.TestCase):
    """What the block could not check before #1592 and now can (section 5.2.1).

    The gate compares ``min_commission`` against a notional, so a minimum
    quoted in the ACCOUNT's currency on a cross-currency run prices the round
    trip in two currencies at once.
    """

    def test_a_minimum_in_the_wrong_currency_is_refused(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _parse(
                _with(_cross(), "costs.min_commission.unit", "EUR"),
                account_currency=ACCOUNT_CURRENCY,
            )
        self.assertEqual(_refusal(ctx.exception), (CONFIG_INVALID_CODE, "unit_mismatch"))
        self.assertEqual(ctx.exception.failure.details["violations"][0]["expected"], "USD")

    def test_the_instrument_currency_is_accepted(self) -> None:
        config = _parse(_cross(), account_currency=ACCOUNT_CURRENCY)
        self.assertEqual(config.costs.min_commission.unit, "USD")


class FxEchoTest(unittest.TestCase):
    def test_the_stated_block_round_trips_in_both_directions(self) -> None:
        for label, data in {"cross": _cross(), "same": _same()}.items():
            with self.subTest(label):
                config = RunConfig.from_jsonable(data, account_currency=ACCOUNT_CURRENCY)
                self.assertEqual(
                    RunConfig.from_jsonable(
                        config.to_jsonable(), account_currency=ACCOUNT_CURRENCY
                    ),
                    config,
                )

    def test_the_echo_renders_only_the_keys_the_caller_stated(self) -> None:
        cross = _parse(_cross(), account_currency=ACCOUNT_CURRENCY).to_jsonable()
        self.assertEqual(tuple(cross["fx"]), FX_CROSS_KEYS)
        same = _parse(_same(), account_currency=ACCOUNT_CURRENCY).to_jsonable()
        self.assertEqual(tuple(same["fx"]), ("instrument_currency",))

    def test_the_echoed_provenance_and_the_engine_object_cannot_disagree(self) -> None:
        # Two representations of one fact: the provenance travels in the result
        # and the ``Fx`` does the arithmetic. Both are built once, from the same
        # parsed values, which is what this asserts.
        config = _parse(_cross(), account_currency=ACCOUNT_CURRENCY)
        fx = config.costs.fx
        self.assertEqual(fx.account_currency, ACCOUNT_CURRENCY)
        self.assertEqual(fx.instrument_currency, config.fx.instrument_currency.value)
        self.assertEqual(fx.rate, config.fx.mid_rate.value)
        self.assertEqual(fx.round_trip_cost_rate, config.fx.round_trip_cost_rate.value)
        self.assertEqual(fx.sizing_buffer_pct, config.fx.sizing_buffer_pct.value)
        self.assertEqual(fx.pair_unit(), config.fx.mid_rate.unit)


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
                "not_a_currency_code",
                "buffer_out_of_range",
            },
        )


if __name__ == "__main__":
    unittest.main()
