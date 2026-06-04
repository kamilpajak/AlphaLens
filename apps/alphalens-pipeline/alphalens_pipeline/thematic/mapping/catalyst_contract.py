"""Typed cross-stage contract for the catalyst payload.

The catalyst payload is the provenance object that links the extraction stage
(``thematic_events`` parquet) → the mapping stage
(``catalyst_resolver.find_trigger_event`` / ``find_template_catalyst_for_ticker``)
→ the screening stage (``scorer.score_candidates``) and the brief generator.
It used to be a bare ``dict`` whose key set was only knowable by reading the
consumers (``(catalyst_event or {}).get("event_type")`` etc.). This module
promotes it to a FROZEN dataclass so the contract is a single typed source of
truth — mirroring the ``trade_setup.model`` style (frozen dataclass + a
``to_dict`` that emits the boundary shape).

Field semantics:
- ``url`` / ``title`` / ``published_at`` — the catalyst (story-arc root) event.
- ``event_type`` / ``confidence`` / ``second_order_implications`` — the
  catalyst's classification (drives ``catalyst_signals.compute_catalyst_strength``).
- ``echo_count`` / ``trigger_url`` / ``trigger_published_at`` / ``is_amplified``
  — story-arc amplification metadata; when the resolver degraded to single-event
  mode ``catalyst is trigger`` so the trigger-* fields equal the primary fields
  and ``is_amplified`` is False.
- ``template_id`` / ``template_facts`` — typed-fact provenance when the catalyst
  is a template-extracted event; both ``None`` on Flash-extracted catalysts.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CatalystPayload:
    """Provenance object linking extraction → mapping → screening."""

    url: str
    title: str
    published_at: str
    event_type: str | None
    confidence: float | None
    second_order_implications: list[str]
    echo_count: int
    trigger_url: str
    trigger_published_at: str
    is_amplified: bool
    template_id: str | None
    template_facts: dict | None = field(default=None)

    def to_dict(self) -> dict:
        """Emit the boundary dict shape (1:1 with the historical payload keys)."""
        return {
            "url": self.url,
            "title": self.title,
            "published_at": self.published_at,
            "event_type": self.event_type,
            "confidence": self.confidence,
            "second_order_implications": self.second_order_implications,
            "echo_count": self.echo_count,
            "trigger_url": self.trigger_url,
            "trigger_published_at": self.trigger_published_at,
            "is_amplified": self.is_amplified,
            "template_id": self.template_id,
            "template_facts": self.template_facts,
        }


__all__ = ["CatalystPayload"]
