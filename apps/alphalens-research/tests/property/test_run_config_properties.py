"""Property: the stated run configuration round-trips through its own wire
form (``intent_replay.config``, spec section 5.2).

``RunConfig.to_jsonable`` is the block PR 7 puts in the result envelope, and
``from_jsonable`` is the only way a block comes in; if the two drift, a run
stops being reconstructible from its own output, which section 5.2 exists to
guarantee. The strategy draws only configurations ``from_jsonable`` accepts —
unavoidable in either direction, since the refused ones have no dataclass to
compare — and varies every field, including each optional scalar's null/value
arm and BOTH arms of the conditional fx key set (#1592): the same-currency
block with one key and the cross-currency block with four. The account currency
is drawn with the block rather than fixed, because it is what selects the arm.
"""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st
from intent_replay.config import RunConfig
from intent_replay.units import BPS, EPOCH_MS_UTC, FRACTION, ISO_4217, PERCENT

from .base import PropertyTestCase

_TEXT = st.text(min_size=1, max_size=40)
_EPOCH = st.integers(min_value=0, max_value=4_102_444_800_000)
_COST = st.one_of(st.integers(min_value=0, max_value=10_000), st.floats(0.0, 1.0e6))
_CODES = ("USD", "EUR", "PLN", "GBP", "CHF")
_CURRENCY = st.sampled_from(_CODES)


def _translated(
    unit: str, value: st.SearchStrategy[object] = _EPOCH
) -> st.SearchStrategy[dict[str, object]]:
    return st.fixed_dictionaries(
        {"kind": _TEXT, "value": value, "unit": st.just(unit), "source": _TEXT, "formula": _TEXT}
    )


def _quantity(unit: st.SearchStrategy[str]) -> st.SearchStrategy[dict[str, object]]:
    return st.fixed_dictionaries({"value": _COST, "unit": unit})


def _fx(account: str, instrument: str) -> st.SearchStrategy[dict[str, object]]:
    """The conditional key set of section 5.2.1: one key when the codes agree,
    four when they differ, and the rate's unit SPELLS the direction."""
    settlement = _translated(ISO_4217, st.just(instrument))
    if instrument == account:
        return st.fixed_dictionaries({"instrument_currency": settlement})
    return st.fixed_dictionaries(
        {
            "instrument_currency": settlement,
            "mid_rate": _translated(
                f"{instrument}_per_{account}", st.floats(min_value=1e-3, max_value=1e3)
            ),
            "round_trip_cost_rate": _quantity(st.just(FRACTION)),
            "sizing_buffer_pct": st.fixed_dictionaries(
                {"value": st.floats(min_value=0.0, max_value=99.9), "unit": st.just(PERCENT)}
            ),
        }
    )


def _block(account: str, instrument: str) -> st.SearchStrategy[dict[str, object]]:
    return st.fixed_dictionaries(
        {
            "entry_deadline": _translated(EPOCH_MS_UTC),
            "walk_start": _translated(EPOCH_MS_UTC),
            # A distance is a POLICY the walk applies; null is OFF. No upper
            # bound: section 5.2 publishes the key as an integer >= 1, and the
            # deployment rail that caps the flag at 150 is not a document fact.
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
                    # The minimum is quoted in the INSTRUMENT's currency and is
                    # checked against `fx.instrument_currency` since #1592.
                    "min_commission": _quantity(st.just(instrument)),
                    "min_commission_applies": st.booleans(),
                    "exit_edge_min_bps": _quantity(st.just(BPS)),
                }
            ),
            "fx": _fx(account, instrument),
        }
    )


_PAIRS = st.tuples(_CURRENCY, _CURRENCY)


@st.composite
def _account_and_block(draw: st.DrawFn) -> tuple[str, dict[str, object]]:
    account, instrument = draw(_PAIRS)
    return account, draw(_block(account, instrument))


class TestRunConfigRoundTrip(PropertyTestCase):
    @given(_account_and_block())
    def test_from_jsonable_of_to_jsonable_is_the_identity(
        self, drawn: tuple[str, dict[str, object]]
    ) -> None:
        account, block = drawn
        config = RunConfig.from_jsonable(block, account_currency=account)
        self.assertEqual(
            RunConfig.from_jsonable(config.to_jsonable(), account_currency=account), config
        )

    @given(_account_and_block())
    def test_the_rendered_block_is_strict_json_equal_to_the_input(self, drawn) -> None:
        # Text equality, not dict equality: ``1 == 1.0`` would hide an int
        # rendered where a float was stated.
        account, block = drawn
        rendered = RunConfig.from_jsonable(block, account_currency=account).to_jsonable()
        self.assertEqual(
            json.dumps(rendered, sort_keys=True, allow_nan=False),
            json.dumps(block, sort_keys=True, allow_nan=False),
        )

    @given(_account_and_block())
    def test_applies_is_the_comparison_of_the_two_stated_codes(self, drawn) -> None:
        account, block = drawn
        config = RunConfig.from_jsonable(block, account_currency=account)
        applies = config.costs.fx.applies
        self.assertEqual(applies, config.fx.instrument_currency.value != account)
        self.assertEqual(len(config.to_jsonable()["fx"]), 4 if applies else 1)

    def test_the_pair_space_reaches_both_arms_of_the_key_set(self) -> None:
        # BRANCH coverage of the generator, enumerated rather than assumed. A
        # property can be true and empty: if the two codes were drawn so that
        # they always differed, every test above would pass while proving the
        # round trip for ONE shape of block and claiming both. Five codes give
        # 25 ordered pairs, 5 of them equal, so the same-currency arm is one
        # draw in five.
        pairs = [(account, instrument) for account in _CODES for instrument in _CODES]
        same = [pair for pair in pairs if pair[0] == pair[1]]
        self.assertEqual((len(same), len(pairs)), (5, 25))
        self.assertEqual(len(pairs) - len(same), 20)
