"""The units a published number carries, and the two shapes that carry one
(spec sections 5.1 and 5.2).

ENGINE module: stdlib only.

Both shapes lived in ``config.py`` until the result envelope became their
second consumer: the config block renders a ``Translated`` per translated
document path and a ``Quantity`` per cost, and the summary renders a
``Quantity`` per measure and a ``Translated`` for the R denominator. Moving
them rather than importing them across keeps ONE definition of "a number that
names its unit". The move left no re-export: neither name is in ``config``'s
``__all__`` any more. Python still resolves ``from intent_replay.config import
Quantity``, because ``config`` imports it for its own fields and every
module-level name is importable -- that is a property of the language, not a
second published path, and nothing in this package uses it.

``Translated`` is generic in its value type and all three parameters in use are
real: the config block translates an epoch to an ``int`` (what the walk compares
against ``Bar.t``) and a settlement currency to a ``str`` (section 5.2.1), and
the section 5.1 denominator translates a price difference to a ``float``.
Nothing at runtime enforces it — the parameter exists so a reader and a type
checker see which of the three a given object is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "BPS",
    "EPOCH_MS_UTC",
    "FRACTION",
    "ISO_4217",
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

# The unit of a stated currency CODE, as opposed to the unit of an amount in
# that currency (section 5.2.1). It replaces the symbolic ``instrument_currency``
# token, which named a unit the tool could not spell: ``avg_entry_price`` and the
# R denominator are prices in the INSTRUMENT's currency, and until #1592 no
# stated fact gave that currency a name. ``fx.instrument_currency`` does, so
# those fields carry a real code and the symbolic token is retired.
ISO_4217: Final = "iso_4217"

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
        # Through the key tuple, as ``Translated`` does. A hand-written literal
        # here renders the same keys and is not checked against the published
        # order by anything, because a dict comparison ignores order.
        return {key: getattr(self, key) for key in QUANTITY_KEYS}
