"""Every field of the wire contract says what it means (#1405).

A JSON Schema generated from `schema.py` can pin shape, and nothing else. The
units are where a third-party producer actually gets hurt: `alloc_pct` is a
PERCENTAGE while `trail_frac` is a fraction, `order_ttl_days` counts XNYS
sessions rather than calendar days, and `limit_price` means a cap rather than a
level when the tier is immediate. None of that is inferable from a type.

So the meaning lives on the field, in `dataclasses.field(metadata=...)`, and the
generator only reads it. These tests are what stops that from rotting: a field
added without a meaning is red, not merely impolite.

The classes are DISCOVERED, never listed — a registry to maintain is a registry
to forget.
"""

from __future__ import annotations

import dataclasses
import unittest

from broker_contract.trade_intent import schema as contract_schema
from broker_contract.trade_intent.schema import IntentMeta, contract_field

# The one field allowed to carry extra JSON Schema keywords, and why. The codec
# refuses `generation` unless it is an int >= 1 (#1371), so a schema that
# accepted 0 would hand a client a green light the door then refuses. Every
# OTHER numeric bound belongs to `validate_intent` and must NOT be mirrored
# here: two owners for one rule is how the two drift apart.
JSON_SCHEMA_ALLOWLIST = {(IntentMeta, "generation")}


def _contract_dataclasses() -> list[type]:
    return [
        value
        for _, value in vars(contract_schema).items()
        if isinstance(value, type)
        and dataclasses.is_dataclass(value)
        and value.__module__ == contract_schema.__name__
    ]


class TestEveryFieldDeclaresItsMeaning(unittest.TestCase):
    def test_the_discovery_finds_the_contract_classes(self) -> None:
        """Positive control: an empty sweep would satisfy every test below."""
        found = {cls.__name__ for cls in _contract_dataclasses()}
        self.assertIn("TradeIntent", found)
        self.assertIn("EntryTierSpec", found)
        self.assertGreaterEqual(len(found), 10)

    def test_no_field_is_left_without_a_meaning(self) -> None:
        missing = [
            f"{cls.__name__}.{field.name}"
            for cls in _contract_dataclasses()
            for field in dataclasses.fields(cls)
            if not str(field.metadata.get("meaning", "")).strip()
        ]
        self.assertEqual(
            missing,
            [],
            "these contract fields carry no meaning — declare one with "
            "`contract_field(...)`, because a third party cannot infer a unit "
            "from a type",
        )


class TestTheHelperRefusesAnEmptyMeaning(unittest.TestCase):
    """Without this the anti-rot gate above is satisfiable by `""`."""

    def test_an_empty_meaning_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            contract_field("")

    def test_a_blank_meaning_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            contract_field("   \n ")

    def test_a_real_meaning_is_accepted(self) -> None:
        """Positive control: the refusal must not be refusing everything."""
        self.assertEqual(
            contract_field("price, instrument currency").metadata["meaning"],
            "price, instrument currency",
        )


class TestExtraSchemaKeywordsStayRare(unittest.TestCase):
    """`json_schema=` is a wedge: it can express any rule `validate_intent`
    already owns. One field uses it, for a reason written down; a second use
    must be a decision somebody makes on purpose, not a habit."""

    def test_only_the_allowlisted_field_carries_extra_keywords(self) -> None:
        carriers = {
            (cls, field.name)
            for cls in _contract_dataclasses()
            for field in dataclasses.fields(cls)
            if field.metadata.get("json_schema")
        }
        self.assertEqual(
            {(cls.__name__, name) for cls, name in carriers},
            {(cls.__name__, name) for cls, name in JSON_SCHEMA_ALLOWLIST},
            "a new `json_schema=` keyword duplicates a rule validate_intent owns "
            "— add it to JSON_SCHEMA_ALLOWLIST with the reason, or put the rule "
            "where its owner already is",
        )

    def test_the_allowlisted_field_really_carries_the_bound(self) -> None:
        """Positive control: the comparison above passes if BOTH sides are empty."""
        generation = next(f for f in dataclasses.fields(IntentMeta) if f.name == "generation")
        self.assertEqual(generation.metadata["json_schema"], {"minimum": 1})


class TestTheTrapsAreActuallyWrittenDown(unittest.TestCase):
    """The specific confusions this ticket exists to prevent."""

    def _meaning(self, cls: type, name: str) -> str:
        field = next(f for f in dataclasses.fields(cls) if f.name == name)
        return str(field.metadata["meaning"])

    def test_percentage_fields_say_percentage(self) -> None:
        for cls_name, field_name in (
            ("EntryTierSpec", "alloc_pct"),
            ("TpTrancheSpec", "tranche_pct"),
            ("TradeSpec", "suggested_size_pct"),
        ):
            cls = getattr(contract_schema, cls_name)
            with self.subTest(field=f"{cls_name}.{field_name}"):
                self.assertIn("percentage", self._meaning(cls, field_name).lower())

    def test_the_one_fraction_says_fraction(self) -> None:
        meaning = self._meaning(contract_schema.TrailingStop, "trail_frac").lower()
        self.assertIn("fraction", meaning)

    def test_the_ttl_names_trading_days(self) -> None:
        self.assertIn(
            "trading day", self._meaning(contract_schema.TradeSpec, "order_ttl_days").lower()
        )


if __name__ == "__main__":
    unittest.main()
