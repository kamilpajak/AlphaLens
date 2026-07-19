"""Constructive hypothesis strategies for the AlphaLens core.

Structural constraints — entries strictly descending above the stop, TPs strictly
ascending above the top entry, OHLC ordering (``l <= o,c <= h``), strictly
increasing bar timestamps — are built into GENERATION, never enforced with
``assume()`` / ``.filter()``. Filter-heavy generation causes rejection storms and
biases coverage to a thin slice of the space (the standard PBT anti-pattern).

Emitted dicts match the shapes ``parse_ladder`` / ``_bar_lhc`` consume in
``alphalens_pipeline.feedback.ladder_replay`` (same keys as the ``_setup`` /
``_bar`` helpers in ``test_feedback_ladder_replay.py``).
"""

from __future__ import annotations

from typing import Any

from hypothesis import strategies as st


def finite_prices(min_value: float = 1e-2, max_value: float = 1e5) -> st.SearchStrategy[float]:
    """Positive, finite, bounded floats — no NaN / inf / subnormals."""
    return st.floats(
        min_value=min_value,
        max_value=max_value,
        allow_nan=False,
        allow_infinity=False,
        allow_subnormal=False,
    )


def _ascending_above(
    draw: Any, floor: float, n: int, min_gap: float, max_gap: float
) -> list[float]:
    """``n`` strictly ascending prices, each a positive gap above the previous."""
    out: list[float] = []
    cur = floor
    for _ in range(n):
        cur = cur + draw(finite_prices(min_gap, max_gap))
        out.append(cur)
    return out


@st.composite
def ladders(
    draw: Any,
    *,
    min_entries: int = 1,
    max_entries: int = 3,
    min_tps: int = 1,
    max_tps: int = 3,
) -> dict[str, Any]:
    """A valid ``brief_trade_setup`` dict: stop < entries (descending) < TPs (ascending)."""
    stop = draw(finite_prices(1.0, 1e4))
    n_entries = draw(st.integers(min_entries, max_entries))
    # Entries built ascending above the stop, then reversed so E1 is the highest.
    ascending_entries = _ascending_above(draw, stop, n_entries, min_gap=0.5, max_gap=100.0)
    entries = sorted(ascending_entries, reverse=True)

    n_tps = draw(st.integers(min_tps, max_tps))
    tps = _ascending_above(draw, entries[0], n_tps, min_gap=0.5, max_gap=200.0)

    entry_w = draw(st.lists(st.integers(1, 100), min_size=n_entries, max_size=n_entries))
    tp_w = draw(st.lists(st.integers(1, 100), min_size=n_tps, max_size=n_tps))
    return {
        "status": "OK",
        "disaster_stop": stop,
        "entry_tiers": [
            {"limit": p, "alloc_pct": float(w)} for p, w in zip(entries, entry_w, strict=True)
        ],
        "tp_tranches": [
            {"target": p, "tranche_pct": float(w)} for p, w in zip(tps, tp_w, strict=True)
        ],
    }


@st.composite
def bar_paths(
    draw: Any,
    *,
    min_bars: int = 1,
    max_bars: int = 8,
    price_lo: float = 0.5,
    price_hi: float = 1e5,
) -> list[dict[str, Any]]:
    """OHLC bars with ``l <= o,c <= h`` and strictly increasing ``t``."""
    n = draw(st.integers(min_bars, max_bars))
    bars: list[dict[str, Any]] = []
    t = draw(st.integers(1, 1000))
    for _ in range(n):
        a = draw(finite_prices(price_lo, price_hi))
        b = draw(finite_prices(price_lo, price_hi))
        low, high = min(a, b), max(a, b)
        o = draw(finite_prices(low, high))
        c = draw(finite_prices(low, high))
        bars.append({"t": t, "l": low, "h": high, "c": c, "o": o})
        t += draw(st.integers(1, 100))
    return bars


@st.composite
def ladder_and_bars(draw: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A ladder plus a bar path spanning its [stop, top-TP] region.

    Spanning the ladder's own price range makes fills / TP-touches / SL-hits
    FREQUENT (independent price generation would almost always miss the ladder and
    only ever exercise NO_FILL).
    """
    setup = draw(ladders())
    stop = setup["disaster_stop"]
    top_tp = max(t["target"] for t in setup["tp_tranches"])
    bars = draw(bar_paths(price_lo=stop * 0.85, price_hi=top_tp * 1.15))
    return setup, bars
