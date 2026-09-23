"""Can a corporate action reach the #1227 estimand? The pre-run split-adjustment gate.

WHY THIS EXISTS
---------------
The #1227 pre-run checklist carried one unrun item: "ATR is built from raw unadjusted
bars while the outcome label uses split-adjusted ones; the label has a ``split_guard``
status and ATR has none." Left unrun, a split inside an episode's window would put a
fabricated return into the one irreversible look.

WHAT THE MEASUREMENT FOUND, AND WHY THE PREMISE WAS WRONG
---------------------------------------------------------
The premise is false, and the real exposure runs the other way.

The bars behind ATR come from ``YFinanceClient.daily_ohlcv``, which asks Yahoo with
``auto_adjust=False``. That does NOT return split-unadjusted bars.
Yahoo's own quote payload is already split-adjusted; ``auto_adjust`` applies the further
``Adj Close / Close`` ratio, which on Yahoo daily data is the dividend and capital-gain
leg. So the ATR frame is ONE fetch, uniformly adjusted, and internally consistent. The
code comment calling these "raw prices" is wrong about the split leg. Note this is a
PROVIDER CONVENTION, not a documented contract, and yfinance ships a ``repair`` feature
precisely for the case where Yahoo fails to adjust - so it is a fact to re-measure, not
a law to rely on.

The label store is the opposite shape. Polygon grouped-daily is fetched with
``adjusted=True`` but written one parquet per session and never re-fetched, so it is a
PATCHWORK: each file is adjusted as of its own fetch date. A split therefore leaves
exactly one unadjusted step, and only for splits after the bulk seed, since the seeded
files all share one adjustment epoch.

WHAT THIS MODULE IS FOR
-----------------------
Three discriminations, each written against the way it quietly fails:

1. **Is a step a corporate action?** Answering from a vendor's split list inherits that
   vendor's completeness. :func:`classify_step` answers from the DATA instead: a step
   present in the patchwork store and ABSENT from a uniformly adjusted source is an
   adjustment artefact; a step both sources agree on is a market move. This is the only
   arm that survives a missing split record.
2. **Does a split reach ATR?** Answering from the final ATR number has almost no power.
   :func:`wilder_weight` says why: at alpha = 1/14 a bar 190 sessions back carries 7.5e-7
   of the weight, so a split-day spike of 300x median decays below noise. The question
   has to be asked on :func:`true_range_pct` of the split bar itself.
3. **Does the guard see it?** :func:`guard_sees` is arithmetic over the bounds
   ``selection_label`` uses, and :func:`blind_band` names the hole in them.

The band hole is real and is NOT closed here: a 3-for-2 leaves a step of 0.667, inside
``(0.55, 1.8)``, so neither the label status nor the beta-window drop fires. Published
US samples put 3-for-2 at roughly a fifth of splits. On the measured panel the band is
empty, which is luck rather than design.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import numpy as np
from alphalens_pipeline.events.insider_cluster import SPLIT_RATIO_HI, SPLIT_RATIO_LO

#: Wilder's ATR smoothing, matching ``technicals_signal._ATR_PERIOD``.
ATR_PERIOD = 14

#: Step templates the guard cannot see, keyed by the split they would come from.
#: Values are the CLOSE STEP, not the share ratio.
SMALL_SPLIT_TEMPLATES = {
    "3-for-2": 2.0 / 3.0,
    "4-for-3": 3.0 / 4.0,
    "5-for-4": 4.0 / 5.0,
    "1-for-1.25": 1.25,
    "1-for-1.333": 4.0 / 3.0,
    "1-for-1.5": 1.5,
}

#: Deliberately loose. The screen must not MISS a split, so it also catches ordinary
#: earnings moves; every hit goes to :func:`classify_step` before it means anything.
TEMPLATE_TOLERANCE = 0.02

#: Two price sources disagreeing by less than this are telling the same story.
SOURCE_AGREEMENT = 0.03

MARKET = "market_move"
ARTEFACT = "adjustment_artefact"
UNKNOWN = "unchecked"


def guard_sees(split_ratio: float) -> bool:
    """Would ``selection_label._out_of_bounds`` fire on the step this split leaves?

    ``split_ratio`` is the share multiplier: 2.0 for a 2-for-1, 0.1 for a 1-for-10
    reverse. A non-positive ratio is not a split and is reported as unseen rather than
    raising, because it reaches here only from a malformed vendor record.
    """
    if split_ratio <= 0:
        return False
    step = 1.0 / split_ratio
    return step < SPLIT_RATIO_LO or step > SPLIT_RATIO_HI


def blind_band() -> tuple[float, float]:
    """The open interval of split ratios the guard cannot see, as ``(lo, hi)``."""
    return 1.0 / SPLIT_RATIO_HI, 1.0 / SPLIT_RATIO_LO


def two_for_one_headroom_pct() -> float:
    """How far a 2-for-1 split day can rally before the guard stops seeing it.

    A plain 2-for-1 leaves a step of 0.5 against a 0.55 bound. That is the smallest
    margin in the whole scheme and it applies to the most common split there is.
    """
    return 100.0 * (SPLIT_RATIO_LO / 0.5 - 1.0)


def expected_raw_true_range_pct(split_ratio: float) -> float:
    """True range a SPLIT-UNADJUSTED bar would show, as a percent of its own close.

    With share multiplier ``r`` the price steps by ``1/r``, so the bar's range is
    ``|P/r - P|`` against a close of ``P/r``, and the ratio reduces to ``100 * |1 - r|``.
    It is strongly asymmetric: a 4-for-1 forward split reads about 300% because the
    denominator shrinks, while a 1-for-10 reverse reads about 90%.

    This is the number the ATR arm is tested against. Quoting one multiple for every
    split, as an earlier draft of this work did, overstates the reverse cases by an
    order of magnitude and understates nothing - so the direction has to be carried.
    """
    if split_ratio <= 0:
        raise ValueError(f"a split ratio must be positive, got {split_ratio!r}")
    return 100.0 * abs(1.0 - split_ratio)


def wilder_weight(lag_sessions: int) -> float:
    """Weight Wilder's ATR still puts on a bar ``lag_sessions`` before the last one."""
    if lag_sessions < 0:
        raise ValueError(f"lag must not be negative, got {lag_sessions!r}")
    alpha = 1.0 / ATR_PERIOD
    return alpha * (1.0 - alpha) ** lag_sessions


