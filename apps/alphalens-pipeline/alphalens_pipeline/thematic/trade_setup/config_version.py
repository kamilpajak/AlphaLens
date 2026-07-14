"""Poolability key for the trade-setup GEOMETRY (ADR 0013, action item 1).

``SCHEMA_VERSION`` is a contract of JSON *shape* and never bumps on a value
change; this token is the complement — a canonical JSON of every constant that
shapes ladder geometry, so two setups built under different constants can never
pool silently. Covers ALL four geometry modules (builder + ladder + levels +
sizing): a builder-only token would still let a TP-R-multiple or tier-spacing
change slip through, the exact failure class the key exists to prevent.

Constants are read at call time (lazy imports keep the module a leaf and let
tests monkeypatch a constant to pin token drift), mirroring
``feedback/ladder_config.py::ladder_config_version``.
"""

from __future__ import annotations

import json

# Bumped ONLY when the SHAPE of this stamp changes (a key added / removed /
# renamed), NEVER when a constant's value changes — a value change must surface
# as a different token, not a schema bump.
_STAMP_SCHEMA = 1


def setup_builder_config_version() -> str:
    """Return a canonical JSON token over the trade-setup geometry constants."""
    from alphalens_pipeline.thematic.trade_setup import builder, ladder, levels, sizing

    config = {
        "schema": _STAMP_SCHEMA,
        # builder.py — bar floor, swing detection, stop/entry placement, risk budget
        "min_bars": builder._MIN_BARS,
        "swing_threshold_mult": builder._SWING_THRESHOLD_MULT,
        "stop_atr_buffer": builder._STOP_ATR_BUFFER,
        "shallow_pullback_mult": builder._SHALLOW_PULLBACK_MULT,
        "deep_fallback_mult": builder._DEEP_FALLBACK_MULT,
        "disaster_floor_frac": builder._DISASTER_FLOOR_FRAC,
        "default_risk_budget_pct": builder._DEFAULT_RISK_BUDGET_PCT,
        # ladder.py — tier/tranche spacing and TP fallbacks
        "min_spacing_mult": ladder._MIN_SPACING_MULT,
        "min_stop_dist_mult": ladder._MIN_STOP_DIST_MULT,
        "r_multiple_fallback": list(ladder._R_MULTIPLE_FALLBACK),
        # levels.py — swing-zone clustering
        "cluster_radius_mult": levels._CLUSTER_RADIUS_MULT,
        # sizing.py — exposure cap
        "max_exposure_pct": sizing._MAX_EXPOSURE_PCT,
    }
    return json.dumps(config, sort_keys=True, separators=(",", ":"))
