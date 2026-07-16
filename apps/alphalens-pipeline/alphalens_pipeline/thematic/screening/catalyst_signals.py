"""Catalyst-derived scoring signals for Layer 4.

Two complementary levers added on top of insider × FCFF × Magic Formula ×
technicals:

- :func:`compute_catalyst_strength` scores the news catalyst itself,
  producing a value in [0, 1]. Strong catalyst (e.g. NVIDIA Ising launch:
  product_launch event_type, high Flash confidence, multiple second-order
  beneficiaries) → high strength → lifts the entire downstream cohort via
  :func:`catalyst_floor`.

- :func:`is_deep_drawdown_reversal` is a per-candidate detector for the
  "thematic momentum reversal" archetype: oversold name + fresh catalyst
  + institutional volume surge. Fires when the candidate is set up to
  benefit from a thematic catalyst it's already pricing-distressed against.
  Discriminates WITHIN a cohort sharing a single catalyst.

The two are complementary: catalyst_strength alone uniformly lifts all
beneficiaries (can't pick QUBT over FORM); deep_drawdown_reversal alone
doesn't recognise strong-catalyst tailwind. Together they distinguish
"strong catalyst + good setup" (QUBT, 4/5) from "strong catalyst + bad
setup" (FORM, 3/5).

Origin: 2026-05-18 NVDA→QUBT 1-month replay analysis. Current scoring
gave all 4 quantum names conf 1/5 despite QUBT/QBTS/RGTI returning
+44.6%/+30.4%/+14.2% one month later.
"""

from __future__ import annotations

import hashlib
import json
import math

from alphalens_pipeline.thematic.extraction.schema import NOISE_EVENT_TYPES
from alphalens_pipeline.thematic.mapping.catalyst_contract import CatalystPayload

# Per-event-type tier weight (hand-calibrated; operator-feedback ledger
# will tune over time). Anchored against the 39-value EVENT_TYPES enum
# from :mod:`alphalens_pipeline.thematic.extraction.schema`. Noise types (opinion,
# promo, lifestyle, listicle, evergreen, sponsored) intentionally absent
# — they should already be filtered upstream, but a 0.0 fallback here
# means leaked noise contributes nothing to catalyst_strength.
EVENT_TYPE_TIER: dict[str, float] = {
    # Highest tier — definitive corporate-action / earnings catalysts
    "m_and_a": 1.00,
    "earnings": 0.95,
    "guidance": 0.90,
    "regulatory": 0.90,
    "bankruptcy": 0.85,
    "ipo": 0.85,
    "secondary": 0.80,
    "spinoff": 0.85,
    "restructuring": 0.80,
    "activist_position": 0.80,
    # Mid tier — meaningful but less binary
    "product_launch": 0.85,
    "product_retirement": 0.75,
    "contract_award": 0.75,
    "partnership": 0.70,
    "financing": 0.70,
    "dividend": 0.65,
    "buyback": 0.70,
    "exec_change": 0.70,
    "board_change": 0.60,
    "strike": 0.65,
    "layoffs": 0.65,
    "litigation": 0.65,
    "settlement": 0.60,
    "investigation": 0.70,
    "recall": 0.70,
    "breach": 0.75,
    "geopolitical": 0.70,
    "central_bank": 0.75,
    # Low tier — analyst/derivative signals
    "analyst": 0.55,
    "rating_change": 0.50,
    "price_target": 0.45,
    "macro": 0.50,
    # Catch-all
    "other": 0.30,
}

# Dimension weights in :func:`compute_catalyst_strength`. Sum to 1.0.
# Tilted toward event_type (largest single dimension) because the tier
# map encodes the strongest prior on market-moving potential. Confidence
# and SOI count are corroborating signals — they refine but don't drive.
_W_EVENT_TYPE = 0.40
_W_CONFIDENCE = 0.40
_W_SOI_COUNT = 0.20

# Anchor: 5 second-order beneficiaries identified by Flash = "rich" signal.
_SOI_SATURATION = 5

# Catalyst-floor breakpoints — convert continuous catalyst_strength into
# a discrete 0/1/2 bonus added to the candidate's weighted_score.
_FLOOR_STRONG_THRESHOLD = 0.70  # ≥ this → +2 (NVDA Ising-class)
# 0.45 cutoff: requires either mid-tier event type or (high conf + ≥1 SOI on
# 'other'-tier). Prior 0.25 was too low — an 'other' event (tier 0.30) with
# Flash conf ≥0.625 alone would clear it, causing rampant cohort inflation
# from any mildly confident extraction.
_FLOOR_MODERATE_THRESHOLD = 0.45

# Bumped ONLY when the SHAPE of the config-version stamp changes (a key added /
# removed / renamed), NEVER when a constant's value changes — a value change
# must surface as a different token, not a schema bump.
_STAMP_SCHEMA = 1

