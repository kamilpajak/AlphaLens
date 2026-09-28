"""The seven trace event kinds of spec section 4.6, and their closed vocabularies.

The kinds are PUBLISHED: section 4.6 names them and the result envelope carries
them, so the tuple below is a contract and not an implementation detail. The
reason vocabularies follow ``bars.BARS_REASONS``: a reason outside the mapping
is a programming error caught at construction, never a shipped event.
"""

from __future__ import annotations

import unittest

from intent_replay import trace

SECTION_4_6_KINDS = (
    "entry_filled",
    "entry_expired",
    "stop_placed",
    "stop_moved",
    "tp_fired",
    "position_closed",
    "horizon_open",
)


class PublishedKindsTest(unittest.TestCase):
    def test_the_kinds_are_the_seven_of_section_4_6_in_that_order(self) -> None:
        self.assertEqual(trace.KINDS, SECTION_4_6_KINDS)

    def test_every_kind_has_exactly_one_event_class(self) -> None:
        self.assertEqual(tuple(cls.kind for cls in trace.EVENT_TYPES), SECTION_4_6_KINDS)


class ClosedVocabulariesTest(unittest.TestCase):
    """Published words, pinned. The reason mappings follow ``bars.BARS_REASONS``;
    ``OUTCOMES`` and ``LADDERS`` are the summary's own two."""

    def test_the_expiry_reasons_are_closed(self) -> None:
        self.assertEqual(set(trace.EXPIRY_REASONS), {"deadline"})

    def test_the_stop_move_reasons_are_closed(self) -> None:
        self.assertEqual(set(trace.STOP_MOVE_REASONS), {"trail", "reanchor-on-fill"})

    def test_the_close_reasons_are_closed(self) -> None:
        self.assertEqual(set(trace.CLOSE_REASONS), {"stop", "tp_complete", "time_stop"})

    def test_the_outcomes_are_closed(self) -> None:
        self.assertEqual(
            set(trace.OUTCOMES),
            {"no_fill", "closed_stop", "closed_tp", "closed_time_stop", "open"},
        )

    def test_the_ladders_are_closed(self) -> None:
        self.assertEqual(set(trace.LADDERS), {"initial_levels", "tp_tranches"})


class ReasonIsCheckedAtConstructionTest(unittest.TestCase):
    """An unregistered reason is a programming error, not a shipped event. Each
    arm carries its own positive control, so a dead check cannot hide behind a
    live one."""

    def test_an_unregistered_expiry_reason_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            trace.EntryExpired(t=1, tiers=(0,), reason="stop_above_rung")
        self.assertIn("stop_above_rung", str(caught.exception))

    def test_every_registered_expiry_reason_is_accepted(self) -> None:
        for reason in trace.EXPIRY_REASONS:
            with self.subTest(reason):
                self.assertEqual(trace.EntryExpired(t=1, tiers=(0,), reason=reason).reason, reason)

    def test_an_unregistered_stop_move_reason_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            trace.StopMoved(t=1, reason="tp-tranche-resize", before=63.0, after=64.0)

    def test_every_registered_stop_move_reason_is_accepted(self) -> None:
        for reason in trace.STOP_MOVE_REASONS:
            with self.subTest(reason):
                event = trace.StopMoved(t=1, reason=reason, before=63.0, after=64.0)
                self.assertEqual(event.reason, reason)

    def test_an_unregistered_close_reason_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            trace.PositionClosed(t=1, reason="cancelled", price=63.0, units=1.0)

    def test_every_registered_close_reason_is_accepted(self) -> None:
        for reason in trace.CLOSE_REASONS:
            with self.subTest(reason):
                event = trace.PositionClosed(t=1, reason=reason, price=63.0, units=1.0)
                self.assertEqual(event.reason, reason)

    def test_an_unregistered_ladder_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            trace.TpFired(
                t=1, tranche_index=0, price=74.0, units=1.0, proceeds=74.0, ladder="tp_ladder"
            )

    def test_every_registered_ladder_is_accepted(self) -> None:
        for ladder in trace.LADDERS:
            with self.subTest(ladder):
                event = trace.TpFired(
                    t=1, tranche_index=0, price=74.0, units=1.0, proceeds=74.0, ladder=ladder
                )
                self.assertEqual(event.ladder, ladder)


class SerializationTest(unittest.TestCase):
    def test_t_comes_first_then_the_kind_then_the_fields_in_order(self) -> None:
        event = trace.EntryFilled(t=1790170200000, tier_index=0, price=68.0, units=13.0, cash=884.0)
        self.assertEqual(
            list(trace.to_jsonable(event)),
            ["t", "kind", "tier_index", "price", "units", "cash"],
        )

    def test_the_values_are_the_event_values(self) -> None:
        event = trace.StopMoved(t=7, reason="trail", before=63.0, after=69.56)
        self.assertEqual(
            trace.to_jsonable(event),
            {"t": 7, "kind": "stop_moved", "reason": "trail", "before": 63.0, "after": 69.56},
        )

    def test_every_field_of_every_kind_reaches_the_output(self) -> None:
        # One serializer over ``dataclasses.fields``: a field added to any kind
        # cannot be forgotten in the rendering.
        built = {
            trace.EntryFilled: {"t": 1, "tier_index": 0, "price": 68.0, "units": 1.0, "cash": 68.0},
            trace.EntryExpired: {"t": 1, "tiers": (1,), "reason": "deadline"},
            trace.StopPlaced: {"t": 1, "level": 63.0},
            trace.StopMoved: {"t": 1, "reason": "trail", "before": 63.0, "after": 64.0},
            trace.TpFired: {
                "t": 1,
                "tranche_index": 0,
                "price": 74.0,
                "units": 1.0,
                "proceeds": 74.0,
                "ladder": "tp_tranches",
            },
            trace.PositionClosed: {"t": 1, "reason": "stop", "price": 63.0, "units": 1.0},
            trace.HorizonOpen: {"t": 1, "units": 1.0},
        }
        self.assertEqual(set(built), set(trace.EVENT_TYPES))
        for cls, kwargs in built.items():
            with self.subTest(cls.kind):
                rendered = trace.to_jsonable(cls(**kwargs))
                self.assertEqual(rendered, {"kind": cls.kind, **kwargs})


if __name__ == "__main__":
    unittest.main()
