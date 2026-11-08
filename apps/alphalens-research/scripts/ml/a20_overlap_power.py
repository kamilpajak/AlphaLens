"""Size before power: does the #1227 ATR gate survive the 20-session overlap?

WHY THIS EXISTS
---------------
``a20_power.py`` answered the gate question and its number is merged: ATR power
at 50% shrinkage on the briefed population is 83.9%. That simulator draws ONE
INDEPENDENT random effect per arrival session and then computes its p-value with
a wild cluster bootstrap clustered on arrival session. It generates and tests
under the same assumption, so it is internally consistent and **cannot detect
that the assumption is wrong**.

The real panel is not built that way. ``sel_ar_20`` accumulates 20 trading
sessions from the arrival open, and arrival clusters land about one per session,
so two adjacent clusters share up to 19 of their 20 outcome sessions. #1227's own
body anticipated this — "session clusters alone are not sufficient" — and said it
when the horizon was ``car_10``, half as long.

An earlier attempt to clear the concern measured the autocorrelation of
SESSION-MEAN RESIDUALS and the CR1 standard error under coarser blocks. Both
adversarial reviewers rejected that, correctly: the quantity that transmits into
a coefficient is the autocovariance of the score contributions ``X_t'u_t``, not
of mean residuals, and a CR1 standard error that FALLS at 13 / 5 / 3 groups is
more likely small-G instability than evidence of independence. That
justification is withdrawn and this module replaces it.

WHAT IT MEASURES
----------------
SIZE FIRST. Under the null, the rejection rate of each candidate inference
method on an overlap-aware panel. This is the number the merged preflight never
produced, and it is the one that decides: if the arrival-session bootstrap
rejects well above 5% here, then 83.9% was never a power figure.

Then power, for the same methods, so the cost of an honest method is visible
rather than argued.

Direction of the risk: understated standard errors inflate ``|t|`` and make
rejection EASIER. For a one-sided test whose rejection means "ATR is real", the
exposure is CONFIRMING a penalty that does not work — and keeping it for good,
because ledger rule 4 ends testing either way.

THE DGP
-------
Only the cross-cluster structure changes. The marginals are pinned to the same
two burnt-panel quantities the merged preflight already measures, ``sd_y`` and
``icc``, so nothing here is a new calibration:

- the residual variance splits into a cluster component ``C = icc * var_resid``
  and an idiosyncratic one ``I = (1 - icc) * var_resid``, exactly as before;
- ``C`` then splits again by ``shared_fraction`` (phi) into a part carried by
  ``horizon`` daily draws, which adjacent windows genuinely share, and a
  cluster-local part that does not travel.

Consequences, all closed-form and all asserted in the tests, WITH FEATURE-ALIGNED
NOISE OFF (the default, and the DGP behind every published figure here):

- within-cluster correlation is ``icc`` for every phi — the calibration is
  untouched;
- correlation at a lag of ``k`` sessions is ``phi * icc * (horizon - k) / horizon``,
  and zero at ``k >= horizon``;
- **phi = 0 reproduces the merged preflight exactly**, which is what makes this a
  measurement of the assumption rather than a replacement of it.

With alignment ON there is NO closed form, and that is stated rather than
papered over. At the SESSION level the within-cluster correlation is still
exactly ``icc``, because the scale is constant inside a cluster and factors out
of numerator and denominator alike. Everything else is a MEASURED DIRECTION:
the ROW level attenuates both forms, and the SESSION level attenuates the
cross-cluster form, with the sizes measured in
``TestTheClosedFormsUnderAlignment`` rather than predicted.

This module did publish a closed form for the ROW level until 2026-11-08,
``E[exp(gam*x)]**2 / E[exp(2*gam*x)]`` on the feature column, justified by "the
panel-wide normaliser cancels". It does not cancel: ``_alignment_scale`` divides
by ``c = sqrt(mean(s**2))`` computed on the panel's OWN rows, so ``c`` is random
and correlated with the very rows whose covariance the form describes. Measured
2026-11-08 over 40 000 panels at panel seed 5, gam 0.6, eight clusters of two
rows, icc 0.4, phi 0, on the SEEDED column draw the suite's own fixture uses
(``default_rng(31)``, a 400-point draw, z-scored), the form understated the
simulated within-cluster correlation by 1.04x on a gaussian column, 1.43x on a
lognormal one at sigma 0.2, 5.06x at sigma 0.4 and 38.65x at sigma 0.6 — and the
same four columns on a differently SHAPED panel, one cluster of two rows, read
1.13x / 1.66x / 6.18x / 48.78x, so the quantity is not a function of the column
at all. The simulated side carries a Monte Carlo error of 0.0051 at that panel
count, about plus or minus 0.8x on the 38.65x. The error is also a function of
the DRAW and not only of the shape: over 200 independent sigma 0.6 draws, seeds
0..199, 4 000 panels each, the ratio spans 1.01x to 132.60x with a median of
2.37x, and seed 31 reads 36.61x there, ranking 195th of those 200 - the 97.5th
percentile, so the headline is a bad draw and not a central estimate. The
form is deleted; ``TestThereIsNoClosedFormForTheRowLevel`` carries every table
with its seeds, and derives these four ratios from the recipe rather than
copying them.

phi = 1 attributes ALL same-session correlation to the shared window. That is the
conservative end: any part of it that is genuinely arrival-specific (one
catalyst, one theme, one day's news) does not spill into the neighbouring
cluster. The truth is somewhere in between, so the grid is reported rather than
one value being chosen.

TWO MECHANISMS, NOT TWO OPTIONS
-------------------------------
kappa and gam COMPOSE. kappa is a LINEAR reweighting of each episode's SHARE of
the shared shock, renormalised by the exact ``sqrt(1 + kappa**2)``: it moves the
dependence, not the marginal variance. gam is an EXPONENTIAL rescaling of the
whole residual's SCALE at one of two levels, held by an EMPIRICAL panel-wide
normaliser. The published grid runs a fitted-kappa arm, so the two will be read
as substitutes unless it is written down that they are not: a
``kappa=fitted`` x aligned cell applies the feature-dependence TWICE through two
link functions, and the printed table and the artifact carry that sentence
beside those cells.

The p-value is the same two-sided wild cluster bootstrap the merged preflight
used, at the same bar, so the numbers here and there are comparable. The
registered test is one-sided; that difference is a level shift applied equally
to every row and does not affect which method wins.
"""

from __future__ import annotations

import datetime as dt
import math
import sys
from typing import Any

import numpy as np
from scripts.ml.a20_power import (
    ALIGNMENT_FEATURE,
    ALIGNMENT_MEMO,
    ALIGNMENT_ROW,
    ALIGNMENT_SESSION,
    BRACKET_ARMS,
    FWER,
    GAM_AT_REALIZED_SE_ROW,
    GAM_AT_REALIZED_SE_SESSION,
    GATE_POWER,
    GATE_SHRINKAGE,
    OUTCOME,
    POPULATION_ALL,
    POPULATION_BRIEFED,
    PRECISION_DISCLOSURE_KEY,
    SIGNALS,
    SIMULATOR_OPTIMISM_RATIO,
    NoiseAlignment,
    PowerBracket,
    _alignment_scale,
    _z,
    burnt_panel,
    estimate_icc_local,
    held_out_episodes_by_arrival,
    load_held_out,
    monte_carlo_se,
    precision_disclosure,
    residual_variance,
    sessions_between,
    shrink,
    standardised_effects,
    verdict_is_resolved,
    wilson_interval,
)

#: An ``sel_ar_20`` episode accumulates this many sessions from the arrival open.
HORIZON_SESSIONS = 20

#: Reported rather than chosen. 0.0 is the merged preflight's assumption, 1.0 is
#: the conservative end where every same-session correlation travels.
SHARED_FRACTION_GRID = (0.0, 0.5, 1.0)

#: Candidate inference methods. ``arrival`` is the status quo; the blocks widen
#: the cluster until it can absorb the shared window.
ARRIVAL_METHOD = "arrival"
BLOCK_PREFIX = "block"
METHODS = (ARRIVAL_METHOD, f"{BLOCK_PREFIX}5", f"{BLOCK_PREFIX}10", f"{BLOCK_PREFIX}20")

#: The optimistic corner of :data:`BRACKET_ARMS` — the DGP that produced every
#: published figure in this memo — and the two the burnt panel does not choose
#: between. Derived from the sibling's tuple rather than re-listed, so one
#: ordering serves both modules.
HOMOSKEDASTIC_ARM = BRACKET_ARMS[0]
ALIGNED_ARMS = tuple(arm for arm in BRACKET_ARMS if arm != HOMOSKEDASTIC_ARM)

#: Half-width multiplier for the printed Monte Carlo error, matching the 95%
#: Wilson interval beside it.
MC_HALF_WIDTH_Z = 1.96

#: What this module leaves on the homoskedastic corner. Passed to the sibling's
#: :func:`precision_disclosure`, because its own default names a cluster-count
#: search, a gate date and an ``--alignment-bracket`` flag, none of which exist
#: here — writing that sentence into this artifact would be a false claim.
PROJECTIONS_NOTE = (
    "only the per-arm sizes and powers are bracketed here; the icc, sd(y) and "
    "kappa this panel is calibrated to are read on the burnt panel under the "
    "homoskedastic assumption, and --alignment-size is what runs the aligned "
    "corners at all"
)

#: Carried on BOTH surfaces beside the aligned table. kappa and gam are not
#: alternatives and their magnitudes do not add; the published grid already runs
#: a fitted-kappa arm, so without this sentence the cells where the two meet
#: would be read as two separable mechanisms.
COMPOSED_MECHANISMS_NOTE = (
    "the kappa=fitted x aligned corners apply the feature-dependence TWICE, through a linear "
    "loading of the shared shock and an exponential rescaling of the whole residual. They are "
    "not separable there and the two magnitudes do not add."
)

#: Printed on a run WITHOUT --alignment-size. Without it a default run reads as a
#: measurement of the claim the aligned corners exist to test.
DEFAULT_RUN_NOTICE = (
    "the aligned arms were NOT run: every size and power above is the "
    f"HOMOSKEDASTIC corner only, the one measured {SIMULATOR_OPTIMISM_RATIO:.2f}x optimistic on "
    "the slope standard error. Re-run with --alignment-size for the three-arm "
    f"size study ({ALIGNMENT_MEMO})"
)

#: The two loading arms, NAMED so the verdict can tell a single-mechanism cell
#: from a composed one. :func:`report` writes these strings into every row's
#: ``loading`` and :func:`size_verdict` reads them back; a literal in either
#: place would let the two drift, and the distinction is the whole content of the
#: second reading: a refutation arriving from a COMPOSED cell is a different
#: claim from one arriving from a single-mechanism cell.
SINGLE_MECHANISM_LOADING = "kappa=0"
COMPOSED_LOADING = "kappa=fitted"

