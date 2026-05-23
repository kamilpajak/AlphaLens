"""Regime-gated Scorer wrapper.

A regime classifier supplies a per-date verdict about whether the broader
market is in a regime where the strategy should run. Two flavours:

  - **Binary** (``is_on(asof) -> bool``): hard gate; OFF days return an
    empty selection.
  - **Graded** (``score(asof) -> float``): soft gate; underlying scores
    are scaled by the float (clamped to [0, 1]).

A classifier MUST implement at least one of these methods. Construction
raises ``TypeError`` rather than silently passing through, so the
ambiguity is caught at wiring time.

The wrapper infers ``asof`` from the engine-supplied ``histories`` —
each invocation receives PIT-truncated histories whose last index IS
the asof. No engine-side change required.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Protocol, runtime_checkable

import pandas as pd

Scorer = Callable[[Mapping[str, pd.DataFrame], Mapping], pd.DataFrame]


@runtime_checkable
class RegimeClassifier(Protocol):
    """Either binary (``is_on``) or graded (``score``) — never both required."""

    def is_on(self, asof: date) -> bool:  # pragma: no cover
        ...

    def score(self, asof: date) -> float:  # pragma: no cover
        ...


def _clamp_unit(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _infer_asof(histories: Mapping[str, pd.DataFrame]) -> date | None:
    """Return the latest non-empty history's last index as ``date``, or None."""
    for df in histories.values():
        if df is None or df.empty:
            continue
        return pd.Timestamp(df.index[-1]).date()
    return None


def regime_gated_scorer(
    base_scorer: Scorer,
    classifier: object,
) -> Scorer:
    """Return a Scorer-protocol Callable that gates ``base_scorer`` on regime.

    Raises ``TypeError`` if ``classifier`` implements neither ``is_on`` nor
    ``score``. When both are present, ``is_on`` wins (hard gate dominates).
    """
    has_is_on = callable(getattr(classifier, "is_on", None))
    has_score = callable(getattr(classifier, "score", None))
    if not (has_is_on or has_score):
        raise TypeError(
            f"{type(classifier).__name__} must implement either "
            "is_on(asof) -> bool or score(asof) -> float"
        )

    def _gated(histories: Mapping[str, pd.DataFrame], config: Mapping) -> pd.DataFrame:
        asof = _infer_asof(histories)
        if asof is None:
            return pd.DataFrame(columns=["ticker", "score"])

        if has_is_on:
            if not classifier.is_on(asof):  # type: ignore[attr-defined]
                return pd.DataFrame(columns=["ticker", "score"])
            return base_scorer(histories, config)

        # graded path
        weight = _clamp_unit(classifier.score(asof))  # type: ignore[attr-defined]
        result = base_scorer(histories, config)
        if "score" in result.columns and len(result) > 0:
            result = result.copy()
            result["score"] = result["score"] * weight
        return result

    return _gated