def true_range_pct(*, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """Wilder's true range as a percent of the bar's own close, one value per bar but the first.

    This is the quantity ATR smooths, and the only one that can show a split artefact:
    a close series can look smooth while ``|high - prev_close|`` does not.
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    if len(close) < 2:
        return np.empty(0, dtype=float)
    prev = close[:-1]
    tr = np.maximum.reduce([high[1:] - low[1:], np.abs(high[1:] - prev), np.abs(low[1:] - prev)])
    here = close[1:]
    out = np.divide(100.0 * tr, here, out=np.full(len(here), np.nan, dtype=float), where=here > 0)
    return out[np.isfinite(out)]


def classify_step(*, store_step: float, reference_step: float | None) -> str:
    """Is a step in the patchwork store a market move, an artefact, or unchecked?

    A split that the store failed to adjust appears ONLY in the store: the reference
    source re-adjusts its whole series at fetch time, so the same two dates come back
    continuous there. A real move appears in both.

    A missing reference returns :data:`UNKNOWN`. It must not collapse into "market
    move", because that would turn "could not check" into "checked and clean".
    """
    if reference_step is None or not math.isfinite(reference_step):
        return UNKNOWN
    if not math.isfinite(store_step):
        return UNKNOWN
    return MARKET if abs(store_step - reference_step) < SOURCE_AGREEMENT else ARTEFACT


def matching_template(step: float) -> str | None:
    """The small split whose step this matches, or ``None``. A screen, not a verdict."""
    for name, target in SMALL_SPLIT_TEMPLATES.items():
        if abs(step - target) <= TEMPLATE_TOLERANCE:
            return name
    return None


def touches_window(
    split_date: dt.date, window_start: dt.date, window_end: dt.date, *, tolerance_days: int
) -> bool:
    """Does a split fall in ``[start, end]``, allowing for the ex-date offset?

    The store's discontinuity can precede the vendor's ex-date: the session file written
    the morning after the split already carries the new scale while the previous file
    does not. Matching on the ex-date alone therefore has a one-session blind spot at
    each window edge, which the tolerance closes.
    """
    if tolerance_days < 0:
        raise ValueError(f"tolerance must not be negative, got {tolerance_days!r}")
    slack = dt.timedelta(days=tolerance_days)
    return window_start - slack <= split_date <= window_end + slack


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reason: str


def verdict(*, label_window_artefacts: int, unknown_steps: int) -> Verdict:
    """The gate's answer. An unchecked step fails, exactly like a confirmed one.

    The asymmetry is deliberate. This gate guards a single irreversible look, so the
    cost of passing on an unexamined step is not symmetric with the cost of examining it.
    """
    if label_window_artefacts:
        return Verdict(
            False,
            f"{label_window_artefacts} adjustment artefact(s) inside a label window; "
            "those episodes carry a fabricated return",
        )
    if unknown_steps:
        return Verdict(
            False,
            f"{unknown_steps} step(s) could not be checked against a second source",
        )
    return Verdict(True, "no adjustment artefact reaches a label window")