#: Which branch of :func:`one_size_reading` set ``claim_survives`` to ``False``.
#: Two branches can, and they measure DIFFERENT cells: the crossing branch reads
#: cells that cross the bar only under alignment, the ordering branch runs only
#: when nothing crossed and reads cells whose HOLDING status differs between
#: corners. The verdict sentence is built off the branch so it can only name a
#: quantity that branch measured.
REFUTING_BRANCH_CROSSING = "crossing"
REFUTING_BRANCH_ORDERING = "ordering"

#: Branch -> the key of :func:`one_size_reading`'s result that holds the cells
#: that branch refuted from.
_DECIDING_CELLS = {
    REFUTING_BRANCH_CROSSING: "crosses_only_under_alignment",
    REFUTING_BRANCH_ORDERING: "holding_differs",
}

#: Branch -> how the verdict sentence NAMES one of those cells.
_REFUTING_CELL_NOUN = {
    REFUTING_BRANCH_CROSSING: "crossing cell",
    REFUTING_BRANCH_ORDERING: "cell whose holding status differs between corners",
}

#: Printed in place of a holding set or an ordering answer on the NO VERDICT
#: branch, where standing alone they read as measurements and are vacuous: an
#: unresolved method is DROPPED from the holding set, so "none" covers both an
#: empty set and a set nobody could resolve.
NOT_RESOLVED_LABEL = "(not resolved)"

#: Printed beside an empty holding set on the branch where no method holds the
#: bar at ANY corner. "none" there is a MEASUREMENT, the opposite of
#: :data:`NOT_RESOLVED_LABEL`, and the two must not read alike: the gate used to
#: be ``claim_survives is None``, which is true on both NO VERDICT branches, so
#: the screen reported "we could not tell" about a measurement it completed at
#: every one of the nine corners.
MEASURED_EMPTY_LABEL = "none (measured at every corner)"

#: Why :func:`one_size_reading` returned ``claim_survives: None``. Two different
#: readings, carried as a FIELD rather than inferred from the shape of the rest:
#: on ``NO_VERDICT_UNRESOLVED`` the instrument could not tell, and the holding
#: sets and the ordering answer are withheld on both surfaces; on
#: ``NO_VERDICT_NOTHING_HOLDS`` every corner was resolved and the answer is that
#: no method wins, so those readings are real and are published.
NO_VERDICT_UNRESOLVED = "unresolved"
NO_VERDICT_NOTHING_HOLDS = "nothing_holds"

#: A method whose size exceeds this is not reported with a power figure at all.
#: Bradley's liberal robustness criterion for a nominal 0.05 test is [0.025,
#: 0.075]; the upper end is the one that matters, because an over-rejecting
#: method is the one that would hand back a confident wrong confirmation.
#: Pre-specified here rather than chosen once the numbers are on screen.
SIZE_TOLERANCE = 0.075

#: A mean absolute residual at or below this counts as no residual spread at
#: all. The outcome is standardised before the fit, so its own scale is 1.
NO_SPREAD_LEFT = 1e-9


def variance_split(
    *, var_resid: float, icc: float, shared_fraction: float, horizon: int
) -> tuple[float, float, float]:
    """``(per-day shared, cluster-local, idiosyncratic)`` variances.

    They reconstruct ``var_resid`` as ``horizon * per_day + local + idio``, which
    is what keeps the total spread and the ICC identical across the phi grid.
    """
    if not 0.0 <= shared_fraction <= 1.0:
        raise ValueError(f"shared_fraction must lie in [0, 1], got {shared_fraction!r}")
    if horizon < 1:
        raise ValueError(f"horizon must be at least 1 session, got {horizon!r}")
    if not 0.0 <= icc <= 1.0:
        raise ValueError(f"icc must lie in [0, 1], got {icc!r}")
    common = icc * var_resid
    idio = (1.0 - icc) * var_resid
    return shared_fraction * common / horizon, (1.0 - shared_fraction) * common, idio


