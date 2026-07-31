"""JSON round-trip codec for :class:`~alphalens_pipeline.trade_intent.schema.TradeIntent`.

PR-7 of the broker-manager extraction arc (memo
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``
section 5): the client now persists the FULL ``TradeIntent`` into
``picks.jsonl`` at arm time, and the daemon drains + decodes it — no brief
touched on the daemon side. Pure stdlib (``json``/``dataclasses``); no I/O.

``intent_to_jsonable`` is a thin wrapper over ``dataclasses.asdict`` (every
field is str/int/float/None/tuple/nested-dataclass, so ``asdict`` already
yields a fully JSON-serializable dict). ``intent_from_jsonable`` is explicit
reconstruction — it dispatches each reaction-plan entry on its ``"kind"``
literal via a small registry and never hand-decodes the well-typed leaves.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from typing import Any

from alphalens_pipeline.trade_intent.schema import (
    EntryTierSpec,
    ExitGeometrySpec,
    InitialLevels,
    InstrumentHint,
    IntentMeta,
    ModelPush,
    ReactionPrimitive,
    ReanchorOnFill,
    TpTrancheSpec,
    TradeIntent,
    TradeSpec,
    TrailingStop,
)

logger = logging.getLogger(__name__)


class TradeIntentDecodeError(ValueError):
    """Raised when a jsonable payload cannot be reconstructed into a TradeIntent.

    Covers: the payload (or a nested leaf) is not a mapping, a required key is
    missing, a reaction-plan entry's ``"kind"`` is not in the known registry,
    or a value has the wrong type (a caught ``TypeError``/``KeyError``/
    ``ValueError`` re-raised with a clear message).
    """


# Dispatch registry for the discriminated reaction-plan union (memo revision
# R3) — bounded vocabulary, mirrors ``ReactionPrimitive`` in schema.py.
_REACTION_BY_KIND: dict[str, type[ReactionPrimitive]] = {
    "reanchor_on_fill": ReanchorOnFill,
    "trailing_stop": TrailingStop,
    "model": ModelPush,
}


def intent_to_jsonable(intent: TradeIntent) -> dict[str, Any]:
    """Render a :class:`TradeIntent` into a fully JSON-serializable dict."""
    return dataclasses.asdict(intent)


def _require_mapping(data: Any, *, what: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise TradeIntentDecodeError(f"{what} must be a mapping, got {type(data).__name__}")
    return data


def _filtered(cls: type, data: Mapping[str, Any]) -> dict[str, Any]:
    """Filter ``data`` down to ``cls``'s declared field names before construction.

    Unknown keys are dropped (forward-compat: a newer client may carry fields an
    older daemon does not model). With a single client + one ``schema_version``
    today, an unknown key instead signals drift or a typo (e.g. ``limit_pirce``)
    whose value would silently vanish — log it at WARNING so drift surfaces early
    (zen review, PR-7). The decoded object is identical either way.
    """
    field_names = {f.name for f in dataclasses.fields(cls)}
    extra = sorted(k for k in data if k not in field_names)
    if extra:
        logger.warning(
            "TradeIntent decode: dropping unknown %s key(s) %s (schema drift or typo?)",
            cls.__name__,
            extra,
        )
    return {k: v for k, v in data.items() if k in field_names}


def _decode_reaction_primitive(raw: Any) -> ReactionPrimitive:
    entry = _require_mapping(raw, what="a reaction-plan entry")
    kind = entry.get("kind")
    cls = _REACTION_BY_KIND.get(str(kind))
    if cls is None:
        raise TradeIntentDecodeError(
            f"unknown reaction-plan kind {kind!r}; known kinds: {sorted(_REACTION_BY_KIND)}"
        )
    return cls(**_filtered(cls, entry))


def _decode_exit(raw: Any) -> ExitGeometrySpec | None:
    if raw is None:
        return None
    exit_map = _require_mapping(raw, what="exit")
    initial_levels_raw = _require_mapping(exit_map["initial_levels"], what="exit.initial_levels")
    initial_levels = InitialLevels(**_filtered(InitialLevels, initial_levels_raw))
    reaction_plan = tuple(
        _decode_reaction_primitive(entry) for entry in exit_map.get("reaction_plan") or ()
    )
    return ExitGeometrySpec(initial_levels=initial_levels, reaction_plan=reaction_plan)


def _decode_spec(raw: Any) -> TradeSpec:
    spec_map = _require_mapping(raw, what="spec")
    entry_tiers = tuple(
        EntryTierSpec(**_filtered(EntryTierSpec, _require_mapping(t, what="an entry tier")))
        for t in spec_map.get("entry_tiers") or ()
    )
    tp_tranches = tuple(
        TpTrancheSpec(**_filtered(TpTrancheSpec, _require_mapping(t, what="a tp tranche")))
        for t in spec_map.get("tp_tranches") or ()
    )
    kwargs = _filtered(TradeSpec, spec_map)
    kwargs["entry_tiers"] = entry_tiers
    kwargs["tp_tranches"] = tp_tranches
    return TradeSpec(**kwargs)


def intent_from_jsonable(data: Mapping[str, Any]) -> TradeIntent:
    """Reconstruct a :class:`TradeIntent` from a jsonable mapping.

    Raises :class:`TradeIntentDecodeError` for: a non-mapping payload/leaf, a
    missing required key, an unknown reaction-plan ``"kind"``, or a value
    with the wrong shape/type.
    """
    top = _require_mapping(data, what="the TradeIntent payload")
    try:
        instrument = InstrumentHint(
            **_filtered(InstrumentHint, _require_mapping(top["instrument"], what="instrument"))
        )
        spec = _decode_spec(top["spec"])
        meta = IntentMeta(**_filtered(IntentMeta, _require_mapping(top["meta"], what="meta")))
        exit_spec = _decode_exit(top.get("exit"))
        kwargs = _filtered(TradeIntent, top)
        kwargs["instrument"] = instrument
        kwargs["spec"] = spec
        kwargs["meta"] = meta
        kwargs["exit"] = exit_spec
        return TradeIntent(**kwargs)
    except TradeIntentDecodeError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise TradeIntentDecodeError(f"malformed TradeIntent payload: {exc}") from exc


__all__ = [
    "TradeIntentDecodeError",
    "intent_from_jsonable",
    "intent_to_jsonable",
]
