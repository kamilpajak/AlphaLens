"""The measured LIVE numbers behind issue #1112 (SMG, 2026-08-24), as constants.

NOT a test module — a shared fixture imported by the three test files that pin
the fix (``tests/paper/test_exit_geometry_spec.py``,
``tests/brokers/automanager/test_entry_watch_wiring.py``,
``tests/brokers/automanager/test_live_exit_decision.py``). Every value below was
read off the live journals on the VPS
(``~/.alphalens/broker_orders/live/{entry_trails,standalone_stops,picks}.jsonl``)
and quoted in the issue; the derived values are reproduced by
``TestIncidentAnchorsReconcile`` in ``tests/paper/test_exit_geometry_spec.py``
against the real ``build_exit_geometry_spec``, so a silent change to the anchor
arithmetic breaks a test rather than the money rail.

The incident in one line: the top entry tier (59.786017) sat ABOVE the exit
target the policy computed off the alloc-weighted planned blend (59.6277), so
the fill at 59.9261 was already past its own take-profit and the exit engine
sold it 62 seconds later for about -380 bps net.
"""

from __future__ import annotations

# --- measured, from picks.jsonl (the armed spec) --------------------------
SMG_TIERS: tuple[tuple[float, float], ...] = (
    (59.786017, 21.07),
    (55.754064, 32.14),
    (53.599998, 46.79),
)
"""(limit, alloc_pct) per entry tier, in brief order."""

SMG_TP_TRANCHES: tuple[float, ...] = (65.25, 68.34, 70.835)
"""The brief's own take-profit targets — the lowest (65.25) sits 5.46 ABOVE the
top entry tier, so the brief itself was internally consistent."""

SMG_DISASTER_STOP = 49.412
SMG_ATR = 2.6880

# --- derived by build_exit_geometry_spec (reproduced by a self-check test) --
SMG_PLANNED_BLEND = 55.5957090157
"""Alloc-weighted (NOT share-weighted) blend over all three intended tiers."""

SMG_GEOMETRY_TP = 59.6277090157
"""blend + 1.5 * ATR — the single ``geometry`` tranche the exit engine fired on."""

SMG_GEOMETRY_STOP = 51.5637090157
"""blend - 1.5 * ATR."""

# --- the REALISED-anchor mirror of the three above (issue #1114) -----------
# The ``/edge`` what-if lens anchored its bracket on the tiers that TOUCHED in
# the bar walk rather than on all intended tiers. On SMG only the top tier was
# reachable, so the lens's anchor is that tier's LIMIT -- not the broker's
# actual fill (59.9261), because the replay fills a tier AT its limit.
SMG_E1_LIMIT = 59.786017
"""Top entry tier limit; the realised anchor when only that tier touches."""

SMG_REALISED_GEOMETRY_TP = 63.818017
"""E1 limit + 1.5 * ATR. Reproduced by a self-check test, not typed from prose."""

SMG_REALISED_GEOMETRY_STOP = 55.754017
"""E1 limit - 1.5 * ATR."""

SMG_AVG_PRICE_GEOMETRY_TP = 63.9581
"""The target a THIRD anchor -- the broker's realised average fill price -- would
give (``SMG_ACTUAL_FILL + 1.5 * ATR``). NO code path produces it today; it is the
number #1112 step 4 would move live onto. Named here so a future third
``AnchorMode`` has its incident constant already measured."""

# --- measured, from entry_trails.jsonl / standalone_stops.jsonl ------------
SMG_TOUCH_BID = 59.77
"""The touch reference bid at 17:20:02 UTC; also the recorded trough."""

SMG_ACTUAL_FILL = 59.9261
"""The native trailing BUY filled here at 18:00:58 UTC — ABOVE its own tier
limit 59.786017 (by 23.4 bps; the issue prose says '40 bps', which its own
quoted prices do not support, so tests pin the PRICES)."""

SMG_EXIT_DECISION_BID = 59.89
"""The bid at 18:02:00 UTC when the exit engine decided to sell."""

SMG_D_BPS = 50
"""``ALPHALENS_BROKER_ENTRY_TRAIL_BPS`` on the live unit at the time."""

SMG_ROUND_TRIP_FEE_BPS = 383.7
"""Round-trip cost on the realised one-share notional (Saxo LIVE: 0.08% with a
$1 per-side minimum, plus 0.25% FX per conversion). Reproduces the issue's
measured -380 bps. Asserted to 0.1 bps in the cost-gate test."""

# --- the three healthy tiers that must keep arming (regression) ------------
ETSY_E3_LIMIT = 67.62
ETSY_E3_TARGET = 77.33634
"""ETSY tier 3 and its geometry target, from the same live journal — quoted in
the issue, not independently recomputed here."""


def smg_brief_trade_setup() -> dict:
    """The SMG ``brief_trade_setup`` dict in the shape ``build_exit_geometry_spec``
    reads (verified against a real row in ``~/.alphalens/thematic_briefs/``)."""
    return {
        "status": "OK",
        "schema_version": "1.0.0",
        "suggested_size_pct": 1.0,
        "disaster_stop": SMG_DISASTER_STOP,
        "entry_tiers": [
            {"limit": limit, "alloc_pct": alloc, "tag": f"E{i + 1}"}
            for i, (limit, alloc) in enumerate(SMG_TIERS)
        ],
        "tp_tranches": [
            {"target": target, "tranche_pct": pct, "r_multiple": 0.0, "tag": f"tp{i + 1}"}
            for i, (target, pct) in enumerate(zip(SMG_TP_TRANCHES, (33.0, 33.0, 34.0), strict=True))
        ],
        "atr": SMG_ATR,
    }


def smg_geometry_stamp() -> dict:
    """The ``geometry`` blob the router stamps on the ``watch_open`` journal line
    (``control_loop._geometry_shadow_stamp`` shape), carrying the SMG numbers."""
    return {
        "policy_name": "atr_bracket_1p5",
        "policy_version": 1,
        "planned_blend": SMG_PLANNED_BLEND,
        "geometry_stop": SMG_GEOMETRY_STOP,
        "geometry_tp": SMG_GEOMETRY_TP,
        "k_atr": 1.5,
        "atr": SMG_ATR,
        "ceiling_price": None,
        "applied": True,
    }
