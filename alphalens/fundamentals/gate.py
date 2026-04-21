"""Pure functions that translate a fundamentals-feature dict into a score
multiplier (soft guardrail) and a hard-reject decision for Guardrails.

Design (per plan, issue #14):
  - `fundamental_gate_score` returns a value in [floor, 1.0]. The technical
    composite score is multiplied by this. 1.0 = no concern, floor = all
    red flags active.
  - `should_hard_reject` flags truly doomed tickers (e.g. runway < 3 months,
    near-bankruptcy) so the Guardrails layer skips them before scoring.

Both are pure functions — deterministic, no I/O. Data quality (missing fields,
NaN) is treated as "no information" → no penalty.
"""

from __future__ import annotations

import math
from typing import Mapping


def _is_valid(x) -> bool:
    """True iff the value is present and usable (not None, not NaN)."""
    if x is None:
        return False
    try:
        return not math.isnan(float(x))
    except (TypeError, ValueError):
        return False


def _runway_penalty(features: Mapping, config: Mapping) -> float:
    """Linear penalty between hard_reject threshold and penalty_full threshold.

    - runway < hard_reject  → penalty = 1.0 (but caller uses should_hard_reject)
    - runway < penalty_full → linear interp from 1.0 at hard_reject to 0.0 at penalty_full
    - runway ≥ penalty_full → no penalty
    """
    runway = features.get("cash_runway_months")
    if not _is_valid(runway):
        return 0.0
    hard = float(config.get("cash_runway_months_hard_reject", 3))
    full = float(config.get("cash_runway_months_penalty_full", 12))
    r = float(runway)
    if r >= full:
        return 0.0
    if r <= hard:
        return 1.0
    # Linear between hard (penalty 1.0) and full (penalty 0.0)
    return (full - r) / (full - hard)


def _ps_preprofit_penalty(features: Mapping, config: Mapping) -> float:
    """P/S penalty, applied only to pre-profit names.

    Profitable companies routinely trade at high P/S multiples (software cos);
    that's a growth signal, not a red flag. Only flag P/S extremes for
    pre-profit names where valuation relies purely on future revenue.
    """
    net_income = features.get("net_income_ttm")
    ps = features.get("ps_ratio")
    if not _is_valid(net_income) or not _is_valid(ps):
        return 0.0
    if float(net_income) > 0:
        return 0.0
    ceiling = float(config.get("ps_ceiling_preprofit_penalty_full", 100))
    # No penalty below ceiling/2, ramp linearly to full at ceiling, clip at 1.0 above.
    x = float(ps)
    start = ceiling / 2.0
    if x <= start:
        return 0.0
    if x >= ceiling:
        return 1.0
    return (x - start) / (ceiling - start)


def _ocf_penalty(features: Mapping, config: Mapping) -> float:
    """Penalty ramps from 0 to 0.5 once consecutive_neg_ocf_quarters crosses
    the configured threshold. Capped at 0.5 because long OCF streaks aren't
    bankruptcy signals on their own (especially for pre-revenue biotech).
    """
    streak = features.get("consecutive_neg_ocf_quarters")
    if not _is_valid(streak):
        return 0.0
    threshold = int(config.get("consecutive_neg_ocf_penalty", 4))
    s = int(streak)
    if s < threshold:
        return 0.0
    # Ramp from 0 at threshold to 0.5 at threshold+4 quarters.
    excess = s - threshold
    return min(0.5, 0.1 * excess)


def fundamental_gate_score(features: Mapping, config: Mapping) -> float:
    """Compute a score multiplier in [floor, 1.0].

    Penalties are combined additively and clipped at 1.0 total, then subtracted
    from 1.0. Floor prevents full veto so bubble-regime winners can still rank
    on strong technical score.
    """
    if not config.get("fundamental_gate_enabled", True):
        return 1.0

    penalty = (
        _runway_penalty(features, config)
        + _ps_preprofit_penalty(features, config)
        + _ocf_penalty(features, config)
    )
    penalty = min(1.0, max(0.0, penalty))

    floor = float(config.get("fundamental_gate_floor", 0.3))
    score = max(floor, 1.0 - penalty)
    return float(score)


def should_hard_reject(features: Mapping, config: Mapping) -> tuple[bool, str]:
    """Return (True, reason) when a ticker's fundamentals are so dire that even
    a high technical score shouldn't get it to Layer 3.

    Criterion: cash runway below hard_reject threshold (near-bankruptcy). No
    other feature triggers hard-reject — they go through the soft gate.
    """
    if not config.get("fundamental_gate_enabled", True):
        return False, ""

    runway = features.get("cash_runway_months")
    if not _is_valid(runway):
        return False, ""

    hard = float(config.get("cash_runway_months_hard_reject", 3))
    if float(runway) < hard:
        return True, f"runway<{int(hard)}mo"
    return False, ""
