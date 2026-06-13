"""The expert registry — ``id -> Expert`` instance, plus lookup helpers.

Per-expert, no god-object: the registry only holds instances and routes an id to
the matching expert; each expert owns its own score / assess logic. A new expert
is registered here. Display-only — nothing in candidate selection or ordering
reads the registry (the panel/score/assessment are characteristics until each
expert's Expert×EDGE correlation is validated).
"""

from __future__ import annotations

from alphalens_pipeline.experts.base import Expert
from alphalens_pipeline.experts.buffett.expert import BuffettExpert
from alphalens_pipeline.experts.oneil.expert import ONeilExpert

_REGISTRY: dict[str, Expert] = {}


def _register(expert: Expert) -> None:
    if expert.id in _REGISTRY:
        raise ValueError(f"duplicate expert id: {expert.id!r}")
    _REGISTRY[expert.id] = expert


def get_expert(expert_id: str) -> Expert:
    """Return the registered expert for ``expert_id``, or raise ``KeyError``."""
    try:
        return _REGISTRY[expert_id]
    except KeyError:
        raise KeyError(f"unknown expert id: {expert_id!r}") from None


def all_experts() -> tuple[Expert, ...]:
    """Every registered expert, registration order."""
    return tuple(_REGISTRY.values())


def expert_ids() -> tuple[str, ...]:
    """Every registered expert id, registration order."""
    return tuple(_REGISTRY.keys())


_register(BuffettExpert())
_register(ONeilExpert())

__all__ = ["all_experts", "expert_ids", "get_expert"]