def simulate_overlapping_panel(
    *,
    burnt_signals: np.ndarray,
    arrival_offsets: list[int],
    cluster_sizes: list[int],
    effects: dict[str, float],
    sd_y: float,
    icc: float,
    shared_fraction: float,
    horizon: int,
    rng: np.random.Generator,
    signal_loading: float = 0.0,
    loading_feature: str = ALIGNMENT_FEATURE,
    alignment: NoiseAlignment | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One simulated panel whose outcome windows really do overlap.

    ``arrival_offsets`` are CALENDAR session indices, not positions in a list, so
    a gap in the arrival calendar produces a real gap in the sharing. Reading
    them as list positions would manufacture overlap between two arrivals a month
    apart, which is the mistake this signature exists to prevent.

    Signal rows are resampled whole from the burnt panel, as in the merged
    preflight, so the cross-signal correlation stays the one the data has.

    ``signal_loading`` (kappa) is the part that decides whether the overlap
    matters at all. With kappa = 0 the shared shock is a pure per-date LEVEL
    SHIFT, identical for every name arriving that date — and a level shift is
    orthogonal to a mean-zero regressor, so it never reaches the coefficient no
    matter how much the windows overlap. Measured here: the score autocorrelation
    stays on the -1/(G-1) small-sample line for every sharing fraction.

    That is only true while the shock hits every name equally. ATR measures
    volatility, so high-ATR names plausibly take MORE of a common move; kappa
    scales each episode's share of the shared shock by ``1 + kappa * z(atr)``.
    That is the one channel through which overlapping windows can bias the ATR
    coefficient, and it is why kappa is estimated from the burnt panel rather
    than assumed to be zero. The draw is renormalised by ``sqrt(1 + kappa**2)``
    so the total variance stays the calibrated one and kappa moves only the
    dependence structure.

    That renormalisation is EXACT, not approximate, and it borrows its exactness
    from ``_z``: because ``_z`` divides by the POPULATION standard deviation, a
    signal column has mean 0 and ``E[x**2] == 1`` to the last bit, so a row
    resampled from it satisfies ``E[(1 + kappa*x)**2] == 1 + kappa**2``. Verified
    at 50,000 independent panels: the population spread sits within one standard
    error of ``sd_y`` at kappa 0, 0.5 and 1.0. Switching ``_z`` to a sample
    standard deviation would quietly put ``E[x**2]`` at ``n/(n-1)`` and let the
    calibrated variance drift with kappa.

    ``loading_feature`` names the column kappa loads onto. It used to be
    ``x[:, 0]`` positionally, which agreed with ``alignment`` only while the
    tested feature happened to be the first effect. It is validated only when
    ``signal_loading`` is non-zero: with no loading there is no column to
    resolve, and zero-kappa fixtures whose effects lack ``atr`` must keep working.

    ``alignment`` is OFF by default and the off path is BIT-IDENTICAL to the
    version that produced every published figure in this memo — same draws, same
    order, same operand order in the sum. The mechanism is a deterministic
    rescaling of noise ALREADY DRAWN, so it consumes zero extra randomness
    whether on or off; ``TestTheDefaultNumericPathDoesNotMove`` pins the
    generator state as well as two consecutive panel digests, because a single
    panel from a fresh generator cannot see a trailing extra draw.

    The mechanism is :func:`a20_power._alignment_scale`, IMPORTED rather than
    re-implemented, and composed the sibling's own way: one scale for the WHOLE
    residual as the ROW sees it, ``y = signal + (shared + local + e) * s``. That
    is what makes the ``shared_fraction = 0`` aligned path bit-identical to the
    sibling's aligned path, which is the only standing guard against the two
    copies of this DGP drifting apart. Scaling just ``local + e`` would apply
    less than the magnitude the memo measured on the WHOLE residual, and would
    land on the OPTIMISTIC side of the bound.

    The shared window is scaled **as the row sees it, not at the day it was
    drawn**: ``_alignment_scale`` hands back a per-row scale derived from that
    row's own cluster block. A DAY-level mechanism — the daily innovation's own
    variance depending on who arrives that day — is not expressible by that
    signature and is not measured at any level in the memo. That is an
    unmeasured invention avoided, not an omission.

    kappa and gam COMPOSE; they are not alternatives. kappa is a LINEAR
    reweighting of each episode's SHARE of the shared shock, renormalised by the
    exact ``sqrt(1 + kappa**2)``, and it moves dependence rather than the
    marginal variance. gam is an EXPONENTIAL rescaling of the whole residual's
    SCALE at one of two levels, held by an EMPIRICAL panel-wide normaliser.
    Running both applies the feature-dependence twice through two link
    functions, so the ``kappa=fitted`` x aligned corners of the grid must not be
    read as two separable mechanisms.
    """
    names = list(effects)
    if min(arrival_offsets) < 0:
        # A negative offset clips or wraps the numpy slice below and corrupts
        # both closed forms SILENTLY rather than raising.
        #
        # Re-measured 2026-11-08 with the construction named, because the two
        # earlier copies of this reading disagreed (+0.262 here, 0.29 in the
        # test) and neither named a panel seed, so neither reproduced. One
        # reading now, published once: `burnt_signals=np.zeros((8, 1))`,
        # `cluster_sizes=[2, 2]`, icc 0.5, phi 1.0, horizon 20, 1 500 panels per
        # generator seed, over generator seeds 5..14, correlating the first row
        # of cluster 0 with the first row of cluster 1.
        #
        # At offsets [-1, 0] the first cluster's spread falls from 1.00 (sd
        # 0.017 across those seeds) to 0.71 (sd 0.014) - it loses its whole
        # shared component, which at icc 0.5 is sqrt(0.5) of the variance. At
        # [-30, 0] two clusters 30 sessions apart come back correlated at +0.29
        # (sd 0.024) where the closed form says 0.000; the control [0, 30] reads
        # -0.01 (sd 0.020). Two decimals, not three: at 1 500 panels a
        # correlation near 0.3 has a Monte Carlo standard deviation of about
        # 0.024, so the third digit is noise and the earlier +0.262 and 0.29 are
        # the same number read twice.
        #
        # The size experiment builds offset grids, so a plausible wrong number
        # is the real risk here, not an exception.
        raise ValueError(
            f"arrival_offsets must be non-negative calendar session indices, got "
            f"{min(arrival_offsets)!r}; shift the calendar so the first arrival is 0 "
            f"(arrival_offsets_from_dates does that)"
        )
    loading_index = 0
    if signal_loading and loading_feature not in names:
        raise ValueError(
            f"loading_feature {loading_feature!r} is not among the simulated effects {names}; "
            f"kappa loads the shared shock onto a NAMED column, so that name must exist"
        )
    if signal_loading:
        loading_index = names.index(loading_feature)
    var_resid = residual_variance(burnt_signals, effects, sd_y)
    var_daily, var_local, var_idio = variance_split(
        var_resid=var_resid, icc=icc, shared_fraction=shared_fraction, horizon=horizon
    )

    span = int(max(arrival_offsets)) + horizon
    daily = (
        rng.normal(0.0, math.sqrt(var_daily), span)
        if var_daily > 0
        else np.zeros(span, dtype=float)
    )

    # Two passes, because `_alignment_scale` needs the WHOLE panel: the session
    # arm z-scores the per-cluster feature means ACROSS clusters, and the
    # normaliser is a mean of squares over EVERY row. The first pass draws and
    # accumulates, the second composes.
    xs, sigs, shareds, locs, es, cls = [], [], [], [], [], []
    for j, (offset, m) in enumerate(zip(arrival_offsets, cluster_sizes, strict=True)):
        idx = rng.integers(0, len(burnt_signals), m)
        x = burnt_signals[idx]
        shared = float(daily[int(offset) : int(offset) + horizon].sum())
        if signal_loading:
            loading = (1.0 + signal_loading * x[:, loading_index]) / math.sqrt(
                1.0 + signal_loading**2
            )
            shared_term = shared * loading
        else:
            shared_term = np.full(m, shared, dtype=float)
        # Both guards stay. Removing them to match the sibling (which draws
        # unconditionally) would add one draw per cluster whenever `phi = 1`,
        # where `var_local == 0` - and `phi = 1` is two published rows. The cost
        # is that the phi = 0 equivalence holds for `0 < icc < 1` only, which
        # `TestThePhiZeroEquivalence` measures rather than leaving implied.
        local = rng.normal(0.0, math.sqrt(var_local)) if var_local > 0 else 0.0
        e = rng.normal(0.0, math.sqrt(var_idio), m) if var_idio > 0 else np.zeros(m, dtype=float)
        # A left fold, not `sum()`: from Python 3.12 `sum()` is not
        # left-fold-equivalent, and the bit-identical pin against the sibling
        # passes only while the two folds match.
        signal = np.zeros(m, dtype=float)
        for i, name in enumerate(names):
            signal = signal + effects[name] * x[:, i]
        sigs.append(signal)
        shareds.append(shared_term)
        locs.append(local)
        es.append(e)
        xs.append(x)
        cls.append(np.full(m, j))

    if alignment is None:
        # `((sig + sh) + lo) + e`, exactly the operand order that produced every
        # published figure. Float addition is not associative and reordering
        # moves the last bits.
        ys = [sig + sh + lo + e for sig, sh, lo, e in zip(sigs, shareds, locs, es, strict=True)]
    else:
        scale = _alignment_scale(xs, alignment=alignment, names=names)
        ys = [
            sig + (sh + lo + e) * s
            for sig, sh, lo, e, s in zip(sigs, shareds, locs, es, scale, strict=True)
        ]

    y = np.concatenate(ys)
    x_all = np.column_stack([np.ones(len(y)), np.vstack(xs)])
    return y, x_all, np.concatenate(cls).astype(str)


def estimate_signal_loading(panel: Any, *, signal: str = "atr") -> float:
    """How much harder a common move hits a high-``signal`` name, from the burnt panel.

    Fits ``|residual| = a + b * z(signal)`` and returns ``b / a``, which is the
    ``kappa`` the simulator uses: the proportional widening of an episode's
    response per one standard deviation of the signal. A burnt-panel read, so no
    ledger charge — the same footing as every other calibration quantity here.

    Why the ratio is the right estimator, since the model is about a standard
    deviation and the fit is on a mean absolute residual: if the residual has a
    fixed shape and a scale ``sigma(z) = sigma_0 * (1 + kappa*z)``, then
    ``E|e| = c * sigma_0 * (1 + kappa*z)`` for a shape constant ``c`` (``c =
    sqrt(2/pi)`` under normality). So the fit returns ``a = c*sigma_0`` and
    ``b = c*sigma_0*kappa``, and ``c`` and ``sigma_0`` both cancel in ``b / a``.
    The estimate is free of the normality assumption as long as the SHAPE does
    not itself change with the signal.

    It reads the widening of the WHOLE residual, while the simulator applies
    kappa only to the SHARED component. If the idiosyncratic part also widens
    with the signal — very likely, since a high-ATR name is noisier in every
    direction — this overstates the loading on the shared part. That direction
    simulates more cross-cluster dependence than the panel has, which is the
    safe way round for a gate.

    Clamped below at 0: a NEGATIVE loading would mean high-ATR names react LESS
    to a common move, which is not a direction worth simulating, and letting it
    through would make the overlap look harmless for the wrong reason.
    """
    y = _z(panel[OUTCOME].astype(float).to_numpy())
    cols = [_z(panel[c].astype(float).to_numpy()) for c in SIGNALS.values()]
    x_all = np.column_stack([np.ones(len(y)), *cols])
    beta, *_ = np.linalg.lstsq(x_all, y, rcond=None)
    resid = np.abs(y - x_all @ beta)

    z_signal = _z(panel[SIGNALS[signal]].astype(float).to_numpy())
    fit, *_ = np.linalg.lstsq(
        np.column_stack([np.ones(len(z_signal)), z_signal]), resid, rcond=None
    )
    intercept, slope = float(fit[0]), float(fit[1])
    if intercept <= NO_SPREAD_LEFT:
        # The intercept here is the MEAN absolute residual (the signal is
        # standardised, so its mean is zero), which cannot be negative. It
        # reaches zero only when the first regression fit the panel exactly -
        # in floating point, "exactly" means residuals near 1e-17, and a bare
        # `<= 0` test lets those through and returns a ratio of two numerical
        # zeros as if it were a loading. The outcome is standardised, so a mean
        # absolute residual this small is no spread at all, not a small one.
        return 0.0
    return max(0.0, slope / intercept)


def block_clusters(
    *, arrival_offsets: list[int], cluster_sizes: list[int], block_sessions: int
) -> np.ndarray:
    """Per-ROW cluster labels, cutting the CALENDAR into blocks of that length.

    Cutting on the arrival index instead would put two arrivals a month apart in
    one block whenever the calendar has a gap, which is the opposite of what a
    block is for.
    """
    if block_sessions < 1:
        raise ValueError(f"block_sessions must be at least 1, got {block_sessions!r}")
    labels: list[str] = []
    for offset, m in zip(arrival_offsets, cluster_sizes, strict=True):
        labels.extend([str(int(offset) // int(block_sessions))] * int(m))
    return np.array(labels)


def labels_for_method(
    *, method: str, arrival_offsets: list[int], cluster_sizes: list[int]
) -> np.ndarray:
    """Cluster labels for a named method. ``arrival`` is a block of one.

    Any ``block<N>`` is accepted, not only the four in :data:`METHODS`: the
    message used to list METHODS while the code accepted more, so it described
    the grid rather than the behaviour.
    """
    if method == ARRIVAL_METHOD:
        block = 1
    elif method.startswith(BLOCK_PREFIX) and method.removeprefix(BLOCK_PREFIX).isdigit():
        block = int(method.removeprefix(BLOCK_PREFIX))
    else:
        raise ValueError(
            f"unknown method {method!r}; expected {ARRIVAL_METHOD!r} or '{BLOCK_PREFIX}<N>'"
        )
    return block_clusters(
        arrival_offsets=arrival_offsets, cluster_sizes=cluster_sizes, block_sessions=block
    )


def rejection_rate(
    *,
    burnt_signals: np.ndarray,
    arrival_offsets: list[int],
    cluster_sizes: list[int],
    effects: dict[str, float],
    sd_y: float,
    icc: float,
    shared_fraction: float,
    horizon: int,
    method: str,
    coef_name: str,
    n_sims: int,
    wcb_boot: int,
    seed: int,
    signal_loading: float = 0.0,
    loading_feature: str = ALIGNMENT_FEATURE,
    alignment: NoiseAlignment | None = None,
    alpha: float = FWER,
    progress_every: int = 0,
) -> float:
    """Share of simulations rejecting ``coef_name``.

    With zero effects this is SIZE; with injected effects it is power. They are
    one function deliberately: a size and a power measured by different code
    could differ for reasons that have nothing to do with the panel.

    ``progress_every`` writes a count to stderr every N simulations, because a
    high-precision cell runs for over an hour and a silent loop forces the
    caller to reconstruct progress from CPU time. It reports the COUNT only,
    never the running rejection tally: that tally is a partial estimate of the
    number the run exists to produce, and watching it before deciding whether to
    keep going is optional stopping. It consumes no randomness, so a run with it
    on and a run with it off return the same answer.

    ``alignment`` must align the noise with the COLUMN UNDER TEST. The only
    magnitudes that exist are calibrated to the ATR slope, so a run that tested
    one column while the noise aligned with another would report a clean size
    for the wrong reason.
    """
    from alphalens_research.diagnostics.options_retro import wild_cluster_bootstrap_p

    names = list(effects)
    if coef_name not in names:
        raise ValueError(f"{coef_name!r} is not among the simulated effects {names}")
    if alignment is not None and alignment.feature != coef_name:
        raise ValueError(
            f"the noise aligns with {alignment.feature!r} while the test is on {coef_name!r}; "
            f"the calibrated magnitudes belong to the {ALIGNMENT_FEATURE!r} slope, so this would "
            f"report a size for the wrong reason. Align with {coef_name!r}, or test "
            f"{alignment.feature!r}."
        )
    coef_index = names.index(coef_name) + 1  # column 0 is the intercept

    labels = labels_for_method(
        method=method, arrival_offsets=arrival_offsets, cluster_sizes=cluster_sizes
    )
    if len(set(labels)) < 2:
        raise ValueError(
            f"method {method!r} leaves {len(set(labels))} cluster(s) on this calendar; "
            "a cluster-robust bootstrap needs at least two"
        )

    rng = np.random.default_rng(seed)
    hits = 0
    for done in range(1, n_sims + 1):
        y, x_all, _ = simulate_overlapping_panel(
            burnt_signals=burnt_signals,
            arrival_offsets=arrival_offsets,
            cluster_sizes=cluster_sizes,
            effects=effects,
            sd_y=sd_y,
            icc=icc,
            shared_fraction=shared_fraction,
            horizon=horizon,
            rng=rng,
            signal_loading=signal_loading,
            loading_feature=loading_feature,
            alignment=alignment,
        )
        p = wild_cluster_bootstrap_p(
            y, x_all, labels, coef_index, n_boot=wcb_boot, seed=int(rng.integers(1 << 30))
        )
        if p <= alpha:
            hits += 1
        if progress_every and done % progress_every == 0:
            # Both names are DERIVED, not passed in. Every cell runs twice, and
            # with --alignment-size it runs SIX times - three corners x size and
            # power - so a line that named neither would leave the reader unable
            # to tell which sixth of a long run they are watching. The arm comes
            # off the alignment itself, so it cannot disagree with the panel that
            # was actually simulated.
            pass_name = "size" if not any(effects.values()) else "power"
            arm = HOMOSKEDASTIC_ARM if alignment is None else alignment.level
            print(
                f"  {pass_name} {arm} {method} phi={shared_fraction:.2f} "
                f"kappa={signal_loading:.3f}: {done}/{n_sims} simulations",
                file=sys.stderr,
                flush=True,
            )
    return hits / n_sims


def size_under_alignment(
    *,
    burnt_signals: np.ndarray,
    arrival_offsets: list[int],
    cluster_sizes: list[int],
    null_effects: dict[str, float],
    gate_effects: dict[str, float],
    sd_y: float,
    icc: float,
    shared_fraction: float,
    horizon: int,
    method: str,
    coef_name: str,
    n_sims: int,
    wcb_boot: int,
    seed: int,
    signal_loading: float = 0.0,
    loading_feature: str = ALIGNMENT_FEATURE,
    gam: float | None = None,
    size_tolerance: float = SIZE_TOLERANCE,
    alpha: float = FWER,
    progress_every: int = 0,
) -> dict[str, Any]:
    """One grid cell at all three corners of the identified noise set.

    The question this answers is whether the SIZE verdict — which method this
    module picks, and whether any method is entitled to a power figure at all —
    survives noise that is heteroskedastic and aligned with the feature under
    test. The memo asserted that it does, on the grounds that a size check is
    not a power figure. That was never measured, and a wild cluster bootstrap's
    size under heteroskedasticity correlated with the regressor is not obviously
    its size under homoskedasticity, especially at few clusters where leverage
    concentrates in the high-|x| clusters.

    ``gam`` of ``None`` runs each aligned arm at the magnitude that REACHES the
    realized precision for that level, :data:`GAM_AT_REALIZED_SE_SESSION` and
    :data:`GAM_AT_REALIZED_SE_ROW`, for the sibling's reason: the worst corner of
    a bound has to be at least as bad as the one out-of-sample standard error
    this programme actually measured. The burnt-panel READINGS are deliberately
    not the default. A scalar runs both arms at that one magnitude.

    All three arms of a cell run on ONE seed, so the corners are PAIRED draw for
    draw and a difference between them is the mechanism rather than three
    independent Monte Carlo draws.

    The two-stage refusal WINS over the bracket: a corner whose size missed the
    bar contributes no power figure, and a span missing a corner is not a span,
    so ``power_span`` is withheld and ``power_span_withheld_reason`` names the
    arm responsible. That shape is the finding, not a formatting problem — if
    alignment pushes the arrival-session bootstrap over the bar, then no method
    is entitled to carry this memo's 64.6-66.2% there.
    """
    if gam is None and coef_name != ALIGNMENT_FEATURE:
        # Same refusal as `a20_power.power_bracket`, same reason: the calibrated
        # pair is a fact about the realized CR2 standard error of the ATR slope
        # on `sel_ar_20` and is not transferable to another column.
        raise ValueError(
            f"no default magnitude for {coef_name!r}: the calibrated pair "
            f"({GAM_AT_REALIZED_SE_SESSION:.3f} on sessions, {GAM_AT_REALIZED_SE_ROW:.3f} on "
            f"rows) belongs to the {ALIGNMENT_FEATURE!r} slope and is not transferable. Measure "
            f"that column's own magnitude and pass it as gam ({ALIGNMENT_MEMO} section 7.5)."
        )
    magnitudes = {
        ALIGNMENT_SESSION: GAM_AT_REALIZED_SE_SESSION if gam is None else float(gam),
        ALIGNMENT_ROW: GAM_AT_REALIZED_SE_ROW if gam is None else float(gam),
    }
    alignments: dict[str, NoiseAlignment | None] = {HOMOSKEDASTIC_ARM: None}
    for level in ALIGNED_ARMS:
        alignments[level] = NoiseAlignment(magnitudes[level], level, feature=coef_name)

    common = {
        "burnt_signals": burnt_signals,
        "arrival_offsets": arrival_offsets,
        "cluster_sizes": cluster_sizes,
        "sd_y": sd_y,
        "icc": icc,
        "shared_fraction": shared_fraction,
        "horizon": horizon,
        "method": method,
        "coef_name": coef_name,
        "n_sims": n_sims,
        "wcb_boot": wcb_boot,
        "signal_loading": signal_loading,
        "loading_feature": loading_feature,
        "alpha": alpha,
        "progress_every": progress_every,
    }

    arms: dict[str, dict[str, Any]] = {}
    for arm in BRACKET_ARMS:
        size = rejection_rate(effects=null_effects, seed=seed, alignment=alignments[arm], **common)
        held = size <= size_tolerance
        power = (
            rejection_rate(effects=gate_effects, seed=seed + 1, alignment=alignments[arm], **common)
            if held
            else None
        )
        arms[arm] = {
            "alignment": alignments[arm],
            "seed": seed,
            "size": size,
            "size_ci": wilson_interval(size, n_sims),
            "mc_half_width": MC_HALF_WIDTH_Z * monte_carlo_se(size, n_sims),
            "size_held": held,
            # A point comparison against the bar is not a verdict at this
            # headroom: the memo's conservative corner has 1.4 points of room
            # and 1.1 points of Monte Carlo error at 2000 panels.
            "size_resolved": verdict_is_resolved(size, n_sims, bar=size_tolerance),
            "power": power,
            "power_ci": None if power is None else wilson_interval(power, n_sims),
        }

    missing = [arm for arm in BRACKET_ARMS if arms[arm]["power"] is None]
    if missing:
        span, resolved = None, None
        reason = (
            f"no span: the {', '.join(missing)} corner(s) rejected above the size bar "
            f"{size_tolerance:.3f} under the null, and a span missing a corner is not a span"
        )
    else:
        powers = [arms[arm]["power"] for arm in BRACKET_ARMS]
        span, reason = (min(powers), max(powers)), None
        # The sibling's OWN predicate, borrowed rather than rewritten: a span
        # whose own Monte Carlo error crosses the gate bar is not an answer, and
        # `PowerBracket.is_resolved` is already that guard. The per-cell result
        # is still not a `PowerBracket` in general — a corner here may
        # legitimately carry no power, which that class refuses — but on the ONE
        # branch where all three corners have a number the shapes coincide
        # exactly, so a second copy of the arithmetic would be the thing that
        # drifts.
        resolved = PowerBracket(
            arms={arm: {coef_name: arms[arm]["power"]} for arm in BRACKET_ARMS},
            alignments={arm: arms[arm]["alignment"] for arm in BRACKET_ARMS},
            target=coef_name,
            shrinkage=GATE_SHRINKAGE,
            n_sims=n_sims,
        ).is_resolved(bar=GATE_POWER)
    return {
        "arms": arms,
        "power_span": span,
        "power_span_resolved": resolved,
        "power_span_withheld_reason": reason,
    }


def verdict_construction(
    rows: list[dict[str, Any]],
    *,
    magnitudes: dict[str, float] | None,
    n_sims: int,
) -> str:
    """The magnitudes, the panel count and the grid, for EVERY verdict branch.

    The surviving branch used to name none of them while the refuting branch
    named its cell and its ``n_sims``, and that asymmetry runs the wrong way: a
    surviving verdict is the one a reader is most likely to quote without its
    construction. The magnitudes were not even reaching the function, so with
    ``--alignment-gam 0.9`` the only gam on screen was the precision
    disclosure's calibrated 0.400 / 0.205 — a sentence that does not describe
    the figures beside it.
    """
    gams = ", ".join(
        f"gam {magnitudes[arm]:.3f} on {arm}s" for arm in ALIGNED_ARMS if arm in (magnitudes or {})
    )
    phis = sorted({row["shared_fraction"] for row in rows})
    loadings = sorted({row["loading"] for row in rows})
    methods = sorted(
        {row["method"] for row in rows},
        key=lambda m: METHODS.index(m) if m in METHODS else len(METHODS),
    )
    return (
        f"[{gams or 'magnitudes NOT RECORDED'}; {n_sims} panels per corner; phi "
        f"{'/'.join(f'{p:.2f}' for p in phis)}; loading {'/'.join(loadings)}; "
        f"method {'/'.join(methods)}]"
    )


def size_verdict(
    rows: list[dict[str, Any]],
    *,
    size_tolerance: float,
    n_sims: int,
    magnitudes: dict[str, float] | None,
    alpha: float = FWER,
) -> dict[str, Any]:
    """The verdict at BOTH readings, refusing a table with nothing in it.

    ``magnitudes`` is REQUIRED and has no default. With a default of ``None`` the
    library surface still handed back an affirmative verdict whose construction
    bracket read "magnitudes NOT RECORDED"; the one on-path caller always has
    them, so requiring the argument removes the branch rather than labelling it
    after the fact. ``None`` remains a legal VALUE, for the run where the aligned
    arms did not go at all.

    Two things sit here rather than in :func:`one_size_reading`.

    First, the REFUSAL. An empty list, and the rows of a default
    non-alignment run, both reached the surviving branch by fall-through and
    handed back ``claim_survives: True`` with the sentence "no method crosses the
    size bar ... (none)". Zero measurement produced an affirmative verdict.

    Second, the SECOND READING. The refutation this instrument produced sits in
    the cell the module itself calls double-counted: at the calibrated
    magnitudes the ``kappa=fitted`` x aligned corner crosses, while at
    ``kappa = 0`` nothing crosses. Reporting that flat conflates "the claim is
    refuted" with "the claim is refuted at these magnitudes in this cell". So the
    single-mechanism rows get a verdict of their own, ``refutation_is_composed``
    says whether every crossing cell applies the feature-dependence twice, and
    the verdict line states which reading refuted. A composed cell can still set
    ``claim_survives`` to ``False`` — the crossing is real — but never silently.
    """
    if not any(row.get("arms") for row in rows):
        raise ValueError(
            f"a size verdict needs aligned corners and this table has no aligned corners in "
            f"{len(rows)} row(s). An empty table and the rows of a default run both used to "
            f"fall through to the surviving branch and report claim_survives=True with nothing "
            f"measured behind it. Run the grid with --alignment-size, or do not ask for a verdict."
        )

    # Only the rows carrying `arms`, which is what `single_rows` below already
    # does. The refusal above is `not any(row.get("arms"))`, so ONE measured row
    # among 24 passes it - and the bracket was then built from ALL the rows,
    # advertising a phi/loading/method grid that was never run at three corners.
    # The second reading's bracket on the same table was honest; this is the same
    # filter applied to the first.
    measured_rows = [row for row in rows if row.get("arms")]
    construction = verdict_construction(measured_rows, magnitudes=magnitudes, n_sims=n_sims)
    full = one_size_reading(
        rows,
        size_tolerance=size_tolerance,
        n_sims=n_sims,
        alpha=alpha,
        construction=construction,
    )

    single_rows = [
        row for row in rows if row.get("arms") and row.get("loading") == SINGLE_MECHANISM_LOADING
    ]
    single = None
    if single_rows:
        single = one_size_reading(
            single_rows,
            size_tolerance=size_tolerance,
            n_sims=n_sims,
            alpha=alpha,
            construction=verdict_construction(single_rows, magnitudes=magnitudes, n_sims=n_sims),
        )

    # Read off the cells the REFUTING BRANCH actually measured, not off the
    # nested verdict: a run restricted to the single-mechanism rows has no
    # composed cell at all, and deciding this from the nested reading would call
    # its unresolved verdict "composed".
    #
    # And not off the crossing list alone either. `claim_survives` goes False on
    # TWO branches, and on the ordering branch `crosses_only_under_alignment` is
    # EMPTY by construction - that branch only runs when nothing crossed. Reading
    # composition off the crossings there forced it False and printed "at least
    # one crossing cell is a kappa=0 cell" beside "no corner crosses the bar",
    # about a table with no crossing cell at all. The ordering branch decides
    # from the cells whose HOLDING status differs between corners instead, which
    # is the quantity that branch measured.
    branch = full["refuting_branch"]
    deciding = _DECIDING_CELLS.get(branch)
    cells = [] if deciding is None else full[deciding]
    # THREE states, because the question has three answers. The field used to be
    # a bool, so `false` meant both "there was no refutation" and "there was a
    # single-mechanism refutation" - published on the surviving branch and on
    # both NO VERDICT branches as a statement about a refutation that does not
    # exist. `None` is "no refutation to report"; `refuting_branch` names which
    # branch produced the one that does.
    composed = (
        None
        if branch is None
        else bool(cells and all(cell["loading"] != SINGLE_MECHANISM_LOADING for cell in cells))
    )

    verdict_line = full["verdict_line"]
    if full["claim_survives"] is False:
        verdict_line = (
            f"{verdict_line}. {_which_reading_refuted(branch=branch, cells=cells, single=single)}"
        )

    return {
        **full,
        "single_mechanism": single,
        "refutation_is_composed": composed,
        "verdict_line": verdict_line,
    }


def _which_reading_refuted(
    *, branch: str | None, cells: list[dict[str, Any]], single: dict[str, Any] | None
) -> str:
    """Which of the two readings produced the refutation, in one sentence.

    ``branch`` names which branch of :func:`one_size_reading` refuted, and
    ``cells`` are the cells THAT branch measured. The noun comes from the branch
    so the sentence can only name a quantity that branch read: the ordering
    branch runs precisely when nothing crossed, so a sentence about crossing
    cells there contradicts the verdict line it is appended to.

    ``cells`` is never empty on either refuting branch, so there is no
    "no cell to name" case here and none is written. The crossing branch runs
    only when the crossing list is non-empty. The ordering branch cannot run with
    an empty ``holding_differs`` either: a method unresolved at any corner sends
    the table to the UNRESOLVED branch first, which takes precedence, so in the
    ordering branch every method is resolved everywhere and the holding sets can
    differ only through a per-row hold difference - which is exactly what
    ``holding_differs`` collects. Checked 2026-11-08 by exhaustive enumeration
    of all 3**12 assignments of held / over / unresolved to three arms x two
    methods x two rows: zero tables reach the ordering branch with no differing
    cell. ``TestEachRefutingBranchNamesOnlyWhatItMeasured`` pins the invariant.
    """
    noun = _REFUTING_CELL_NOUN.get(branch, "refuting cell")
    if any(cell["loading"] == SINGLE_MECHANISM_LOADING for cell in cells):
        return (
            f"the refutation is NOT composed: at least one {noun} is a "
            f"{SINGLE_MECHANISM_LOADING} cell, where the feature-dependence is applied ONCE"
        )
    if single is None:
        return (
            f"the refutation is COMPOSED: every {noun} applies the feature-dependence "
            f"TWICE, and this table carries no {SINGLE_MECHANISM_LOADING} rows to read it against"
        )
    if single["claim_survives"] is False:
        return (
            f"the refutation is COMPOSED in every {noun}, but the "
            f"{SINGLE_MECHANISM_LOADING} rows refute the claim on their own as well, so it does "
            f"not rest on the double-counted cells"
        )
    if single["claim_survives"] is None:
        return (
            f"the refutation is COMPOSED: on the {SINGLE_MECHANISM_LOADING} rows alone there is "
            f"NO VERDICT, so it stands only at the composed magnitudes"
        )
    return (
        f"the refutation is COMPOSED: on the {SINGLE_MECHANISM_LOADING} rows alone, where the "
        f"feature-dependence is applied ONCE, the claim SURVIVES. It fails only in cells that "
        f"apply it TWICE, through a linear loading of the shared shock and an exponential "
        f"rescaling of the whole residual, whose two magnitudes do not add"
    )


def _is_rejection_rate(value: Any) -> bool:
    """A rejection rate is a PROPORTION: a real number in ``[0, 1]``.

    Finiteness alone is not the predicate. The guard used to test it, while its
    own message said "not a finite rejection rate", and every finite number got
    through: a size of ``-0.50`` has a Wilson interval of ``(-0.4990, -0.4971)``,
    so it reads as resolved AND as holding the bar and produced "the published
    claim survives"; a size of ``1.50`` produced a resolved refutation printed as
    "rejects 150.0%". Both are the absence of a measurement wearing the shape of
    one, which is the same defect class as the NaN case beside it.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return math.isfinite(float(value)) and 0.0 <= float(value) <= 1.0


def _require_measured_corners(rows: list[dict[str, Any]]) -> None:
    """Every row carrying ``arms`` carries ALL three corners with a real size and power.

    One refusal for four shapes that all used to pass as measurements or fail
    with a bare builtin error:

    - a row carrying only some corners raised ``KeyError: 'row'``, which names
      neither the cell nor a remedy;
    - a ``size`` of ``None`` raised ``TypeError: '<=' not supported between
      instances of 'NoneType' and 'float'``;
    - a ``size`` of NaN raised nothing at all. ``nan <= tolerance`` is ``False``
      and reads as "did not hold", while ``verdict_is_resolved(nan, n)`` is
      ``True`` and reads as "resolved", so a run whose simulations all failed
      reported the claim SURVIVING with zero unresolved cells.
    - a ``size`` OUTSIDE ``[0, 1]`` raised nothing either, and read as a
      measurement in both directions: see :func:`_is_rejection_rate`.

    ``power`` is checked by the same predicate, because the whole argument above
    holds verbatim for the other measurement in the same dict: a non-finite power
    prints as ``nan-nan%`` with no ``?`` mark and ``PowerBracket.is_resolved``
    returns ``True`` for it, so an absent power reads as a resolved answer.
    ``None`` is LEGITIMATE there and is skipped - a corner whose size missed the
    bar contributes no power figure by design.

    The cell is named by its four coordinates, because a grid run is 72 cells and
    "a size is not a number" locates none of them.
    """
    for row in rows:
        arms = row.get("arms")
        if not arms:
            continue
        where = (
            f"phi {row.get('shared_fraction')!r}, loading {row.get('loading')!r}, "
            f"method {row.get('method')!r}"
        )
        for arm in BRACKET_ARMS:
            if arm not in arms:
                raise ValueError(
                    f"the cell at {where} carries no {arm!r} corner; a verdict compares all of "
                    f"{list(BRACKET_ARMS)} and a table missing one is not a measurement. Re-run "
                    f"that cell with --alignment-size, or drop the row."
                )
            for field in ("size", "power"):
                value = arms[arm].get(field)
                if field == "power" and value is None:
                    # A corner whose size missed the bar carries no power, by
                    # design: `size_under_alignment` withholds the whole span and
                    # names the arm responsible.
                    continue
                if not _is_rejection_rate(value):
                    raise ValueError(
                        f"the {arm!r} corner of the cell at {where} has {field} {value!r}, which "
                        f"is not a rejection rate. A rejection rate is a PROPORTION in [0, 1], "
                        f"and anything else is the ABSENCE of a measurement: it reads as 'did not "
                        f"hold' against the bar and as 'resolved' against the Monte Carlo error, "
                        f"so it would enter the verdict as a real refutation. Re-run that cell."
                    )


def one_size_reading(
    rows: list[dict[str, Any]],
    *,
    size_tolerance: float,
    n_sims: int,
    alpha: float = FWER,
    construction: str = "",
) -> dict[str, Any]:
    """Does alignment change WHICH METHOD WINS? A pure function of the table.

    Separated from the simulation on purpose: a verdict reachable only through 72
    cells of bootstrap could never be pinned in both directions, and the
    direction that matters most is the one where the claim fails.

    It recomputes ``size_held`` and the resolution from each arm's ``size``
    against ``size_tolerance`` and ``n_sims`` rather than reading the flags the
    simulation wrote. Same arithmetic, same answer — but it makes the function
    testable on a synthetic table and keeps the verdict from inheriting a stale
    flag.

    A cell counts as a CROSSING only when the homoskedastic corner holds the bar
    RESOLVED and an aligned corner exceeds it RESOLVED. Either side unresolved
    sends the cell to ``unresolved`` and never to the finding, which is how Monte
    Carlo noise cannot become the result.

    ``claim_survives`` is three-state and is PRINTED, never raised: this runs
    over the whole grid and a raise in the last cell would discard every cell
    before it. Precedence, in order: a resolved crossing makes it ``False``; any
    unresolved cell the verdict depends on makes it ``None``; a set of
    size-holding methods that differs between corners makes it ``False`` too,
    because that is the claim failing in the other direction; a table where NO
    method holds the bar at any corner makes it ``None``, because the claim under
    test is about WHICH method wins and there is no winner to compare; otherwise
    ``True``.

    The ARGUMENT is validated and refused, which is the one thing here that does
    raise. A verdict is a statement about a measurement, and a missing corner or
    a non-finite size is the absence of one: ``nan <= tolerance`` is ``False``,
    which reads as "did not hold", while ``verdict_is_resolved(nan, n)`` is
    ``True``, which reads as "resolved". An all-NaN table therefore reported the
    claim SURVIVING with zero unresolved cells, and a table whose aligned corners
    alone were NaN reported a resolved COMPOSED refutation that printed as
    ``nan%`` on screen and went to ``--out-json`` with no NaN marker on it.
    """
    _require_measured_corners(rows)

    def _held(size: float) -> bool:
        return size <= size_tolerance

    def _resolved(size: float) -> bool:
        return verdict_is_resolved(size, n_sims, bar=size_tolerance)

    crossings: list[dict[str, Any]] = []
    # Two SHAPES, counted apart. A cell-shaped entry has four coordinates (phi,
    # loading, method, arm); a method-corner entry has two (arm, method) and
    # speaks about a method's status across the WHOLE grid. Pooling them made
    # the NO VERDICT sentence print "12 cell(s)" for a four-cell table, where 12
    # was 3 arms x 4 methods and not a count of cells at all.
    unresolved_cells: list[dict[str, Any]] = []
    unresolved_method_corners: list[dict[str, Any]] = []
    # Cells whose HOLD status differs between the homoskedastic corner and an
    # aligned one, in EITHER direction. The crossing list is the one direction
    # that refutes on its own (home holds, aligned does not); this list is what
    # the ORDERING branch refutes from, where by construction nothing crossed.
    holding_differs: list[dict[str, Any]] = []
    # (arm, method) -> does it hold, and is it resolved, in EVERY cell of the
    # grid? A method that over-rejects at one phi is not entitled to a power
    # figure there, so "winning" means holding everywhere.
    status: dict[tuple[str, str], dict[str, bool]] = {}
    by_cell: dict[tuple[float, str], dict[str, dict[str, Any]]] = {}

    for row in rows:
        arms = row.get("arms")
        if not arms:
            continue
        by_cell.setdefault((row["shared_fraction"], row["loading"]), {})[row["method"]] = arms
        home = arms[HOMOSKEDASTIC_ARM]["size"]
        for arm in BRACKET_ARMS:
            size = arms[arm]["size"]
            state = status.setdefault((arm, row["method"]), {"held": True, "resolved": True})
            state["held"] = state["held"] and _held(size)
            state["resolved"] = state["resolved"] and _resolved(size)
            if arm == HOMOSKEDASTIC_ARM or _held(home) == _held(size):
                continue
            cell = {
                "shared_fraction": row["shared_fraction"],
                "loading": row["loading"],
                "method": row["method"],
                "arm": arm,
                "homoskedastic_size": home,
                "homoskedastic_size_ci": wilson_interval(home, n_sims),
                "aligned_size": size,
                "aligned_size_ci": wilson_interval(size, n_sims),
            }
            holding_differs.append(cell)
            if not (_held(home) and not _held(size)):
                # The other direction: the aligned corner holds where the
                # homoskedastic one does not. That is not a crossing — alignment
                # did not push anything OVER the bar — but it is exactly what
                # makes the holding sets differ, so the ordering branch needs it.
                continue
            if _resolved(home) and _resolved(size):
                crossings.append(cell)
            else:
                unresolved_cells.append(
                    {**cell, "reason": "a Wilson interval straddles the tolerance"}
                )

    methods_holding_size: dict[str, tuple[str, ...]] = {}
    for arm in BRACKET_ARMS:
        kept = []
        for method in METHODS:
            state = status.get((arm, method))
            if state is None:
                continue
            if not state["resolved"]:
                unresolved_method_corners.append(
                    {
                        "arm": arm,
                        "method": method,
                        "reason": "this method's size is not resolved against the tolerance",
                    }
                )
                continue
            if state["held"]:
                kept.append(method)
        methods_holding_size[arm] = tuple(kept)

    def _ranked(arms_by_method: dict[str, dict[str, Any]], arm: str) -> list[str]:
        present = [m for m in METHODS if m in arms_by_method]
        return sorted(
            present, key=lambda m: (abs(arms_by_method[m][arm]["size"] - alpha), METHODS.index(m))
        )

    def _disjoint(arms_by_method: dict[str, dict[str, Any]], arm: str, a: str, b: str) -> bool:
        lo_a, hi_a = wilson_interval(arms_by_method[a][arm]["size"], n_sims)
        lo_b, hi_b = wilson_interval(arms_by_method[b][arm]["size"], n_sims)
        return hi_a < lo_b or hi_b < lo_a

    # A pairwise swap counts only when the two Wilson intervals are disjoint on
    # BOTH sides. Two methods whose intervals overlap are not ordered, so
    # "reordering" them is noise.
    size_rank_changed: dict[str, tuple[dict[str, Any], ...]] = {}
    for arm in ALIGNED_ARMS:
        swaps: list[dict[str, Any]] = []
        for (phi, loading), arms_by_method in sorted(by_cell.items()):
            home_order = _ranked(arms_by_method, HOMOSKEDASTIC_ARM)
            arm_order = _ranked(arms_by_method, arm)
            for i, first in enumerate(home_order):
                for second in home_order[i + 1 :]:
                    if arm_order.index(first) < arm_order.index(second):
                        continue
                    if _disjoint(arms_by_method, HOMOSKEDASTIC_ARM, first, second) and _disjoint(
                        arms_by_method, arm, first, second
                    ):
                        swaps.append(
                            {
                                "shared_fraction": phi,
                                "loading": loading,
                                "pair": (first, second),
                            }
                        )
        size_rank_changed[arm] = tuple(swaps)

    # The union, in the order the two loops produced it, because every existing
    # consumer reads `unresolved` as "everything the verdict could not resolve".
    unresolved = unresolved_cells + unresolved_method_corners

    holding_sets = {methods_holding_size[arm] for arm in BRACKET_ARMS}
    ordering_changed = len(holding_sets) > 1

    branch: str | None = None
    # Why there is no verdict, when there is none. `claim_survives is None` has
    # TWO causes and they are not the same reading: on the unresolved branch the
    # instrument could not tell, and on the nothing-holds branch it measured
    # every corner and found no winner. The screen used to label both
    # "(not resolved)", which reports non-resolution about a completed
    # measurement.
    no_verdict_cause: str | None = None
    if crossings:
        survives: bool | None = False
        branch = REFUTING_BRANCH_CROSSING
        first = crossings[0]
        home_lo, home_hi = first["homoskedastic_size_ci"]
        aligned_lo, aligned_hi = first["aligned_size_ci"]
        verdict_line = (
            f"the published claim does NOT survive {construction}: at phi "
            f"{first['shared_fraction']:.2f}, "
            f"{first['loading']}, method {first['method']}, the {first['arm']}-aligned corner "
            f"rejects {first['aligned_size']:.1%} [95% {aligned_lo:.1%}, {aligned_hi:.1%}] "
            f"against the homoskedastic {first['homoskedastic_size']:.1%} "
            f"[95% {home_lo:.1%}, {home_hi:.1%}] and a bar of {size_tolerance:.1%}; "
            f"{len(crossings)} cell(s) cross only under alignment"
        )
    elif unresolved:
        survives = None
        no_verdict_cause = NO_VERDICT_UNRESOLVED
        verdict_line = (
            f"NO VERDICT on the published claim {construction}: "
            f"{len(unresolved_cells)} cell(s) and "
            f"{len(unresolved_method_corners)} method-corner(s) it depends on are "
            f"unresolved at {n_sims} simulations, so neither answer is distinguishable from "
            f"Monte Carlo noise. Re-run with more than --n-sims {n_sims}."
        )
    elif ordering_changed:
        survives = False
        branch = REFUTING_BRANCH_ORDERING
        verdict_line = (
            f"the published claim does NOT survive {construction}: no corner crosses the bar, "
            f"but the set of methods holding size differs between corners - "
            + "; ".join(f"{arm}={list(methods_holding_size[arm])}" for arm in BRACKET_ARMS)
        )
    elif not methods_holding_size[HOMOSKEDASTIC_ARM]:
        # An empty TABLE is refused above; this is an empty MEASUREMENT, which
        # used to fall through to the surviving branch and print "the methods
        # holding size are the same at all three corners (none)". Zero methods
        # entitled to a power figure is not the claim surviving: the claim is
        # about WHICH method wins under alignment, and there is no winner to
        # compare. It sits AFTER the ordering branch on purpose - a corner that
        # holds nothing while another holds something is a real refutation.
        survives = None
        no_verdict_cause = NO_VERDICT_NOTHING_HOLDS
        verdict_line = (
            f"NO VERDICT on the published claim {construction}: NO method holds the size bar "
            f"{size_tolerance:.1%} at ANY corner, so there is no size verdict for alignment to "
            f"leave unaffected and no corner is entitled to a power figure"
        )
    else:
        survives = True
        kept = ", ".join(methods_holding_size[HOMOSKEDASTIC_ARM])
        verdict_line = (
            f"the published claim survives {construction}: no method crosses the size bar "
            f"{size_tolerance:.1%} only under alignment, and the methods holding size are the "
            f"same at all three corners ({kept})"
        )

    # The holding sets and the ordering answer are MEASUREMENTS exactly when
    # NOTHING was left unresolved, because an unresolved (arm, method) is
    # DROPPED from the holding set - so an empty set then means "nobody could
    # tell" and not "nothing held", and the ordering answer is a comparison of
    # two sets neither of which was resolved. The artifact used to carry them
    # anyway, with `ordering_changed: false` written on the very run whose test
    # asserts that string never reaches the screen. A CLI's human and machine
    # renderings carry the SAME facts, so both surfaces withhold them, and this
    # field is what a consumer reads instead of inferring the suppression from a
    # null.
    #
    # Keyed on `unresolved` and NOT on `no_verdict_cause`: the cause is assigned
    # only on the unresolved branch, and `elif unresolved:` sits AFTER
    # `if crossings:`. A table with a resolved crossing AND an unresolved
    # method-corner therefore takes the CROSSING branch with `unresolved`
    # non-empty, and a cause-keyed gate published the truncated sets there as
    # measurements. `unresolved` exists on every branch; the cause does not.
    are_measurements = not unresolved
    return {
        "size_tolerance": size_tolerance,
        "n_sims": n_sims,
        "crosses_only_under_alignment": crossings,
        "holding_differs": holding_differs,
        "unresolved": unresolved,
        "unresolved_cells": unresolved_cells,
        "unresolved_method_corners": unresolved_method_corners,
        "holding_sets_are_measurements": are_measurements,
        "methods_holding_size": methods_holding_size if are_measurements else None,
        "ordering_changed": ordering_changed if are_measurements else None,
        "size_rank_changed": size_rank_changed,
        "claim_survives": survives,
        "no_verdict_cause": no_verdict_cause,
        "refuting_branch": branch,
        "verdict_line": verdict_line,
    }


#: Printed after a span whose own Monte Carlo error crosses the gate bar.
#: Without it an unresolved span reads exactly like an answer.
UNRESOLVED_SPAN_MARK = " ?"

#: Printed in place of a span when a corner missed the size bar.
WITHHELD_SPAN = "withheld"


def format_power_span(span: tuple[float, float] | None, *, resolved: bool | None) -> str:
    """One cell's power span for the screen, with its resolution.

    Extracted from :func:`main` so BOTH branches can be asserted. Inline, the
    only reachable fixture was one where every span resolves, so the
    unresolved rendering was unreachable from a test and
    ``span[0]..span[1]`` -> ``span[1]..span[1]`` left the suite green.
    """
    if span is None:
        return WITHHELD_SPAN
    mark = "" if resolved else UNRESOLVED_SPAN_MARK
    return f"{span[0] * 100:.1f}-{span[1] * 100:.1f}%{mark}"


def score_autocovariance(
    *, y: np.ndarray, x_all: np.ndarray, clusters: np.ndarray, coef_index: int, max_lag: int
) -> list[float]:
    """Autocorrelation of the per-session SCORE contributions, by session lag.

    The quantity that transmits dependence into a coefficient is
    ``s_t = sum over the session of x_t * u_t`` for the tested column, not the
    session-mean residual. The first diagnostic in this investigation measured
    the latter and was wrong to; this is the replacement.

    Sessions are ordered NUMERICALLY. They used to be sorted as strings, which
    with ten or more clusters puts "10" next to "1", so "lag 1" paired session 1
    with session 10. Measured 2026-11-07 on a 35-session panel whose per-session
    score is a monotone ramp (the test fixture, where the OLS fit is exactly zero
    so the score is known): calendar order reads +0.914, the lexicographic order
    reads +0.527.

    The SIGN of that error is NOT predictable, which is why the fix is the order
    and not a correction factor. Three AR(1) panels at rho 0.6 gave gaps of
    +0.236, +0.019 and −0.142 — one of the three read the scrambled panel as MORE
    dependent than it is. Reporting the ordering as a one-directional bias would
    have been a second wrong claim.

    A label that is not an integer string has no ordinal position, so a lag over
    it is not a lag and is refused rather than guessed at.
    """
    beta, *_ = np.linalg.lstsq(x_all, y, rcond=None)
    resid = y - x_all @ beta
    contrib = x_all[:, coef_index] * resid

    distinct = sorted(set(clusters.tolist()))
    sums = {c: float(contrib[clusters == c].sum()) for c in distinct}

    # The degeneracy refusal comes FIRST and deliberately: it is the more
    # fundamental one and it does not depend on the order, so a panel with no
    # variation to measure is named as that rather than as a labelling problem.
    centred = np.array([sums[c] for c in distinct], dtype=float)
    centred = centred - centred.mean()
    denom = float(np.dot(centred, centred))
    if denom <= 0:
        # Returning zeros here would read as "no dependence" when the truth is
        # "no variation to measure" -- a perfectly-fitting regression and an
        # independent panel would come back identical. Refuse instead.
        raise ValueError(
            "score contributions have no variation across sessions; the panel is "
            "degenerate and its autocorrelation is undefined, not zero"
        )

    try:
        order = sorted(distinct, key=int)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"session labels must be integer strings to carry a lag; got {distinct[:4]}. "
            f"A lag over non-ordinal labels is not a lag."
        ) from exc
    s = np.array([sums[c] for c in order], dtype=float)
    s = s - s.mean()
    out = []
    for lag in range(1, max_lag + 1):
        out.append(float(np.dot(s[:-lag], s[lag:]) / denom) if lag < len(s) else 0.0)
    return out