# Deep-drawdown-reversal thresholds. Drawdown threshold matches the
# renderer's setup classifier so the brief's "Pattern: deep drawdown"
# label and the scoring signal stay in lock-step.
_DEEP_DRAWDOWN_PCT_OFF_HIGH = -30.0
_VOLUME_SURGE_ZSCORE = 2.0


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def catalyst_config_version() -> str:
    """Poolability key for the catalyst-strength formula (ADR 0013 rule R3).

    Returns ``catalyst-v{schema}-{sha256(canonical_json)[:12]}`` where the
    canonical JSON covers every live constant that shapes
    :func:`compute_catalyst_strength` / :func:`catalyst_floor`: the three
    dimension weights, the SOI saturation anchor, both floor thresholds, and
    the full ``EVENT_TYPE_TIER`` map (sorted key/value pairs).

    Bump semantics: the token drifts AUTOMATICALLY on any change to a weight,
    threshold, tier value, or the strength definition's constant inputs —
    never edit the token by hand. ``_STAMP_SCHEMA`` bumps only when the stamp
    SHAPE changes (key added / removed / renamed). Rows carrying different
    tokens were scored under different formulas and must NEVER pool in EDGE
    calibration; a missing column marks the pre-versioning pool.

    Constants are read at call time from the live module namespace so tests
    can ``mock.patch`` a constant and pin token drift.
    """
    config = {
        "schema": _STAMP_SCHEMA,
        "w_event_type": _W_EVENT_TYPE,
        "w_confidence": _W_CONFIDENCE,
        "w_soi_count": _W_SOI_COUNT,
        "soi_saturation": _SOI_SATURATION,
        "floor_strong": _FLOOR_STRONG_THRESHOLD,
        "floor_moderate": _FLOOR_MODERATE_THRESHOLD,
        "event_type_tier": sorted(EVENT_TYPE_TIER.items()),
    }
    canon = json.dumps(config, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]
    return f"catalyst-v{_STAMP_SCHEMA}-{digest}"


def compute_catalyst_strength(event: CatalystPayload | None) -> float:
    """Score the news catalyst itself in [0, 1].

    ``event`` is the typed :class:`CatalystPayload` from the resolver
    (event_type, confidence, second_order_implications, ...). Returns 0.0
    when ``event`` is None.
    """
    if not event:
        return 0.0

    event_type = str(event.event_type or "other").lower()
    # Defence-in-depth: noise types already filtered upstream, but if any
    # leak through return 0 strength outright — Flash's own classification
    # asserts the article is non-market-moving, so even high confidence /
    # rich SOIs on noise don't earn cohort lift.
    if event_type in NOISE_EVENT_TYPES:
        return 0.0
    tier = EVENT_TYPE_TIER.get(event_type, EVENT_TYPE_TIER["other"])

    conf_raw = _safe_float(event.confidence)
    conf = max(0.0, min(1.0, conf_raw)) if conf_raw is not None else 0.0

    soi = event.second_order_implications
    soi_count = len(soi) if soi else 0
    soi_norm = min(1.0, soi_count / _SOI_SATURATION)

    strength = _W_EVENT_TYPE * tier + _W_CONFIDENCE * conf + _W_SOI_COUNT * soi_norm
    return max(0.0, min(1.0, strength))


def catalyst_floor(strength: float) -> int:
    """Discretise catalyst_strength into a 0-2 weighted-score bonus.

    Strong catalyst (≥0.70) → +2 (lifts entire cohort by two notches).
    Moderate (≥0.45) → +1. Weak (<0.45) → 0 (no lift).
    """
    s = _safe_float(strength)
    if s is None:
        return 0
    if s >= _FLOOR_STRONG_THRESHOLD:
        return 2
    if s >= _FLOOR_MODERATE_THRESHOLD:
        return 1
    return 0


def is_deep_drawdown_reversal(row: dict) -> bool:
    """Per-candidate detector: oversold + fresh catalyst + volume surge.

    Three conjunction conditions:
    - ``technical_pct_off_52w_high`` ≤ -30% (deep drawdown setup)
    - ``source_event_url`` present (fresh catalyst from Phase B)
    - ``technical_volume_zscore`` ≥ 2.0 (institutional accumulation)

    All thresholds reuse already-justified constants in the codebase /
    standard signal thresholds. NOT optimised on the NVDA→QUBT cohort.
    """
    pct_off = _safe_float(row.get("technical_pct_off_52w_high"))
    if pct_off is None or pct_off > _DEEP_DRAWDOWN_PCT_OFF_HIGH:
        return False

    # URL check must handle pd.NA / float('nan') / None uniformly. A naive
    # ``not str(url).strip()`` fails on NaN (str(nan) = "nan" is truthy) —
    # would silently flag missing-catalyst rows as having a valid setup.
    url = row.get("source_event_url")
    if url is None:
        return False
    url_str = str(url).strip().lower()
    if url_str in ("", "nan", "none", "<na>"):
        return False

    vol_z = _safe_float(row.get("technical_volume_zscore"))
    return not (vol_z is None or vol_z < _VOLUME_SURGE_ZSCORE)


__all__ = [
    "EVENT_TYPE_TIER",
    "catalyst_config_version",
    "catalyst_floor",
    "compute_catalyst_strength",
    "is_deep_drawdown_reversal",
]
