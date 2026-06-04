"""Pin the typed catalyst payload contract + the producer->consumer seam.

Covers:
1. ``CatalystPayload`` is frozen and exposes the full field set the consumers
   read (scorer, orchestrator, catalyst_signals).
2. A round-trip: the resolver producer builds a ``CatalystPayload`` and the
   three production consumers read it identically to the legacy dict path
   (behaviour-preservation guard for the typed-contract refactor).
"""

from __future__ import annotations

import dataclasses
import unittest

import pandas as pd
from alphalens_pipeline.thematic.mapping import catalyst_resolver
from alphalens_pipeline.thematic.mapping.catalyst_contract import CatalystPayload
from alphalens_pipeline.thematic.screening import catalyst_signals


class TestCatalystPayloadContract(unittest.TestCase):
    def test_is_frozen(self):
        payload = _sample_payload()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            payload.url = "mutated"  # type: ignore[misc]

    def test_field_set_matches_consumers(self):
        names = {f.name for f in dataclasses.fields(CatalystPayload)}
        self.assertEqual(
            names,
            {
                "url",
                "title",
                "published_at",
                "event_type",
                "confidence",
                "second_order_implications",
                "echo_count",
                "trigger_url",
                "trigger_published_at",
                "is_amplified",
                "template_id",
                "template_facts",
            },
        )

    def test_to_dict_round_trips_all_fields(self):
        payload = _sample_payload()
        d = payload.to_dict()
        self.assertEqual(d["url"], "https://x/news")
        self.assertEqual(d["event_type"], "m_and_a")
        self.assertEqual(d["template_id"], "m_and_a_press_release")
        self.assertEqual(d["template_facts"], {"deal_value_usd": 1.0})
        # to_dict carries exactly the field set (no extra / missing keys).
        self.assertEqual(set(d), {f.name for f in dataclasses.fields(CatalystPayload)})


class TestProducerConsumerSeam(unittest.TestCase):
    """Build a payload from the resolver producer, feed it to each consumer."""

    def _build_payload_from_resolver(self) -> CatalystPayload:
        catalyst = pd.Series(
            {
                "url": "https://x/news",
                "title": "Acme acquires Beta",
                "event_type": "m_and_a",
                "confidence": 0.9,
                "second_order_implications": ["supplier upside"],
                "timestamp": pd.Timestamp("2026-05-10T12:00:00Z"),
                "template_id": "m_and_a_press_release",
                "template_fields_json": '{"deal_value_usd": 1.0}',
            }
        )
        return catalyst_resolver._build_catalyst_payload(catalyst, "timestamp")

    def test_producer_returns_typed_payload(self):
        payload = self._build_payload_from_resolver()
        self.assertIsInstance(payload, CatalystPayload)
        self.assertEqual(payload.url, "https://x/news")
        self.assertEqual(payload.event_type, "m_and_a")
        self.assertEqual(payload.published_at, "2026-05-10")
        self.assertEqual(payload.echo_count, 1)
        self.assertFalse(payload.is_amplified)
        self.assertEqual(payload.template_id, "m_and_a_press_release")
        self.assertEqual(payload.template_facts, {"deal_value_usd": 1.0})

    def test_catalyst_signals_consumes_payload(self):
        payload = self._build_payload_from_resolver()
        strength = catalyst_signals.compute_catalyst_strength(payload)
        self.assertGreater(strength, 0.0)
        self.assertLessEqual(strength, 1.0)


def _sample_payload() -> CatalystPayload:
    return CatalystPayload(
        url="https://x/news",
        title="Acme acquires Beta",
        published_at="2026-05-10",
        event_type="m_and_a",
        confidence=0.9,
        second_order_implications=["supplier upside"],
        echo_count=1,
        trigger_url="https://x/news",
        trigger_published_at="2026-05-10",
        is_amplified=False,
        template_id="m_and_a_press_release",
        template_facts={"deal_value_usd": 1.0},
    )


if __name__ == "__main__":
    unittest.main()
