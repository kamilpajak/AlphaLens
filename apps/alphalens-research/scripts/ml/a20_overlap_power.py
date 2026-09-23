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

Consequences, all closed-form and all asserted in the tests:

- within-cluster correlation is ``icc`` for every phi — the calibration is
  untouched;
- correlation at a lag of ``k`` sessions is ``phi * icc * (horizon - k) / horizon``,
  and zero at ``k >= horizon``;
- **phi = 0 reproduces the merged preflight exactly**, which is what makes this a
  measurement of the assumption rather than a replacement of it.

phi = 1 attributes ALL same-session correlation to the shared window. That is the
conservative end: any part of it that is genuinely arrival-specific (one
catalyst, one theme, one day's news) does not spill into the neighbouring
cluster. The truth is somewhere in between, so the grid is reported rather than
one value being chosen.

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
    FWER,
    GATE_SHRINKAGE,
    OUTCOME,
    POPULATION_ALL,
    POPULATION_BRIEFED,
    SIGNALS,
    _z,
    burnt_panel,
    estimate_icc_local,
    held_out_episodes_by_arrival,
    load_held_out,
    residual_variance,
    sessions_between,
    shrink,
    standardised_effects,
)

#: An ``sel_ar_20`` episode accumulates this many sessions from the arrival open.
HORIZON_SESSIONS = 20

#: Reported rather than chosen. 0.0 is the merged preflight's assumption, 1.0 is
#: the conservative end where every same-session correlation travels.
SHARED_FRACTION_GRID = (0.0, 0.5, 1.0)

#: Candidate inference methods. ``arrival`` is the status quo; the blocks widen
#: the cluster until it can absorb the shared window.
METHODS = ("arrival", "block5", "block10", "block20")

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
    """
    names = list(effects)
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

    xs, ys, cls = [], [], []
    for j, (offset, m) in enumerate(zip(arrival_offsets, cluster_sizes, strict=True)):
        idx = rng.integers(0, len(burnt_signals), m)
        x = burnt_signals[idx]
        shared = float(daily[int(offset) : int(offset) + horizon].sum())
        if signal_loading:
            loading = (1.0 + signal_loading * x[:, 0]) / math.sqrt(1.0 + signal_loading**2)
            shared_term = shared * loading
        else:
            shared_term = np.full(m, shared, dtype=float)
        local = rng.normal(0.0, math.sqrt(var_local)) if var_local > 0 else 0.0
        e = rng.normal(0.0, math.sqrt(var_idio), m) if var_idio > 0 else np.zeros(m, dtype=float)
        signal = np.zeros(m, dtype=float)
        for i, name in enumerate(names):
            signal = signal + effects[name] * x[:, i]
        ys.append(signal + shared_term + local + e)
        xs.append(x)
        cls.append(np.full(m, j))

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
    """Cluster labels for a named method. ``arrival`` is a block of one."""
    if method == "arrival":
        block = 1
    elif method.startswith("block"):
        block = int(method.removeprefix("block"))
    else:
        raise ValueError(f"unknown method {method!r}; expected one of {METHODS}")
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
    """
    from alphalens_research.diagnostics.options_retro import wild_cluster_bootstrap_p

    names = list(effects)
    if coef_name not in names:
        raise ValueError(f"{coef_name!r} is not among the simulated effects {names}")
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
        )
        p = wild_cluster_bootstrap_p(
            y, x_all, labels, coef_index, n_boot=wcb_boot, seed=int(rng.integers(1 << 30))
        )
        if p <= alpha:
            hits += 1
        if progress_every and done % progress_every == 0:
            # The pass name is derived, not passed in: every cell runs twice, and
            # two identical progress lines leave the reader unable to tell which
            # half of a two-hour run they are watching.
            pass_name = "size" if not any(effects.values()) else "power"
            print(
                f"  {pass_name} {method} phi={shared_fraction:.2f} "
                f"kappa={signal_loading:.3f}: {done}/{n_sims} simulations",
                file=sys.stderr,
                flush=True,
            )
    return hits / n_sims


def score_autocovariance(
    *, y: np.ndarray, x_all: np.ndarray, clusters: np.ndarray, coef_index: int, max_lag: int
) -> list[float]:
    """Autocorrelation of the per-session SCORE contributions, by session lag.

    The quantity that transmits dependence into a coefficient is
    ``s_t = sum over the session of x_t * u_t`` for the tested column, not the
    session-mean residual. The first diagnostic in this investigation measured
    the latter and was wrong to; this is the replacement.
    """
    beta, *_ = np.linalg.lstsq(x_all, y, rcond=None)
    resid = y - x_all @ beta
    contrib = x_all[:, coef_index] * resid

    order = sorted(set(clusters.tolist()))
    s = np.array([contrib[clusters == c].sum() for c in order], dtype=float)
    s = s - s.mean()
    denom = float(np.dot(s, s))
    if denom <= 0:
        # Returning zeros here would read as "no dependence" when the truth is
        # "no variation to measure" -- a perfectly-fitting regression and an
        # independent panel would come back identical. Refuse instead.
        raise ValueError(
            "score contributions have no variation across sessions; the panel is "
            "degenerate and its autocorrelation is undefined, not zero"
        )
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
) -> dict[str, Any]:
    """Size first, then power only where size held.

    Power for a method whose size is wrong is not a power figure, so computing
    it would invite quoting it. The two-stage shape is the point, not a saving.
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
    sizes = list(by_anchor.values())
    offsets = arrival_offsets_from_dates(sorted(by_anchor))

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
    loadings: list[tuple[str, float]] = [("kappa=0", 0.0)]
    if kappa != 0.0:
        loadings.append(("kappa=fitted", kappa))

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
                    "progress_every": progress_every,
                }
                size = rejection_rate(effects=null_effects, seed=seed, **common)
                held_size = size <= size_tolerance
                power = (
                    rejection_rate(effects=gate_effects, seed=seed + 1, **common)
                    if held_size
                    else None
                )
                rows.append(
                    {
                        "shared_fraction": phi,
                        "loading": loading_name,
                        "method": method,
                        "size": size,
                        "size_held": held_size,
                        "power": power,
                    }
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
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
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
    args = parser.parse_args(argv)

    r = report(
        labels_dir=args.labels_dir,
        briefs_dir=args.briefs_dir,
        population=args.population,
        n_sims=args.n_sims,
        wcb_boot=args.wcb_boot,
        seed=args.seed,
        progress_every=args.progress_every,
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
    print(f"\nsize bar {r['size_tolerance']:.3f} for a nominal {FWER:.2f}; power at the 50% gate\n")
    print(f"{'phi':>5} {'loading':>12} {'method':>9} {'size':>8} {'held':>6} {'power':>8}")
    for row in r["rows"]:
        power = "-" if row["power"] is None else f"{row['power'] * 100:7.1f}%"
        print(
            f"{row['shared_fraction']:>5.2f} {row['loading']:>12} {row['method']:>9} "
            f"{row['size'] * 100:7.1f}% {'yes' if row['size_held'] else 'NO':>6} {power:>8}"
        )
    print("\n(phi 0.00 with kappa 0 is the merged preflight's assumption)")

    if args.out_json:
        args.out_json.write_text(json.dumps(r, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
