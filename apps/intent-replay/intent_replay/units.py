"""The units a published number carries, and the two shapes that carry one
(spec sections 5.1 and 5.2).

ENGINE module: stdlib only.

Both shapes lived in ``config.py`` until the result envelope became their
second consumer: the config block renders a ``Translated`` per translated
document path and a ``Quantity`` per cost, and the summary renders a
``Quantity`` per measure and a ``Translated`` for the R denominator. Moving
them rather than importing them across keeps ONE definition of "a number that
names its unit"; the move is complete, with no re-export left behind, because
a package with two import paths for one class has two definitions in every
sense that matters to a reader.

``Translated`` is generic in its value type and both parameters are real: the
config block translates an epoch to an ``int`` (what the walk compares against
``Bar.t``), and the section 5.1 denominator translates a price difference to a
``float``. Nothing at runtime enforces it — the parameter exists so a reader
and a type checker see which of the two a given object is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "BPS",
    "EPOCH_MS_UTC",
    "FRACTION",
    "INSTRUMENT_CURRENCY",
    "PERCENT",
    "QUANTITY_KEYS",
    "R_UNIT",
    "TRANSLATED_KEYS",
    "Quantity",
    "Translated",
]

# The published unit strings. Sections 5.2 (the config block) and 5 (the
# summary) print these exact tokens, so a rename here is a wire change.
EPOCH_MS_UTC: Final = "epoch_ms_utc"
FRACTION: Final = "fraction"
BPS: Final = "bps"
PERCENT: Final = "percent"
R_UNIT: Final = "R"

# SYMBOLIC, and deliberately not a currency code: ``avg_entry_price`` and the R
# denominator are prices in the INSTRUMENT's currency, which no document path
# states and which section 4.3.1 puts out of scope. The tool must not resolve
# it, so it names the unit it cannot spell (section 5).
INSTRUMENT_CURRENCY: Final = "instrument_currency"

TRANSLATED_KEYS: Final = ("kind", "value", "unit", "source", "formula")
QUANTITY_KEYS: Final = ("value", "unit")


@dataclass(frozen=True, slots=True)
class Translated[T]:
    """The section 5.1 provenance shape of a TRANSLATED document path.

    ``source`` names the path and ``formula`` shows the arithmetic, so a reader
    can check the value against the numbers beside it. A label they cannot
    check is what section 5.2 rules out.
    """

    kind: str
    value: T
    unit: str
    source: str
    formula: str

    def to_jsonable(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in TRANSLATED_KEYS}


@dataclass(frozen=True, slots=True)
class Quantity:
    """A number that carries its unit (the section 5.2 costs block and every
    scalar measure of the section 5 summary)."""

    value: float
    unit: str

    def to_jsonable(self) -> dict[str, Any]:
        return {"value": self.value, "unit": self.unit}
