"""Unit tests for ``intent_replay/units.py`` — the units vocabulary and the two
shapes that carry a unit (spec sections 5.1 and 5.2).

Both shapes moved here from ``config.py`` when the envelope became their second
consumer. The test that matters is the one ``config.py`` already had: the
dataclasses are frozen and carry ``__slots__``, so a field cannot be set after
construction and a typo cannot create a new one.
"""

from __future__ import annotations

import dataclasses
import unittest

from intent_replay.units import (
    BPS,
    EPOCH_MS_UTC,
    FRACTION,
    INSTRUMENT_CURRENCY,
    PERCENT,
    R_UNIT,
    Quantity,
    Translated,
)


class VocabularyTest(unittest.TestCase):
    """The published unit strings. A rename here is a wire change."""

    def test_every_unit_string_is_the_published_one(self) -> None:
        self.assertEqual(
            (EPOCH_MS_UTC, FRACTION, BPS, PERCENT, R_UNIT, INSTRUMENT_CURRENCY),
            ("epoch_ms_utc", "fraction", "bps", "percent", "R", "instrument_currency"),
        )


class ShapeTest(unittest.TestCase):
    def test_both_shapes_are_frozen_and_carry_slots(self) -> None:
        for cls in (Quantity, Translated):
            with self.subTest(cls.__name__):
                self.assertTrue(dataclasses.fields(cls))
                self.assertTrue(cls.__dataclass_params__.frozen)

    def test_a_quantity_has_no_instance_dictionary(self) -> None:
        quantity = Quantity(1.0, BPS)
        self.assertFalse(hasattr(quantity, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            quantity.value = 2.0  # type: ignore[misc]

    def test_a_translated_has_no_instance_dictionary(self) -> None:
        translated = Translated(
            kind="day1_session_open",
            value=1790170200000,
            unit=EPOCH_MS_UTC,
            source="meta.source + meta.trade_date",
            formula="session_open_utc(2026-09-23)",
        )
        self.assertFalse(hasattr(translated, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            translated.value = 0  # type: ignore[misc]

    def test_a_quantity_renders_value_then_unit(self) -> None:
        self.assertEqual(Quantity(5.0, BPS).to_jsonable(), {"value": 5.0, "unit": "bps"})

    def test_a_translated_renders_the_five_provenance_keys_in_order(self) -> None:
        rendered = Translated(
            kind="order_ttl_sessions",
            value=1791230400000,
            unit=EPOCH_MS_UTC,
            source="spec.order_ttl_days",
            formula="session_close_utc(...)",
        ).to_jsonable()
        self.assertEqual(list(rendered), ["kind", "value", "unit", "source", "formula"])
        self.assertEqual(rendered["value"], 1791230400000)


class GenericValueTest(unittest.TestCase):
    """``Translated`` carries an epoch ``int`` in the config block and a price
    ``float`` in the section 5.1 denominator, so its value type is a parameter.
    """

    def test_the_class_is_subscriptable_in_both_parameters(self) -> None:
        self.assertIsNotNone(Translated[int])
        self.assertIsNotNone(Translated[float])

    def test_a_float_value_survives_rendering_unchanged(self) -> None:
        denominator = Translated(
            kind="placed_stop",
            value=1.63,
            unit=INSTRUMENT_CURRENCY,
            source="spec.disaster_stop",
            formula="avg_entry_price - placed_stop",
        )
        self.assertEqual(denominator.to_jsonable()["value"], 1.63)
        self.assertFalse(hasattr(denominator, "__dict__"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