def arrival_offsets_from_dates(dates: list[str]) -> list[int]:
    """Calendar session offsets for sorted ISO arrival dates, first arrival = 0."""
    first = dt.date.fromisoformat(dates[0])
    return [sessions_between(first, dt.date.fromisoformat(d)) for d in dates]


def report(
    *,
    labels_dir: Any,
    briefs_dir: Any,
    population: str,
    n_sims: int,
    wcb_boot: int,
    seed: int,
    coef_name: str = "atr",
    horizon: int = HORIZON_SESSIONS,
    size_tolerance: float = SIZE_TOLERANCE,
    progress_every: int = 0,
    alignment_size: bool = False,
    alignment_gam: float | None = None,
    single_mechanism_only: bool = False,
) -> dict[str, Any]:
    """Size first, then power only where size held.

    Power for a method whose size is wrong is not a power figure, so computing
    it would invite quoting it. The two-stage shape is the point, not a saving.

    ``alignment_size`` adds the two aligned corners to every cell, as FIELDS on
    the existing row rather than as extra rows. Three corners as extra rows
    would triple the row count, break the pre-specified grid count, and split
    the random stream across corners so the corners stopped being paired. It is
    OFF by default because it triples the cost of the run that is this memo's own
    instrument, and a default run says so on screen.

    The per-cell result is NOT an :class:`a20_power.PowerBracket`. That class
    requires all three corners to carry a number, while here a corner can
    legitimately have none: its size missed the bar and the module's own rule
    refuses it a power figure. A bracket that treated a missing corner as present
    would hand back a span narrower than the identified set.

    ``single_mechanism_only`` drops the ``kappa=fitted`` arm and runs the
    ``kappa = 0`` grid alone. The overlap memo pre-authorises exactly that
    restriction — its size sentence "may only be edited, restricted to the
    kappa = 0 rows, or withdrawn" after a publishable run — so this flag exists
    to make that authorisation reachable. Without it the only quotable reading
    would be the one whose refuting cell applies the feature-dependence twice.
    """
    burnt = burnt_panel(labels_dir, briefs_dir, population=population)
    effects = standardised_effects(burnt)

    held = load_held_out(labels_dir)

    # Sizes and calendar come from ONE read, so they cannot describe different
    # panels. They used to be derived separately and a consistency check stood
    # here to catch the disagreement; the check is gone because the two reads
    # are gone. The episode collapse removes whole arrival sessions (one that
    # holds only a chained repeat of an earlier episode), which is exactly the
    # disagreement that check would have reported.
    by_anchor = held_out_episodes_by_arrival(held, population=population)
    # Sizes and offsets are zipped POSITIONALLY downstream, so cluster j's size
    # must be cluster j's calendar slot. Both are taken from one sorted pass for
    # that reason: `list(by_anchor.values())` would agree only because the mapping
    # happens to be built in key order, and a silent mis-pairing here would give
    # every cluster someone else's arrival date without changing any count.
    arrivals = sorted(by_anchor)
    sizes = [by_anchor[a] for a in arrivals]
    offsets = arrival_offsets_from_dates(arrivals)

    y_b = burnt[OUTCOME].astype(float).to_numpy()
    sd_y = float(np.std(y_b))
    icc = estimate_icc_local(y_b, burnt["arrival"].to_numpy())
    kappa = estimate_signal_loading(burnt, signal=coef_name)
    signals = np.column_stack([_z(burnt[c].astype(float).to_numpy()) for c in SIGNALS.values()])

    null_effects = dict.fromkeys(SIGNALS, 0.0)
    gate_effects = {n: shrink(effects[n], GATE_SHRINKAGE) * sd_y for n in SIGNALS}

    # Built up front rather than broken out of mid-loop. Exact equality is the
    # right test and not a smell here: `estimate_signal_loading` CLAMPS at zero,
    # so an exactly-zero fitted loading means the fitted arm would repeat the
    # control arm draw for draw, under the same seed. The omission is recorded
    # in the result so a reader never has to wonder why the grid is short.
    loadings: list[tuple[str, float]] = [(SINGLE_MECHANISM_LOADING, 0.0)]
    if kappa != 0.0 and not single_mechanism_only:
        loadings.append((COMPOSED_LOADING, kappa))

    rows: list[dict[str, Any]] = []
    for phi in SHARED_FRACTION_GRID:
        for loading_name, loading in loadings:
            for method in METHODS:
                common = {
                    "burnt_signals": signals,
                    "arrival_offsets": offsets,
                    "cluster_sizes": sizes,
                    "sd_y": sd_y,
                    "icc": icc,
                    "shared_fraction": phi,
                    "horizon": horizon,
                    "method": method,
                    "coef_name": coef_name,
                    "n_sims": n_sims,
                    "wcb_boot": wcb_boot,
                    "signal_loading": loading,
                    # Resolved BY NAME, so kappa and the alignment agree about
                    # which column the feature is.
                    "loading_feature": coef_name,
                    "progress_every": progress_every,
                }
                if alignment_size:
                    cell = size_under_alignment(
                        null_effects=null_effects,
                        gate_effects=gate_effects,
                        seed=seed,
                        gam=alignment_gam,
                        size_tolerance=size_tolerance,
                        **common,
                    )
                    home = cell["arms"][HOMOSKEDASTIC_ARM]
                    size, held_size, power = home["size"], home["size_held"], home["power"]
                else:
                    size = rejection_rate(effects=null_effects, seed=seed, **common)
                    held_size = size <= size_tolerance
                    power = (
                        rejection_rate(effects=gate_effects, seed=seed + 1, **common)
                        if held_size
                        else None
                    )
                    cell = {
                        "arms": None,
                        "power_span": None,
                        "power_span_resolved": None,
                        "power_span_withheld_reason": None,
                    }
                rows.append(
                    {
                        "shared_fraction": phi,
                        "loading": loading_name,
                        "method": method,
                        # The homoskedastic corner, named as `size` and `power`
                        # so every merged consumer of these two keys still reads
                        # the figure it has always read.
                        "size": size,
                        "size_held": held_size,
                        "power": power,
                        **cell,
                    }
                )

    # Read OFF the alignments that ran, not recomputed from `alignment_gam`: a
    # recomputation here could disagree with the panels that were simulated,
    # which is the exact failure the verdict line exists to prevent.
    magnitudes = (
        {arm: rows[0]["arms"][arm]["alignment"].gam for arm in ALIGNED_ARMS}
        if alignment_size and rows and rows[0]["arms"]
        else None
    )

    return {
        "population": population,
        "burnt_episodes": len(burnt),
        "held_out_clusters": len(sizes),
        "held_out_episodes": int(sum(sizes)),
        "calendar_span_sessions": offsets[-1] + 1,
        "sd_y": sd_y,
        "icc": icc,
        "signal_loading": kappa,
        "fitted_arm_omitted_as_duplicate": kappa == 0.0,
        "effects": effects,
        "n_sims": n_sims,
        "wcb_boot": wcb_boot,
        "seed": seed,
        "size_tolerance": size_tolerance,
        "alignment_size": alignment_size,
        "alignment_gam": alignment_gam,
        "alignment_magnitudes": magnitudes,
        "single_mechanism_only": single_mechanism_only,
        # In the artifact as well as on screen: a reader who only ever sees the
        # JSON would otherwise take the two mechanisms for substitutes.
        "composed_mechanisms_note": COMPOSED_MECHANISMS_NOTE,
        # Same rule, same reason. Without this a JSON-only reader has to INFER
        # from `alignment_size: false` plus `size_verdict: null` that every size
        # and power in the table is the homoskedastic corner alone - and the
        # inference does not supply the measured ratio the notice names.
        "default_run_notice": None if alignment_size else DEFAULT_RUN_NOTICE,
        "size_verdict": (
            size_verdict(rows, size_tolerance=size_tolerance, n_sims=n_sims, magnitudes=magnitudes)
            if alignment_size
            else None
        ),
        # TOP LEVEL, beside `size_tolerance` and `seed`. The sibling nests its
        # copy inside each population's record because its top level is keyed by
        # population and a sibling key would read as a third population. This
        # report is one flat single-population dict, so the top level IS the
        # record: a `for record in out.values()` loop copied from there would
        # attach a disclosure to `rows`, `effects` and the floats.
        PRECISION_DISCLOSURE_KEY: precision_disclosure(projections_note=PROJECTIONS_NOTE),
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    import dataclasses
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    home = Path.home() / ".alphalens"
    parser.add_argument("--labels-dir", type=Path, default=home / "selection_labels")
    parser.add_argument("--briefs-dir", type=Path, default=home / "thematic_briefs")
    parser.add_argument("--n-sims", type=int, default=400)
    parser.add_argument("--wcb-boot", type=int, default=399)
    parser.add_argument("--seed", type=int, default=20260923)
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument(
        "--progress-every",
        type=int,
        default=0,
        help="write a simulation count to stderr every N sims (0 = silent)",
    )
    parser.add_argument(
        "--population", choices=[POPULATION_BRIEFED, POPULATION_ALL], default=POPULATION_BRIEFED
    )
    parser.add_argument(
        "--alignment-size",
        action="store_true",
        help=(
            "run every cell at all three corners of the identified noise set "
            "(homoskedastic, session-aligned, row-aligned) and report whether the SIZE "
            "verdict - which method wins - survives alignment"
        ),
    )
    parser.add_argument(
        "--single-mechanism-only",
        action="store_true",
        help=(
            f"drop the {COMPOSED_LOADING} arm and run the {SINGLE_MECHANISM_LOADING} grid "
            "alone, the restriction this memo pre-authorises; a refutation arriving from a "
            "composed cell is a different claim from one arriving from a single-mechanism cell"
        ),
    )
    parser.add_argument(
        "--alignment-gam",
        type=float,
        default=None,
        help=(
            "run BOTH aligned arms at this magnitude instead of each level's own "
            "calibrated value; reproduces the memo's section 7.5 comparison"
        ),
    )
    args = parser.parse_args(argv)

    # Argument errors before the burnt read, so a wrong combination costs no
    # minutes. Three branches rather than one, because a single message about the
    # ZERO case would also be printed for a negative magnitude and for nan.
    if args.alignment_gam is not None:
        if not args.alignment_size:
            parser.error(
                "--alignment-gam sets the magnitude of the aligned arms and has no effect "
                "without --alignment-size"
            )
        way_out = (
            f"Omit --alignment-gam to run each level at the magnitude that reaches the "
            f"realized precision ({GAM_AT_REALIZED_SE_SESSION:.3f} on sessions, "
            f"{GAM_AT_REALIZED_SE_ROW:.3f} on rows)."
        )
        if not math.isfinite(args.alignment_gam):
            parser.error(f"--alignment-gam must be finite, got {args.alignment_gam}. {way_out}")
        if args.alignment_gam == 0:
            parser.error(
                f"--alignment-gam must be strictly positive, got {args.alignment_gam}. A zero "
                f"magnitude is the homoskedastic corner, not a second arm: it makes both "
                f"aligned corners bit-identical to it and the span zero-wide. {way_out}"
            )
        if args.alignment_gam < 0:
            parser.error(
                f"--alignment-gam must be positive, got {args.alignment_gam}. A negative "
                f"magnitude says the QUIET names carry the loud noise, which nothing measured "
                f"supports. {way_out}"
            )

    r = report(
        labels_dir=args.labels_dir,
        briefs_dir=args.briefs_dir,
        population=args.population,
        n_sims=args.n_sims,
        wcb_boot=args.wcb_boot,
        seed=args.seed,
        progress_every=args.progress_every,
        alignment_size=args.alignment_size,
        alignment_gam=args.alignment_gam,
        single_mechanism_only=args.single_mechanism_only,
    )

    print(f"\n=== population: {r['population']} ===")
    print(
        f"burnt {r['burnt_episodes']} episodes | held-out {r['held_out_episodes']} episodes "
        f"in {r['held_out_clusters']} clusters over {r['calendar_span_sessions']} sessions"
    )
    print(
        f"sd(y) {r['sd_y']:.4f} | icc {r['icc']:.3f} | fitted signal loading kappa "
        f"{r['signal_loading']:.3f} | {r['n_sims']} sims x {r['wcb_boot']} boot"
    )
    # Both magnitudes interpolated, and the two quantities NAMED APART. A
    # hardcoded `50%` here duplicated `a20_power.GATE_SHRINKAGE` and could drift
    # from it, and calling both this and `GATE_POWER` below "the gate" two lines
    # apart made the shrinkage of the alternative read as the power bar.
    print(
        f"\nsize bar {r['size_tolerance']:.3f} for a nominal {FWER:.2f}; power against the "
        f"alternative at {GATE_SHRINKAGE:.0%} shrinkage\n"
    )
    header = f"{'phi':>5} {'loading':>12} {'method':>9} {'size':>8} {'held':>6} {'power':>8}"
    if r["alignment_size"]:
        # The magnitude is in the COLUMN HEADING, read off the alignments that
        # ran. Without it a --alignment-gam run printed figures whose only
        # on-screen gam came from the precision disclosure, which quotes the
        # CALIBRATED pair whatever this run used.
        for arm in ALIGNED_ARMS:
            gam = r["alignment_magnitudes"][arm]
            header += f" | {arm + f' size gam {gam:.3f}':>22} {'held':>4}"
        header += f" | {'power span':>15}"
    print(header)
    for row in r["rows"]:
        power = "-" if row["power"] is None else f"{row['power'] * 100:7.1f}%"
        line = (
            f"{row['shared_fraction']:>5.2f} {row['loading']:>12} {row['method']:>9} "
            f"{row['size'] * 100:7.1f}% {'yes' if row['size_held'] else 'NO':>6} {power:>8}"
        )
        if row["arms"]:
            for arm in ALIGNED_ARMS:
                cell = row["arms"][arm]
                mark = "yes" if cell["size_held"] else "NO"
                line += (
                    f" | {cell['size'] * 100:14.1f}% "
                    f"+/-{cell['mc_half_width'] * 100:4.1f} {mark:>4}"
                )
            shown = format_power_span(row["power_span"], resolved=row["power_span_resolved"])
            line += f" | {shown:>15}"
        print(line)
    print("\n(phi 0.00 with kappa 0 is the merged preflight's assumption)")
    if r["alignment_size"]:
        print(
            f"(a span marked ? straddles the {GATE_POWER:.0%} POWER bar at {r['n_sims']} "
            f"panels and is not an answer)"
        )

    if r["size_verdict"] is None:
        # Without this line a default run reads as a measurement of the claim
        # the aligned corners exist to test.
        print(f"\n  *** {DEFAULT_RUN_NOTICE}")
    else:
        verdict = r["size_verdict"]
        print("\n  THE SIZE CLAIM UNDER TEST, from this memo's own qualifier: 'the bootstrap")
        print("  LEVEL verdict is unaffected: it is a size check, not a power figure'")
        print(f"  {verdict['verdict_line']}")
        if not verdict["holding_sets_are_measurements"]:
            # Vacuous whenever anything was left unresolved and standing alone
            # they read as measurements: an unresolved method is DROPPED from the
            # holding set, so "none" covers both an empty set and a set nobody
            # could resolve. Gated on the PAYLOAD FIELD, so the screen and
            # `--out-json` cannot disagree about what was withheld. Not on
            # `claim_survives is None`, which is also true on the nothing-holds
            # branch where every corner IS resolved and the empty sets are a
            # reading; and not on `no_verdict_cause`, which is unset on the
            # crossing branch even when that branch carries unresolved corners.
            print(f"    methods holding size: {NOT_RESOLVED_LABEL}")
            print(f"    ordering changed: {NOT_RESOLVED_LABEL}")
        else:
            empty = (
                MEASURED_EMPTY_LABEL
                if verdict["no_verdict_cause"] == NO_VERDICT_NOTHING_HOLDS
                else "none"
            )
            for arm in BRACKET_ARMS:
                kept = ", ".join(verdict["methods_holding_size"][arm]) or empty
                print(f"    methods holding size, {arm}: {kept}")
            print(f"    ordering changed: {verdict['ordering_changed']}")
        single = verdict["single_mechanism"]
        if single is not None:
            print(f"    SECOND READING, {SINGLE_MECHANISM_LOADING} rows alone:")
            print(f"      {single['verdict_line']}")
        # Tri-state on screen as well as in the payload, and still PRINTED when
        # there is nothing to report: a line that disappeared would leave the
        # reader unable to tell "not composed" from "the question did not arise".
        composed = verdict["refutation_is_composed"]
        print(
            "    refutation is composed: "
            + ("n/a (no refutation to report)" if composed is None else str(composed))
        )
        for crossing in verdict["crosses_only_under_alignment"]:
            print(
                f"    crosses only under alignment: phi {crossing['shared_fraction']:.2f} "
                f"{crossing['loading']} {crossing['method']} {crossing['arm']} "
                f"{crossing['aligned_size']:.1%} vs {crossing['homoskedastic_size']:.1%}"
            )
        if verdict["unresolved"]:
            # Both counts, named for what they count. One number over the pooled
            # list described neither shape.
            print(
                f"    unresolved: {len(verdict['unresolved_cells'])} cell(s), "
                f"{len(verdict['unresolved_method_corners'])} method-corner(s)"
            )
        for arm in ALIGNED_ARMS:
            if verdict["size_rank_changed"][arm]:
                print(f"    size rank swaps, {arm}: {verdict['size_rank_changed'][arm]}")
        # Printed beside the table, and recorded in the artifact under
        # `composed_mechanisms_note`, so neither surface can be read as two
        # separable mechanisms.
        print(f"    NOTE: {r['composed_mechanisms_note']}")

    # Printed on EVERY run, not behind a flag: every power figure in the table
    # above is conditional on it.
    print("\n  PRECISION DISCLOSURE (every size and power above is conditional on it)")
    for key, value in r[PRECISION_DISCLOSURE_KEY].items():
        print(f"  {key}: {value}")

    if args.out_json:
        # Dataclasses are written as STRUCTURED data, not as a stringified repr:
        # `asdict` keeps each aligned corner's `gam` a NUMBER, so a recorded
        # figure never loses the magnitude that produced it. `default=str` alone
        # would have written `NoiseAlignment(gam=0.4, ...)` as prose.
        def _as_json(value: Any) -> Any:
            if dataclasses.is_dataclass(value) and not isinstance(value, type):
                return dataclasses.asdict(value)
            return str(value)

        args.out_json.write_text(json.dumps(r, indent=2, default=_as_json) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
