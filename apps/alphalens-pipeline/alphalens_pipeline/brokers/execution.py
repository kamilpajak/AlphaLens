"""Broker-agnostic execution planning: ladder decomposition + poolability key.

Pure module — no vendor imports, no I/O. Two exports:

- :func:`decompose_setup_plan` — maps a sized :class:`SetupPlan` (T5 SETUP
  output rendered by ``paper/sizing.py::compute_setup_plan``) onto a list of
  :class:`BracketOrderRequest`, ONE order-attached 3-way bracket per NON-ZERO
  entry tier. Decision record: design memo §P2
  (``docs/research/saxo_broker_layer_design_2026_07_17.md``). The live
  netting read (``PositionNettingMode="Intraday"`` /
  ``PositionNettingProfile="FifoRealTime"``, 2026-07-17) kills the
  fill-then-attach-exits alternative — position-attached related orders only
  work on End-of-Day netting — so exits MUST be order-attached at entry time,
  and Saxo caps related orders at exactly one Limit + one stop-type with
  identical Amount across all three. Hence: per-tier brackets, tier-sized
  Amount, the shared disaster-stop PRICE on every bracket (children activate
  only when their tier fills, so aggregate stop coverage always equals filled
  quantity — economically equivalent to the replay's single shared stop).

- :func:`execution_config_version` — the ADR 0013 R3 poolability key stamped
  on every submission record (``~/.alphalens/broker_orders/submissions.jsonl``)
  and echoed by the CLI. A bump is a cohort boundary: forward-only, existing
  records are never restamped, analyses never pool across tokens. Live fills
  are a NEW measurement source (T8) — never pooled with broker-free replays.

Fidelity deliberately LOST vs the replay ladder (memo §P2 decision record):
tranche_pct scale-out WITHIN a tier (each tier's whole qty exits at one
target); intermediate targets when tranches > tiers; ratchet/TP1->BE stop
moves (P3+); partial-tier-fill child-amount behaviour is an open question
observed via the SIM order probe.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Literal

from alphalens_pipeline.brokers.contract import BracketOrderRequest, InstrumentRef
from alphalens_pipeline.paper.constants import DEFAULT_ORDER_TTL_DAYS
from alphalens_pipeline.paper.sizing import SetupPlan

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Policy constants — every _UPPER_CASE name below is covered by
# execution_config_version() (pinned by tests/brokers/test_execution_config_
# version.py, which sweeps the module namespace). Change a VALUE and the token
# drifts automatically; never edit the token by hand.
# ---------------------------------------------------------------------------

# Bumped ONLY when the SHAPE of the stamp changes (key added/removed/renamed),
# NEVER when a constant's value changes — a value change must surface as a
# different digest, not a schema bump.
_STAMP_SCHEMA = "1"

# One 3-way bracket (entry Limit + TP Limit child + StopIfTraded child) per
# NON-ZERO entry tier, placed as a single POST each.
_DECOMPOSITION_MODE = "per-tier-bracket"

# Bracket for tier k takes tp_tranches[min(k, len-1)].target_price, keyed on
# the tier's ORIGINAL tier_index (zero-qty skips do not shift the pairing).
_TP_ASSIGNMENT_POLICY = "index-clamped-last"

# Zero-qty tiers (deliberately emitted by compute_setup_plan so the intent is
# recorded) are skipped with a structured log entry — never POSTed.
_ZERO_QTY_TIER_POLICY = "skip-log"

# When tranches > tiers the intermediate targets are unused; when tiers >
# tranches the deep tiers reuse the LAST target.
_EXCESS_TRANCHE_POLICY = "clamp"

# Saxo stop-child order type for the disaster stop.
_STOP_ORDER_TYPE = "StopIfTraded"

# Exits outlive the entry's TTL — GoodTillCancel on both children.
_EXIT_DURATION = "GoodTillCancel"

# Entry duration: GoodTillDate with a DATE-ONLY expiration (ExpirationDate-
# ContainsTime=false), computed entry_ttl_days TRADING days ahead on the
# venue's exchange calendar — exchange-local HH:mm rules avoided entirely.
_ENTRY_DURATION = "GoodTillDate-date-only"

# order_ttl_days == 0 is the planner's "field absent" sentinel — fall back to
# the paper-planner default so the two consumers cannot drift.
_TTL_ZERO_SENTINEL_DAYS = DEFAULT_ORDER_TTL_DAYS

# POST /trade/v2/orders/precheck runs before EVERY real placement POST.
_PRECHECK_REQUIRED = True

# ManualOrder pinned false on parent and both children (generated/routed
# without human intervention — Saxo's definition; field is becoming mandatory).
_MANUAL_ORDER = False

# Prices are quantized to the instrument's tick size by nearest-rounding...
_TICK_QUANTIZE_POLICY = "nearest"

# ...and the placement HARD-FAILS if the adjustment exceeds this cap — the
# quantization must be a rounding, not a silent price change. 25 bps = half a
# $0.01 tick at a $2 price floor; ladder limits come from real price data so
# a larger drift means the tick scheme disagrees with the setup's price scale.
_MAX_TICK_ADJUSTMENT_BPS = 25.0


def execution_config_version() -> str:
    """Poolability key for the execution/decomposition policy (ADR 0013 R3).

    Returns ``execution-v{schema}-{sha256(canonical_json)[:12]}`` over every
    policy constant above. Constants are read at CALL TIME from the live
    module namespace so ``mock.patch.object`` drift tests work. Rows carrying
    different tokens were executed under different policies and must NEVER
    pool in any live-fill analysis; a bump is a forward-only cohort boundary.
    """
    config = {
        "schema": _STAMP_SCHEMA,
        "decomposition_mode": _DECOMPOSITION_MODE,
        "tp_assignment_policy": _TP_ASSIGNMENT_POLICY,
        "zero_qty_tier_policy": _ZERO_QTY_TIER_POLICY,
        "excess_tranche_policy": _EXCESS_TRANCHE_POLICY,
        "stop_order_type": _STOP_ORDER_TYPE,
        "exit_duration": _EXIT_DURATION,
        "entry_duration": _ENTRY_DURATION,
        "ttl_zero_sentinel_days": _TTL_ZERO_SENTINEL_DAYS,
        "precheck_required": _PRECHECK_REQUIRED,
        "manual_order": _MANUAL_ORDER,
        "tick_quantize_policy": _TICK_QUANTIZE_POLICY,
        "max_tick_adjustment_bps": _MAX_TICK_ADJUSTMENT_BPS,
    }
    canon = json.dumps(config, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]
    return f"execution-v{_STAMP_SCHEMA}-{digest}"


def decompose_setup_plan(
    setup_plan: SetupPlan,
    instrument: InstrumentRef,
    *,
    side: Literal["BUY", "SELL"] = "BUY",
) -> list[BracketOrderRequest]:
    """Map a sized :class:`SetupPlan` onto per-tier bracket requests.

    One :class:`BracketOrderRequest` per NON-ZERO entry tier:

    - ``quantity`` = ``tier.qty``; ``entry_limit`` = ``tier.limit_price``;
    - ``stop_loss`` = the shared ``disaster_stop`` price (tier-sized Amount);
    - ``take_profit`` = ``tp_tranches[min(tier_index, len-1)].target_price``,
      or ``None`` when the plan has no tranches (stop-only bracket);
    - ``entry_ttl_days`` = ``order_ttl_days`` with the 0 sentinel resolved to
      :data:`_TTL_ZERO_SENTINEL_DAYS`;
    - ``client_request_id`` = a FRESH uuid4 per bracket (Saxo ``x-request-id``
      dedup token — reused only when retrying the SAME logical bracket).

    Zero-qty tiers are skipped with a structured log entry and never POSTed
    (:data:`_ZERO_QTY_TIER_POLICY`).
    """
    ttl_days = (
        setup_plan.order_ttl_days if setup_plan.order_ttl_days > 0 else (_TTL_ZERO_SENTINEL_DAYS)
    )
    tranches = setup_plan.tp_tranches

    brackets: list[BracketOrderRequest] = []
    for tier in setup_plan.entry_tiers:
        if tier.qty <= 0:
            logger.info(
                "skipping zero-qty tier: ticker=%s mic=%s tier_index=%d limit=%.4f "
                "alloc_pct=%.2f (policy=%s)",
                instrument.ticker,
                instrument.exchange_mic,
                tier.tier_index,
                tier.limit_price,
                tier.alloc_pct,
                _ZERO_QTY_TIER_POLICY,
            )
            continue
        take_profit: float | None = None
        if tranches:
            take_profit = tranches[min(tier.tier_index, len(tranches) - 1)].target_price
        brackets.append(
            BracketOrderRequest(
                instrument=instrument,
                side=side,
                quantity=tier.qty,
                entry_limit=tier.limit_price,
                stop_loss=setup_plan.disaster_stop,
                take_profit=take_profit,
                entry_ttl_days=ttl_days,
                client_request_id=str(uuid.uuid4()),
            )
        )
    return brackets


__all__ = [
    "decompose_setup_plan",
    "execution_config_version",
]
