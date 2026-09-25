"""Property: the stated run configuration round-trips through its own wire
form (``intent_replay.config``, spec section 5.2).

``RunConfig.to_jsonable`` is the block PR 7 puts in the result envelope, and
``from_jsonable`` is the only way a block comes in; if the two drift, a run
stops being reconstructible from its own output, which section 5.2 exists to
guarantee. The strategy draws only configurations ``from_jsonable`` accepts —
unavoidable in either direction, since the refused ones have no dataclass to
compare — and varies every field, including the free currency code and each
optional scalar's null/value arm.
"""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st
from intent_replay.config import BPS, EPOCH_MS_UTC, FRACTION, RunConfig

from .base import PropertyTestCase

_TEXT = st.text(min_size=1, max_size=40)
_EPOCH = st.integers(min_value=0, max_value=4_102_444_800_000)
_COST = st.one_of(st.integers(min_value=0, max_value=10_000), st.floats(0.0, 1.0e6))
_CURRENCY = st.sampled_from(["USD", "EUR", "PLN", "GBP", "CHF"])


def _translated(unit: str) -> st.SearchStrategy[dict[str, object]]:
    return st.fixed_dictionaries(
        {"kind": _TEXT, "value": _EPOCH, "unit": st.just(unit), "source": _TEXT, "formula": _TEXT}
    )


def _quantity(unit: st.SearchStrategy[str]) -> st.SearchStrategy[dict[str, object]]:
    return st.fixed_dictionaries({"value": _COST, "unit": unit})


_BLOCK = st.fixed_dictionaries(
    {
        "entry_deadline": _translated(EPOCH_MS_UTC),
        "walk_start": _translated(EPOCH_MS_UTC),
        "entry_trail_bps": st.one_of(st.none(), st.integers(min_value=1, max_value=10_000)),
        "ceiling_price": st.one_of(
            st.none(),
            st.integers(min_value=1, max_value=100_000),
            st.floats(min_value=0.01, max_value=1.0e6),
        ),
        "time_stop_t": st.one_of(st.none(), _EPOCH),
        "oco": st.just(False),
        "costs": st.fixed_dictionaries(
            {
                "commission_rate": _quantity(st.just(FRACTION)),
                "min_commission": _quantity(_CURRENCY),
                "min_commission_applies": st.booleans(),
                "fx_applies": st.booleans(),
                "exit_edge_min_bps": _quantity(st.just(BPS)),
            }
        ),
    }
)


class TestRunConfigRoundTrip(PropertyTestCase):
    @given(_BLOCK)
    def test_from_jsonable_of_to_jsonable_is_the_identity(self, block: dict[str, object]) -> None:
        config = RunConfig.from_jsonable(block)
        self.assertEqual(RunConfig.from_jsonable(config.to_jsonable()), config)

    @given(_BLOCK)
    def test_the_rendered_block_is_strict_json_equal_to_the_input(self, block) -> None:
        # Text equality, not dict equality: ``1 == 1.0`` would hide an int
        # rendered where a float was stated.
        rendered = RunConfig.from_jsonable(block).to_jsonable()
        self.assertEqual(
            json.dumps(rendered, sort_keys=True, allow_nan=False),
            json.dumps(block, sort_keys=True, allow_nan=False),
        )
