"""Rule-based macro regime scorer for Tactical Sector Rotation (Layer 2e).

The scorer takes a snapshot of signal values (from ``SignalSet.as_of()``) and
evaluates a pre-committed list of ``Rule``s. Each rule that fires contributes
its ``tilt`` dict to the aggregated ``MacroRegime.tilt_sum``. The resulting
``MacroRegime`` is handed to ``OverlayAllocator`` to produce target weights.

LLM-based scorers are deliberately excluded from v1 — un-gittable output breaks
pre-commit discipline (see project memory + 2026-04-24 R12 consultation).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

_OPS = {
    "gt": lambda x, t: x > t,
    "lt": lambda x, t: x < t,
    "ge": lambda x, t: x >= t,
    "le": lambda x, t: x <= t,
}


@dataclass(frozen=True)
class Rule:
    name: str
    signal: str
    operator: str  # "gt" | "lt" | "ge" | "le"
    threshold: float
    tilt: Mapping[str, float]


@dataclass(frozen=True)
class MacroRegime:
    flags: Mapping[str, bool]
    tilt_sum: Mapping[str, float] = field(default_factory=dict)


class RuleBasedScorer:
    def __init__(self, rules: Sequence[Rule]):
        self._rules = tuple(rules)

    def score(self, signals: Mapping[str, float]) -> MacroRegime:
        flags: dict[str, bool] = {}
        tilt_sum: dict[str, float] = {}
        for rule in self._rules:
            fired = self._evaluate(rule, signals)
            flags[rule.name] = fired
            if fired:
                for ticker, delta in rule.tilt.items():
                    tilt_sum[ticker] = tilt_sum.get(ticker, 0.0) + float(delta)
        # Drop zero-sum tilts so equality checks are clean
        tilt_sum = {k: v for k, v in tilt_sum.items() if abs(v) > 1e-12}
        return MacroRegime(flags=flags, tilt_sum=tilt_sum)

    def explain(self, signals: Mapping[str, float]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for rule in self._rules:
            value = signals.get(rule.signal, float("nan"))
            out[rule.name] = {
                "fired": self._evaluate(rule, signals),
                "signal": rule.signal,
                "signal_value": float(value),
                "operator": rule.operator,
                "threshold": rule.threshold,
                "tilt": dict(rule.tilt),
            }
        return out

    def _evaluate(self, rule: Rule, signals: Mapping[str, float]) -> bool:
        value = signals.get(rule.signal, float("nan"))
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False
        if math.isnan(value):
            return False
        op = _OPS.get(rule.operator)
        if op is None:
            raise ValueError(f"unknown operator: {rule.operator}")
        return bool(op(value, float(rule.threshold)))
