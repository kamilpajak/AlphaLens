"""Power preflight for #1227 on `sel_ar_20` — does the ATR test clear its gate?

#1227 is the single held-out confirmation of the three Bonferroni-clear signals
(ATR, MA50 extension, press gate), Holm step-down at FWER 0.05. It may run only
once a pre-registered, outcome-blind power simulation shows **>=80% power at 50%
of the July discovery effect for the ATR test**. Owner decision D5
(`ml_label_registry_design_2026_09_16.md` §8 step 3) moved its primary from
`car_10` to `sel_ar_20`, which is why the discovery effect has to be recomputed
on the new label before any power number means anything.

This module answers one question — is it powered, and if not, on what date —
and registers nothing.

Outcome blindness
-----------------
The rule is an ALLOWLIST, not a ban on label columns:

* **Held-out (> 2026-07-05) contributes STRUCTURE only** —
  :data:`HELD_OUT_COLUMNS`. Which arrival session an episode falls on, whether
  it matured, and whether the brief carried it. Not the label, and **not the
  signal columns either**: reading held-out ``technical_atr_pct`` to learn how
  the three statistics co-move would put the held-out dependence structure
  straight into a Holm power number without touching one label value. A
  ``sel_ar_*`` denylist would have missed that; the allowlist does not, and
  ``tests/test_a20_power_preflight.py`` asserts the exact set.
* **Burnt (<= 2026-07-05) outcome values are fair game.** Discovery is frozen
  and already spent under ledger rule 3, with precedent: ``preflight_power_sim``
  in ``2026_09_experts_last_look.py`` reads burnt-panel scale and ICC, annotated
  "already-seen data, so this stays outcome-blind", and the ledger's 2026-09-03
  row records a burnt-panel read as no charge.

The simulated dependence therefore comes from REAL burnt-panel signal rows
resampled into the held-out cluster structure — never from synthetic
independent draws, which would overstate power for three technical signals on
the same names.

What the default DGP here is known to get wrong
-----------------------------------------------
Measured 2026-11-07, after the #1227 look was spent: this simulator is **1.36x
optimistic on the slope standard error** — it predicted 0.0538 where the run
realized 0.0731 (CR2). The cause is not the ICC, not normality and not the
resampling; it is noise that is HETEROSKEDASTIC AND ALIGNED WITH THE FEATURE
UNDER TEST. Full record, with seven probes:
``docs/research/a20_simulator_precision_gap_2026_11_07.md``.

The mechanism is available here as :class:`NoiseAlignment`, and it is OFF by
default so every published figure keeps reproducing bit for bit. It takes a
required LEVEL because the burnt panel does not identify one: the realized
precision is reached at ``gam`` 0.400 when a whole session's noise scales with
that session's mean ATR, and at 0.205 when each row's noise scales with its own
ATR — about a factor of two apart in the parameter. So the aligned answer this
module will produce is a :class:`PowerBracket` over all three corners, never a
single aligned power figure, and :meth:`PowerBracket.power_at_gate` refuses
rather than return one when the span crosses the gate.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
import re
from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

#: Every column the held-out branch may touch, and why each is structure rather
#: than outcome. ``sel_label_status_20`` says whether the horizon RESOLVED, never
#: what it resolved to; ``briefed_any_theme`` is a selection fact fixed at brief
#: time. Measured on the real store, ``population`` does NOT separate the two
#: arms (its values describe the provenance of the row set), so it is not here.
HELD_OUT_COLUMNS = (
    "brief_date",
    "ticker",
    "anchor_session",
    "briefed_any_theme",
    "sel_label_status_20",
)

#: The confirmation population is an open pre-registration decision. Discovery
#: ran on briefed names, so BRIEFED is the estimand-preserving choice; ALL adds
#: the LLM-proposed arm the stamper also writes (measured 2026-09-21: 396 vs 573
#: matured held-out episodes).
POPULATION_BRIEFED = "briefed"
POPULATION_ALL = "all"

#: The label status that means "this horizon resolved".
_RESOLVED = "ok"

#: #1227 pre-registered this grid; the gate is the middle one.
SHRINKAGE_GRID = (0.25, 0.50, 0.75)
GATE_SHRINKAGE = 0.50
GATE_POWER = 0.80

FWER = 0.05

#: An A20 episode resolves 20 trading sessions after its arrival, so the matured
#: frontier always trails the calendar by that much. #1227's Wake date was
#: derived under car_10 (10 sessions) and does not account for it.
A20_MATURITY_SESSIONS = 20

DISCOVERY_CUTOFF = "2026-07-05"

#: Step of the cluster-count search. The answer is the first count ON THIS GRID
#: that clears, never the true minimum - the memo must say so when it quotes it.
_SEARCH_STEP = 2

#: An arrival session, as the label store stamps it. Measured 2026-09-24: every
#: one of 1001 held-out rows matches, at exactly 10 characters.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def population_mask(frame: pd.DataFrame, population: str) -> np.ndarray:
    """Row mask for the confirmation population. Reads one allowlisted column.

    Shared by every held-out read so the cluster shape, the accrual rate and
    the arrival list cannot end up describing three different panels.
    """
    if population == POPULATION_ALL:
        return np.ones(len(frame), dtype=bool)
    if population != POPULATION_BRIEFED:
        raise ValueError(f"unknown population {population!r}")
    return frame["briefed_any_theme"].fillna(False).astype(bool).to_numpy()


def held_out_episodes_by_arrival(frame: pd.DataFrame, *, population: str) -> dict[str, int]:
    """Episodes per arrival session on the held-out side. Structure only.

    One read, one panel. The cluster sizes, the cluster COUNT and the arrival
    calendar all come out of this mapping, because deriving them separately is
    how they came to disagree: the accrual rate counted arrival sessions from
    raw rows while the sizes came from something else.

    Reads nothing outside :data:`HELD_OUT_COLUMNS`, and takes every column as a
    named Series rather than masking the frame, so a test can watch exactly what
    was asked for.

    The population filter is not cosmetic: taking cluster sizes from every
    held-out row while the confirmation runs on the briefed subset would inflate
    power by the ratio between them, and no assertion about label columns would
    catch it.

    Neither is the unit. Ledger rule 5 counts ticker-EPISODES under the chained
    5-session collapse, which is what ``burnt_panel`` applies on the other side.
    Counting distinct ``(brief_date, ticker)`` pairs here instead would take the
    effect size from collapsed episodes and the cluster sizes from uncollapsed
    rows; measured on the real store 2026-09-23 the two differ by about a factor
    of two, all of it in the direction of overstating power.
    """
    from alphalens_research.diagnostics.options_retro import ticker_episode_dedup

    status = frame["sel_label_status_20"].astype(str).to_numpy()
    # Sliced to a date: the store stamps plain dates today, but an anchor that
    # ever carried a time would silently split one session into several.
    anchor = frame["anchor_session"].astype(str).str.slice(0, 10).to_numpy()
    brief_date = frame["brief_date"].astype(str).to_numpy()
    ticker = frame["ticker"].astype(str).to_numpy()

    keep = (status == _RESOLVED) & population_mask(frame, population)

    # Refuse an unusable arrival HERE rather than let it reach the grouping.
    # Measured 2026-09-24: `.str.slice` hands a missing value back as float NaN
    # even after `astype(str)`, and `groupby` DROPS a NaN key by default — so an
    # episode with no arrival session silently leaves the panel, shrinking the
    # count with nothing raised anywhere. It does not inflate the cluster count,
    # which was the intuition; it deletes an episode.
    # Checked against the panel, not the file: an unusable anchor on a row this
    # panel excludes anyway is not a reason to refuse a run.
    unusable = {a for a in anchor[keep] if not (isinstance(a, str) and _ISO_DATE.fullmatch(a))}
    if unusable:
        raise ValueError(
            f"anchor_session is unusable on {len(unusable)} value(s) in the panel: "
            f"{sorted(map(repr, unusable))[:5]}. An arrival session must be an ISO date."
        )

    rows = pd.DataFrame(
        {"anchor": anchor[keep], "brief_date": brief_date[keep], "ticker": ticker[keep]}
    ).drop_duplicates(subset=["brief_date", "ticker"])
    # The chained collapse keeps the FIRST row of each episode, so the surviving
    # ``anchor`` is the episode's own arrival session, and an arrival session
    # holding nothing but chained repeats drops out of the mapping entirely.
    episodes = ticker_episode_dedup(rows)
    counts = episodes.groupby("anchor").size()
    # Ascending by arrival date, and callers may rely on it: the overlap simulator
    # pairs cluster sizes with calendar offsets by position.
    return {str(k): int(v) for k, v in sorted(counts.items())}


def holm_bars(m: int, alpha: float = FWER) -> list[float]:
    """The step-down bars for ``m`` hypotheses, smallest p first."""
    return [alpha / (m - i) for i in range(m)]


def holm_reject(pvalues: dict[str, float], alpha: float = FWER) -> set[str]:
    """Holm step-down. Returns the rejected hypothesis names.

    The early stop is the whole point: once a p-value misses its bar, nothing
    further is rejected even if a later p-value is below the unadjusted alpha.
    Without it this would be three independent tests wearing one alpha.
    """
    ordered = sorted(pvalues.items(), key=lambda kv: kv[1])
    bars = holm_bars(len(ordered), alpha)
    rejected: set[str] = set()
    for (name, p), bar in zip(ordered, bars, strict=True):
        if p <= bar:
            rejected.add(name)
        else:
            break
    return rejected


def rejected_under_family(
    pvalues: dict[str, float], *, family_size: int | None, alpha: float = FWER
) -> set[str]:
    """Which hypotheses reject, under a multiplicity family of ``family_size``.

    ``None`` means "all of them", which is the three-member Holm family #1227
    was first scoped as and the one every merged number was computed under.
    ``1`` is the family the registration narrowed to, where each hypothesis
    faces a plain one-sided ``alpha``.

    Only those two are accepted. A family of two among three hypotheses does not
    say which two share the bar, and inventing an answer would put an undefined
    correction into a pre-registration.
    """
    if family_size is None or family_size == len(pvalues):
        return holm_reject(pvalues, alpha)
    if family_size == 1:
        return {name for name, p in pvalues.items() if p <= alpha}
    raise ValueError(
        f"family_size {family_size} is undefined for {len(pvalues)} hypotheses; "
        f"use 1 (the registered family) or {len(pvalues)} (Holm across all)"
    )


def shrink(effect: float, factor: float) -> float:
    """Shrink a discovery effect toward zero, keeping its sign.

    The July ATR effect is negative (higher ATR, worse outcome); an
    implementation that flipped the sign would simulate the wrong alternative.
    """
    return effect * factor


def sessions_between(start: dt.date, end: dt.date) -> int:
    from alphalens_pipeline.market.calendar import trading_days_elapsed

    return int(trading_days_elapsed(start, end))


def gate_date(
    *,
    observed_arrivals: list[str],
    clusters_needed: int,
    accrual_per_session: float,
    maturity_lag_sessions: int = A20_MATURITY_SESSIONS,
    today: dt.date | None = None,
) -> dt.date:
    """When the panel will hold ``clusters_needed`` MATURED arrival clusters.

    ``observed_arrivals`` is every arrival session already in the held-out
    window, matured or not — and reading it is the whole point. An arrival that
    happened four weeks ago is already in flight; it only has to finish
    maturing. An earlier version of this function projected the time for those
    arrivals to HAPPEN as well, which double-counted and put the gate five weeks
    later than it is. Measured 2026-09-22: 55 arrivals exist, 34 have matured.

    The accrual projection survives only for the case it is actually for — when
    fewer arrivals exist than are needed.
    """
    from alphalens_pipeline.market.calendar import advance_trading_sessions

    today = today or dt.date.today()
    # De-duplicated here rather than trusted from the caller: one arrival
    # session is one cluster, and a repeated session would make the Nth entry
    # of this list the wrong session and the gate date wrong with it.
    arrivals = sorted(set(observed_arrivals))
    if clusters_needed <= 0:
        return today
    if len(arrivals) >= clusters_needed:
        nth = dt.date.fromisoformat(arrivals[clusters_needed - 1])
        return max(advance_trading_sessions(nth, maturity_lag_sessions), today)
    if accrual_per_session <= 0:
        raise ValueError("accrual_per_session must be positive")
    missing = clusters_needed - len(arrivals)
    sessions = math.ceil(missing / accrual_per_session) + maturity_lag_sessions
    return advance_trading_sessions(today, sessions)


def monte_carlo_se(power: float, n_sims: int) -> float:
    """Standard error of a simulated power estimate: sqrt(p(1-p)/n)."""
    if n_sims <= 0:
        raise ValueError("n_sims must be positive")
    return math.sqrt(max(power * (1.0 - power), 0.0) / n_sims)


def wilson_interval(power: float, n_sims: int, *, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a simulated proportion.

    Used in place of the plain ``p +/- z*SE`` because that one COLLAPSES at the
    boundary: 4 out of 4 rejections gives SE = 0 and would claim certainty that
    power exceeds any bar. Wilson keeps a width at 0 and 1, which is the whole
    point of the guard.
    """
    if n_sims <= 0:
        raise ValueError("n_sims must be positive")
    denom = 1.0 + z**2 / n_sims
    centre = (power + z**2 / (2 * n_sims)) / denom
    half = z / denom * math.sqrt(max(power * (1.0 - power), 0.0) / n_sims + z**2 / (4 * n_sims**2))
    return centre - half, centre + half


def verdict_is_resolved(power: float, n_sims: int, *, bar: float = GATE_POWER) -> bool:
    """False when the simulation interval straddles the bar — no verdict yet.

    This exists because it already went wrong: 200 simulations gave 76% and 400
    gave 79.75%, both printed to whole percent while the error was +/-2 points.
    Only 4 000 simulations (+/-0.7) could tell "clears" from "does not". A
    number whose own error bar crosses the bar is not an answer, and printing
    it as one is how a gate gets decided by noise.
    """
    low, high = wilson_interval(power, n_sims)
    return not (low <= bar <= high)


def measured_accrual(sizes_by_anchor: dict[str, int]) -> float:
    """Clusters per XNYS session over the matured window actually observed.

    Measured rather than assumed: dividing matured clusters by the number of
    held-out BRIEF dates instead would count arrivals that have not resolved and
    understate the rate (2026-09-21: 34 clusters over 34 sessions = 1.00, while
    the same clusters over 77 brief dates would read 0.44).
    """
    anchors = sorted(sizes_by_anchor)
    if len(anchors) < 2:
        return 0.0
    span = (
        sessions_between(dt.date.fromisoformat(anchors[0]), dt.date.fromisoformat(anchors[-1])) + 1
    )
    return len(anchors) / span if span > 0 else 0.0


def residual_variance(burnt_signals: np.ndarray, effects: dict[str, float], sd_y: float) -> float:
    """The noise variance left once the injected signal has taken its share.

    The panel's ``sd_y`` is the TOTAL spread of the outcome, signal included. A
    DGP that draws noise with variance ``sd_y**2`` and then adds the signal on
    top simulates a panel with more variance than the one it was calibrated to,
    inflates the residual the test statistic divides by, and reports too little
    power. Measured 2026-09-22 on the burnt panel: R2 = 0.28, so at the 50%
    gate the injected signal accounts for 7% of the variance — worth roughly
    four points of power, against a gap to the bar of 2.7.

    An effect large enough to explain the whole panel is refused rather than
    floored: negative noise is an incoherent DGP, and a floor would quietly
    simulate a panel nobody measured.
    """
    names = list(effects)
    beta = np.array([effects[name] for name in names], dtype=float)
    explained = float(np.var(burnt_signals[:, : len(beta)] @ beta))
    remaining = sd_y**2 - explained
    if remaining <= 0:
        raise ValueError(
            f"injected effects explain {explained:.6g} of a panel whose variance is "
            f"{sd_y**2:.6g}; the DGP has no noise left"
        )
    return remaining


# ------------------------------------------------- feature-aligned noise
#: The two LEVELS the burnt panel does NOT choose between. Section 7.5 of
#: ``a20_simulator_precision_gap_2026_11_07.md``: the realized CR2 standard error
#: is reached at ``gam`` 0.400 when a whole session's noise scales with that
#: session's mean feature, and at 0.205 when each ROW's noise scales with its own
#: feature value — with no clustering at all.
ALIGNMENT_SESSION = "session"
ALIGNMENT_ROW = "row"
ALIGNMENT_LEVELS = (ALIGNMENT_SESSION, ALIGNMENT_ROW)

#: The feature the #1227 gate turns on, and so the default alignment target.
ALIGNMENT_FEATURE = "atr"

#: Measured on the burnt panel 2026-11-07, residuals from the 3-signal
#: standardised fit (memo sections 7.3 and 7.5). The SESSION arm has 21 points
#: and a t of 3.56; the ROW arm has nine times the points and finds less.
MEASURED_GAM_SESSION = 0.360
MEASURED_GAM_SESSION_SE = 0.101
MEASURED_GAM_SESSION_POINTS = 21
MEASURED_GAM_ROW = 0.132
MEASURED_GAM_ROW_SE = 0.085
MEASURED_GAM_ROW_POINTS = 198

#: The magnitude at which each level reaches the realized CR2 precision. The two
#: differ by about a factor of two, which is why the LEVEL matters (section 7.5).
GAM_AT_REALIZED_SE_SESSION = 0.400
GAM_AT_REALIZED_SE_ROW = 0.205

#: The gap this mechanism exists to explain (memo sections 4 and 7).
SIMULATED_SLOPE_SE_PUBLISHED = 0.0538
REALIZED_SLOPE_SE_CR2 = 0.0731
SIMULATOR_OPTIMISM_RATIO = 1.36

#: What the section 7.3 estimator reads back when a known truth is simulated, and
#: its size under homoskedasticity against a nominal 5% (section 7.5, 1500 panels
#: per row). The session arm OVER-READS a session truth and still reads clearly
#: positive under a purely ROW-level truth, which is the non-identification.
ESTIMATOR_READING_OF_A_SESSION_TRUTH = 0.419
ESTIMATOR_READING_OF_A_ROW_TRUTH = 0.240
ESTIMATOR_SIZE_UNDER_HOMOSKEDASTICITY = 0.090

#: Session-level estimation floors. A session with fewer episodes has no usable
#: residual sd; fewer qualifying sessions than this has no slope and no se.
MIN_EPISODES_PER_SESSION_FOR_GAM = 3
MIN_SESSIONS_FOR_GAM_SLOPE = 3

#: Memo reference, printed beside every aligned figure so a quoted number
#: carries its construction.
ALIGNMENT_MEMO = "a20_simulator_precision_gap_2026_11_07.md"

#: Key the disclosure is written under in the machine-readable artifact.
PRECISION_DISCLOSURE_KEY = "precision_disclosure"

#: Guard, not a model: an exactly-zero residual is ``-inf`` in a log and would
#: poison a whole slope. At 198 points it is unreachable in practice.
_LOG_ABS_FLOOR = 1e-12


@dataclasses.dataclass(frozen=True)
class NoiseAlignment:
    """Noise scaled by ``exp(gam * z(feature))`` — a magnitude AND a level.

    The level is a required field with no default on purpose. The burnt panel
    does not identify it: the realized CR2 standard error of 0.0731 is reached at
    ``gam`` 0.400 on sessions and at 0.205 on rows, so a default would bake an
    unidentified choice into every later power figure.

    ``feature`` is a KEY of the ``effects`` mapping, not a column name.
    """

    gam: float
    level: str
    feature: str = ALIGNMENT_FEATURE

    def __post_init__(self) -> None:
        # Argument errors, so plain ValueError — the one RuntimeError in this
        # area is reserved for the identified set straddling the gate.
        if self.level not in ALIGNMENT_LEVELS:
            raise ValueError(
                f"unknown alignment level {self.level!r}; the panel does not choose between "
                f"{ALIGNMENT_SESSION!r} and {ALIGNMENT_ROW!r}, so one of them must be named"
            )
        if not math.isfinite(self.gam):
            raise ValueError(f"gam must be finite, got {self.gam!r}")
        if self.gam <= 0:
            # Zero is refused as well as negative. At gam 0 the scale is exactly
            # 1 everywhere, so an "aligned" arm is BIT-IDENTICAL to the
            # homoskedastic one: the span collapses to zero width while still
            # printing as a bracket, and the one scalar accessor then hands back
            # the corner this module measures to be
            # SIMULATOR_OPTIMISM_RATIO optimistic. A negative magnitude would
            # say the QUIET names carry the loud noise, which nothing measured
            # supports.
            raise ValueError(
                f"gam must be strictly positive, got {self.gam!r}. A zero magnitude is the "
                f"homoskedastic corner, not a second arm: it makes the aligned corners "
                f"bit-identical to it and the span zero-wide."
            )


def _alignment_scale(
    xs: list[np.ndarray], *, alignment: NoiseAlignment, names: list[str]
) -> list[np.ndarray]:
    """Per-row noise scale for each cluster, already variance-normalised.

    The scale is a function of ``x``, which the caller has ALREADY drawn, so this
    consumes no randomness — that is what lets the mechanism exist without moving
    the default path by one bit.

    The normaliser is EMPIRICAL (``c = sqrt(mean of s**2)`` over the panel's own
    rows) and deliberately not the analytic ``sqrt(1 + kappa**2)`` that
    ``a20_overlap_power.simulate_overlapping_panel`` uses. That one is exact
    because its mechanism is LINEAR in ``x`` and ``_z`` makes ``E[x**2] == 1``.
    ``exp()`` has no such closed form: its second moment depends on the whole
    distribution of the feature, and ATR is skewed. Do not "fix" this into a
    constant.

    No clipping of the scale. A clip would be a second, undocumented mechanism;
    the variance is held by the normaliser instead.
    """
    if alignment.feature not in names:
        raise ValueError(
            f"alignment feature {alignment.feature!r} is not one of the injected effects "
            f"{names}; the feature names a hypothesis, not a column"
        )
    k = names.index(alignment.feature)
    gam = alignment.gam

    if alignment.level == ALIGNMENT_ROW:
        # The columns come from `_z`, which divides by the POPULATION sd, so
        # x[:, k] IS z(row feature) — the regressor section 7.5's row arm uses.
        raw = [np.exp(gam * x[:, k]) for x in xs]
    else:
        # z(session mean feature), the regressor the section 7.3 estimator
        # regresses on, so `gam` carries one unit here and there. Constant within
        # a cluster, so the cluster structure survives exactly.
        cluster_means = np.array([float(np.mean(x[:, k])) for x in xs])
        zc = _z(cluster_means)
        raw = [
            np.full(len(x), math.exp(gam * float(z)), dtype=float)
            for x, z in zip(xs, zc, strict=True)
        ]

    rows = sum(len(part) for part in raw)
    c = math.sqrt(float(sum(float(np.sum(part**2)) for part in raw)) / rows)
    return [part / c for part in raw]


def simulate_panel(
    *,
    burnt_signals: np.ndarray,
    cluster_sizes: list[int],
    effects: dict[str, float],
    sd_y: float,
    icc: float,
    rng: np.random.Generator,
    alignment: NoiseAlignment | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One simulated panel: ``(y, X, clusters)``.

    Split out of :func:`simulate_power` so a test can measure the spread of the
    outcome it produces. That is the check that would have caught the variance
    bug: assert ``std(y)`` against the ``sd_y`` the panel was calibrated to.

    ``icc`` is applied to the RESIDUAL variance. It was estimated on the real
    outcome, which carries the signal's own cluster structure, so some
    within-day dependence is counted twice — that direction costs power rather
    than manufacturing it, which is the safe way round for a gate.

    ``alignment`` is OFF by default and the off path is bit-identical to the
    version that produced every published figure: the loop below draws the same
    integers, the same cluster shock and the same idiosyncratic noise in the same
    order, and the rescaling is a deterministic function of ``x``, which is drawn
    first. Measured 2026-11-07, this DEFAULT path is 1.36x optimistic on the
    slope standard error (0.0538 simulated against a realized 0.0731) because
    the real noise aligns with the feature under test; see
    :func:`power_bracket` for the identified set and :data:`ALIGNMENT_MEMO`.

    ``a20_overlap_power.simulate_overlapping_panel`` is a second copy of this
    DGP and has NO aligned path. Its bit-identical pin against this function
    therefore covers the off path only.
    """
    names = list(effects)
    var_resid = residual_variance(burnt_signals, effects, sd_y)
    var_u = icc * var_resid
    var_e = (1.0 - icc) * var_resid

    xs, sigs, us, es, cls = [], [], [], [], []
    for j, m in enumerate(cluster_sizes):
        idx = rng.integers(0, len(burnt_signals), m)
        x = burnt_signals[idx]  # (m, k) REAL rows, correlation intact
        u = rng.normal(0.0, math.sqrt(var_u))
        e = rng.normal(0.0, math.sqrt(var_e), m)
        signal = sum(effects[name] * x[:, i] for i, name in enumerate(names))
        sigs.append(signal)
        us.append(u)
        es.append(e)
        xs.append(x)
        cls.append(np.full(m, j))

    if alignment is None:
        # The same expression, the same operand order, the same broadcast of the
        # scalar `u` as before the mechanism existed.
        ys = [sig + u + e for sig, u, e in zip(sigs, us, es, strict=True)]
    else:
        scale = _alignment_scale(xs, alignment=alignment, names=names)
        ys = [sig + (u + e) * s for sig, u, e, s in zip(sigs, us, es, scale, strict=True)]
    y = np.concatenate(ys)
    X = np.column_stack([np.ones(len(y)), np.vstack(xs)])
    return y, X, np.concatenate(cls).astype(str)


def simulate_power(
    *,
    burnt_signals: np.ndarray,
    cluster_sizes: list[int],
    effects: dict[str, float],
    sd_y: float,
    icc: float,
    n_sims: int,
    wcb_boot: int,
    seed: int,
    family_size: int | None = None,
    alignment: NoiseAlignment | None = None,
) -> dict[str, float]:
    """Power for each hypothesis at the given injected effects.

    ``family_size`` picks the multiplicity family; see
    :func:`rejected_under_family`. The default is Holm across all of them,
    which is what every merged number was computed under.

    ``alignment`` exists because :func:`power_bracket` needs a per-arm engine.
    It is not a way to obtain "the aligned power figure": one level is not
    identified, so the caller-facing answer is a :class:`PowerBracket`.

    ``burnt_signals`` is a real (rows x hypotheses) matrix from the BURNT panel.
    Rows are resampled whole into the held-out cluster structure, so the
    cross-signal correlation is the one the data actually has — simulating
    independent normals instead would overstate power for three technical
    signals on the same names.
    """
    from alphalens_research.diagnostics.options_retro import wild_cluster_bootstrap_p

    names = list(effects)
    rng = np.random.default_rng(seed)
    wins = dict.fromkeys(names, 0)

    for _ in range(n_sims):
        y, X, cl = simulate_panel(
            burnt_signals=burnt_signals,
            cluster_sizes=cluster_sizes,
            effects=effects,
            sd_y=sd_y,
            icc=icc,
            rng=rng,
            alignment=alignment,
        )
        pvalues = {
            name: wild_cluster_bootstrap_p(
                y, X, cl, i + 1, n_boot=wcb_boot, seed=int(rng.integers(1 << 30))
            )
            for i, name in enumerate(names)
        }
        for name in rejected_under_family(pvalues, family_size=family_size):
            wins[name] += 1

    return {name: wins[name] / n_sims for name in names}


class AlignmentLevelNotIdentifiedError(RuntimeError):
    """The identified set straddles the gate, so no single power figure is
    defensible: the session and row readings reach the realized precision at
    magnitudes that differ by about a factor of two."""


#: The identified set, ordered OPTIMISTIC FIRST. The homoskedastic corner is the
#: DGP that produced every published figure; the other two are the levels the
#: burnt panel does not choose between.
BRACKET_ARMS = ("homoskedastic", ALIGNMENT_SESSION, ALIGNMENT_ROW)


@dataclasses.dataclass(frozen=True)
class PowerBracket:
    """Power over the identified set of noise mechanisms, never a point.

    ``arms`` holds every corner, keyed by :data:`BRACKET_ARMS`; ``alignments``
    records the :class:`NoiseAlignment` each aligned corner ran at, so a quoted
    span can never lose track of the magnitude that produced it — the same
    reason :func:`report` carries ``family_size``.
    """

    arms: dict[str, dict[str, float]]
    alignments: dict[str, NoiseAlignment | None]
    target: str
    shrinkage: float
    n_sims: int

    def __post_init__(self) -> None:
        # A two-arm bracket prints in the same format as a three-arm one, and its
        # span then reads as the identified set while being a subset of it.
        for label, got in (("arms", self.arms), ("alignments", self.alignments)):
            if set(got) != set(BRACKET_ARMS):
                raise ValueError(
                    f"a bracket covers exactly {list(BRACKET_ARMS)}; {label} carries "
                    f"{sorted(got)}. A subset of the identified set is not a bracket."
                )

    def span(self) -> tuple[float, float]:
        """The lowest and highest corner for the target hypothesis."""
        values = [self.arms[arm][self.target] for arm in BRACKET_ARMS]
        return min(values), max(values)

    def arm_power(self, arm: str) -> float:
        """One named corner. There is deliberately no positional accessor."""
        if arm not in self.arms:
            raise ValueError(f"unknown bracket arm {arm!r}; the corners are {list(BRACKET_ARMS)}")
        return self.arms[arm][self.target]

    def is_resolved(self, *, bar: float = GATE_POWER) -> bool:
        """True only when all three corners' Wilson intervals clear the bar together.

        Same shape and same reasoning as :func:`verdict_is_resolved`, one level
        up: a span whose own Monte Carlo error crosses the bar is not an answer.
        """
        sides = set()
        for arm in BRACKET_ARMS:
            low, high = wilson_interval(self.arms[arm][self.target], self.n_sims)
            if low <= bar <= high:
                return False
            sides.add(high < bar)
        return len(sides) == 1

    def power_at_gate(self, *, bar: float = GATE_POWER) -> float:
        """The MOST PESSIMISTIC corner, and only when the span resolves.

        The one scalar accessor on this type, so reaching for a number can never
        hand back the homoskedastic corner that an ordered container would.
        """
        if self.is_resolved(bar=bar):
            return min(self.arms[arm][self.target] for arm in BRACKET_ARMS)

        low, high = self.span()
        homo = self.arms["homoskedastic"][self.target]
        session = self.arms[ALIGNMENT_SESSION][self.target]
        row = self.arms[ALIGNMENT_ROW][self.target]
        gam_session = self.alignments[ALIGNMENT_SESSION]
        gam_row = self.alignments[ALIGNMENT_ROW]
        # The homoskedastic corner is the one a caller reaching for a scalar
        # would get from any ordered container, and it is the corner measured
        # 1.36x optimistic. Returning any single arm here would print in the
        # same format as a measured power figure.
        raise AlignmentLevelNotIdentifiedError(
            f"{self.target} power at the gate spans {low:.1%} to {high:.1%} across the "
            f"identified set (homoskedastic {homo:.1%}, session gam "
            f"{gam_session.gam if gam_session is not None else float('nan'):.3f} -> "
            f"{session:.1%}, row gam "
            f"{gam_row.gam if gam_row is not None else float('nan'):.3f} -> {row:.1%}, "
            f"{self.n_sims} panels) "
            f"and the span crosses the {bar:.0%} bar. This panel does not choose the LEVEL: the "
            f"realized CR2 standard error of {REALIZED_SLOPE_SE_CR2:.4f} is reached at gam "
            f"{GAM_AT_REALIZED_SE_SESSION:.3f} on sessions and at gam "
            f"{GAM_AT_REALIZED_SE_ROW:.3f} on rows ({ALIGNMENT_MEMO} section 7.5), so one corner "
            f"is NOT a power figure. The three corners above stand; a single number does not. "
            f"Quote the span, or raise --n-sims if the corners are separated only by Monte "
            f"Carlo error."
        )


def power_bracket(
    *,
    burnt_signals: np.ndarray,
    cluster_sizes: list[int],
    effects: dict[str, float],
    sd_y: float,
    icc: float,
    n_sims: int,
    wcb_boot: int,
    seed: int,
    family_size: int | None = None,
    target: str = ALIGNMENT_FEATURE,
    feature: str = ALIGNMENT_FEATURE,
    gam: float | Mapping[str, float] | None = None,
    shrinkage: float = GATE_SHRINKAGE,
) -> PowerBracket:
    """Power at all three corners of the identified set.

    ``gam`` of ``None`` runs each aligned level at the magnitude that REACHES the
    realized precision for that level — :data:`GAM_AT_REALIZED_SE_SESSION` and
    :data:`GAM_AT_REALIZED_SE_ROW`. That is the default because a power bracket is
    a BOUND: its worst corner has to be at least as bad as the one out-of-sample
    standard error this programme actually measured, or the bracket would have
    failed to warn about #1227 itself.

    The burnt-panel READINGS, :data:`MEASURED_GAM_SESSION` and
    :data:`MEASURED_GAM_ROW`, are deliberately NOT the default. They are
    estimates of the mechanism rather than magnitudes consistent with the
    realized precision, the row reading carries a t of 1.55 and is not
    significant, and the session reading is known to over-read a session truth
    (:data:`ESTIMATOR_READING_OF_A_SESSION_TRUTH`). They belong to
    :func:`measure_noise_alignment`'s report. An earlier version of this function
    ran the arms at the readings while the printed span line named the calibrated
    pair as the identified set, which made the span about four times narrower
    than it claimed to be.

    ``gam`` as a NUMBER runs both aligned arms at that one magnitude, which
    reproduces the memo's section 7.5 comparison table. ``gam`` as a MAPPING
    keyed by level sets each arm separately, which is the only way to reach a
    pair of different magnitudes such as the measured one — a single scalar
    cannot express two levels measured on two scales.

    ``shrinkage`` is carried for the record only; the caller passes already-shrunk
    ``effects``, exactly as :func:`report` does.

    There is no "cheap bracket" switch that drops an arm. The cheapening knob is
    ``n_sims``, as everywhere else in this programme — dropping a corner is the
    one shortcut that silently narrows the published span.
    """
    defaults = {
        ALIGNMENT_SESSION: GAM_AT_REALIZED_SE_SESSION,
        ALIGNMENT_ROW: GAM_AT_REALIZED_SE_ROW,
    }
    if gam is None:
        chosen = defaults
    elif isinstance(gam, Mapping):
        unknown = sorted(set(gam) - set(ALIGNMENT_LEVELS))
        if unknown:
            raise ValueError(
                f"gam mapping carries unknown levels {unknown}; the aligned corners are "
                f"{list(ALIGNMENT_LEVELS)}"
            )
        chosen = {level: gam.get(level, defaults[level]) for level in ALIGNMENT_LEVELS}
    else:
        chosen = dict.fromkeys(ALIGNMENT_LEVELS, gam)
    alignments: dict[str, NoiseAlignment | None] = {
        "homoskedastic": None,
        ALIGNMENT_SESSION: NoiseAlignment(
            chosen[ALIGNMENT_SESSION], ALIGNMENT_SESSION, feature=feature
        ),
        ALIGNMENT_ROW: NoiseAlignment(chosen[ALIGNMENT_ROW], ALIGNMENT_ROW, feature=feature),
    }
    # One seed for every arm, so the corners are PAIRED across simulations and
    # the span is a mechanism difference rather than three independent Monte
    # Carlo draws.
    arms = {
        arm: simulate_power(
            burnt_signals=burnt_signals,
            cluster_sizes=cluster_sizes,
            effects=effects,
            sd_y=sd_y,
            icc=icc,
            n_sims=n_sims,
            wcb_boot=wcb_boot,
            seed=seed,
            family_size=family_size,
            alignment=alignments[arm],
        )
        for arm in BRACKET_ARMS
    }
    return PowerBracket(
        arms=arms,
        alignments=alignments,
        target=target,
        shrinkage=shrinkage,
        n_sims=n_sims,
    )


def print_power_bracket(
    bracket: PowerBracket, *, bar: float = GATE_POWER, seed: int | None = None
) -> None:
    """The three corners, their span, and which side the span resolved on.

    Uses the :meth:`PowerBracket.is_resolved` predicate and never
    :meth:`PowerBracket.power_at_gate`, so a driver cannot raise mid-run. Both
    exist for the same reason :func:`verdict_is_resolved` (printed) sits beside
    ``FloorDoesNotBindError`` (raised when a caller reaches for a number).
    """
    header = (
        f"  ALIGNMENT BRACKET at {bracket.shrinkage:.0%} of the discovery effect, "
        f"target {bracket.target}, {bracket.n_sims} panels"
    )
    print(header if seed is None else f"{header}, seed {seed}")
    for arm in BRACKET_ARMS:
        alignment = bracket.alignments[arm]
        label = "homoskedastic" if alignment is None else f"{arm}-aligned"
        magnitude = "" if alignment is None else f"gam {alignment.gam:.3f}"
        power = bracket.arms[arm][bracket.target]
        low, high = wilson_interval(power, bracket.n_sims)
        print(
            f"    {label:<16s} {magnitude:<14s}: {bracket.target} {power:.1%}  "
            f"[95% {low:.1%}, {high:.1%}]"
        )
    low, high = bracket.span()
    print(
        f"    span {low:.1%} to {high:.1%}; the LEVEL is not identified "
        f"({REALIZED_SLOPE_SE_CR2:.4f} is reached at gam {GAM_AT_REALIZED_SE_SESSION:.3f} on "
        f"sessions and {GAM_AT_REALIZED_SE_ROW:.3f} on rows)"
    )
    if not bracket.is_resolved(bar=bar):
        print(
            f"    NO BRACKET VERDICT: the identified set crosses the {bar:.0%} bar; quote the "
            f"span, not a corner. Re-run with more than --n-sims {bracket.n_sims} if the "
            f"corners overlap."
        )
    elif low >= bar:
        print(f"    BRACKET VERDICT: every corner clears {bar:.0%}")
    else:
        # is_resolved() is also true when every corner sits BELOW the bar, and
        # calling that "clears" would invert the answer.
        print(f"    BRACKET VERDICT: no corner clears {bar:.0%}")


def load_held_out(labels_dir: Any) -> pd.DataFrame:
    """Read the label store's held-out side, allowlisted columns only."""
    from pathlib import Path

    frames = [
        pd.read_parquet(path, columns=list(HELD_OUT_COLUMNS))
        for path in sorted(Path(labels_dir).glob("*.parquet"))
        if path.stem > DISCOVERY_CUTOFF
    ]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# --------------------------------------------------------------- burnt side
#: The three #1227 hypotheses and the brief columns that carry them. These are
#: the discovery columns unchanged — re-deriving any of them would be a second
#: definition of a signal that already has one.
SIGNALS = {
    "atr": "technical_atr_pct",
    "ma50": "technical_ma50_distance_pct",
    "press": "n_gates_passed",
}

OUTCOME = "sel_ar_20"

#: Below this the burnt fit is not an estimate. Measured 2026-09-21 the real
#: panel holds 197 episodes, so this only ever fires on a broken join.
_MIN_BURNT_EPISODES = 30


def _z(values: np.ndarray) -> np.ndarray:
    sd = float(np.std(values))
    return (values - float(np.mean(values))) / sd if sd > 0 else np.zeros_like(values)


def burnt_panel(labels_dir: Any, briefs_dir: Any, *, population: str) -> pd.DataFrame:
    """Burnt-side (<= 2026-07-05) labels joined to the brief signals.

    Outcome values ARE read here, which is what the burnt side is for. The join
    key is stringified on both sides deliberately: the two stores stamp
    ``brief_date`` with different types, and a silent type mismatch would empty
    the join and hand back a clean-looking empty panel.
    """
    from pathlib import Path

    from alphalens_research.diagnostics.options_retro import ticker_episode_dedup

    label_cols = [
        "brief_date",
        "ticker",
        "anchor_session",
        "briefed_any_theme",
        "sel_label_status_20",
        OUTCOME,
    ]
    labels = pd.concat(
        [
            pd.read_parquet(path, columns=label_cols)
            for path in sorted(Path(labels_dir).glob("*.parquet"))
            if path.stem <= DISCOVERY_CUTOFF
        ],
        ignore_index=True,
    )
    briefs = pd.concat(
        [
            pd.read_parquet(path, columns=["ticker", *SIGNALS.values()]).assign(
                brief_date=path.stem
            )
            for path in sorted(Path(briefs_dir).glob("*.parquet"))
            if path.stem <= DISCOVERY_CUTOFF
        ],
        ignore_index=True,
    )

    labels["brief_date"] = labels["brief_date"].astype(str)
    labels["ticker"] = labels["ticker"].astype(str).str.upper()
    briefs["ticker"] = briefs["ticker"].astype(str).str.upper()

    panel = labels.merge(briefs, on=["brief_date", "ticker"], how="inner", validate="m:1")
    panel = panel[panel["sel_label_status_20"].astype(str) == _RESOLVED]
    if population == POPULATION_BRIEFED:
        panel = panel[panel["briefed_any_theme"].fillna(False).astype(bool)]
    elif population != POPULATION_ALL:
        raise ValueError(f"unknown population {population!r}")
    panel = panel.dropna(subset=[OUTCOME, *SIGNALS.values()])
    panel = panel.rename(columns={"anchor_session": "arrival"})
    panel["arrival"] = panel["arrival"].astype(str)
    panel = ticker_episode_dedup(panel)
    if len(panel) < _MIN_BURNT_EPISODES:
        # A join that comes back empty (or nearly so) looks exactly like a
        # clean run with no data: the fit returns zeros, the simulation runs,
        # and a power number appears. Refuse instead.
        raise ValueError(
            f"burnt panel has {len(panel)} episodes for population {population!r}; "
            f"expected at least {_MIN_BURNT_EPISODES}. Check that the two stores "
            f"agree on brief_date and ticker."
        )
    return panel


def _standardised_design(panel: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """``(z(outcome), [1, z(signal) ...])`` — the burnt fit's one definition.

    Shared by :func:`standardised_effects` and :func:`measure_noise_alignment`
    rather than written twice: a second definition of the burnt fit is how two
    published numbers come to disagree.
    """
    y = _z(panel[OUTCOME].astype(float).to_numpy())
    X = np.column_stack(
        [np.ones(len(panel))]
        + [_z(panel[col].astype(float).to_numpy()) for col in SIGNALS.values()]
    )
    return y, X


def standardised_effects(panel: pd.DataFrame) -> dict[str, float]:
    """The three partial slopes of z(outcome) on z(signal), jointly.

    Standardised on both sides so a slope reads as a correlation and the
    25/50/75% shrinkage grid means what it says. Fitted jointly rather than one
    at a time because the July memo's own verdict for MA50 and the press gate is
    the ATR-partial one — a marginal slope would re-import the confound the
    discovery already controlled for.
    """
    y, X = _standardised_design(panel)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return {name: float(beta[i + 1]) for i, name in enumerate(SIGNALS)}


@dataclasses.dataclass(frozen=True)
class AlignmentEstimate:
    """Both levels' readings, together. Neither can be read without the other."""

    feature: str
    session_gam: float | None
    session_se: float | None
    session_t: float | None
    session_points: int
    row_gam: float
    row_se: float
    row_t: float
    row_points: int


def precision_disclosure() -> dict[str, Any]:
    """What every power figure this module prints is conditional on.

    Written into the JSON artifact as well as the human output, because the
    artifact is what a memo quotes and a silent artifact defeats the point.
    """
    return {
        "memo": ALIGNMENT_MEMO,
        "simulated_slope_se_published": SIMULATED_SLOPE_SE_PUBLISHED,
        "realized_slope_se_cr2": REALIZED_SLOPE_SE_CR2,
        "optimism_ratio": SIMULATOR_OPTIMISM_RATIO,
        "homoskedastic_default": (
            "the default DGP gives every observation ONE noise scale, which is the "
            "assumption measured to be optimistic by the ratio above"
        ),
        "level_not_identified": (
            f"the realized precision is reached at gam {GAM_AT_REALIZED_SE_SESSION:.3f} on "
            f"sessions and {GAM_AT_REALIZED_SE_ROW:.3f} on rows; the burnt panel does not "
            f"choose between the two levels, so a single aligned power figure is not "
            f"identified"
        ),
        "projections_are_not_bracketed": (
            "the cluster-count search and the gate DATE are computed on the "
            "homoskedastic corner only; --alignment-bracket does not move them"
        ),
    }


def _ols_slope(y: np.ndarray, x: np.ndarray) -> tuple[float, float, float]:
    """Slope of ``y`` on ``x`` with an intercept: ``(slope, se, t)``."""
    design = np.column_stack([np.ones(len(x)), x])
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    resid = y - design @ beta
    dof = len(x) - 2
    sxx = float(np.sum((x - float(np.mean(x))) ** 2))
    if dof <= 0 or sxx <= 0:
        return float(beta[1]), float("nan"), float("nan")
    sigma2 = float(resid @ resid) / dof
    se = math.sqrt(sigma2 / sxx)
    return float(beta[1]), se, (float(beta[1]) / se if se > 0 else float("nan"))


def measure_noise_alignment(
    panel: pd.DataFrame,
    *,
    feature: str = ALIGNMENT_FEATURE,
    min_episodes_per_session: int = MIN_EPISODES_PER_SESSION_FOR_GAM,
) -> AlignmentEstimate:
    """How far the burnt panel's noise ALIGNS with ``feature``, at both levels.

    Returns BOTH levels because this panel does not identify which one is real.
    The slope of log(per-session residual sd) on z(per-session mean feature)
    measures a session-level mechanism, but it does not identify the level:
    simulated on the real burnt design with 1500 panels per row, a ROW-level
    truth of 0.360 reads back as +0.240 and is significant at ``|t| > 1.96`` in
    72.4% of panels, so a positive, significant session reading is not evidence
    for the session level.

    It also OVER-READS a session-level truth: a known session truth of 0.360
    reads back as +0.419 on average (sd 0.091). And it is slightly liberal under
    the null — 9.0% of homoskedastic panels read significant against a nominal
    5%. Measured 2026-11-07, ``a20_simulator_precision_gap_2026_11_07.md``
    section 7.5.

    The two readings disagree about magnitude by roughly a factor of two once
    they are turned into a simulator calibration: the realized CR2 standard error
    of 0.0731 is reached at gam 0.400 on sessions and 0.205 on rows. A
    registration quoting its own feature's alignment must quote the span, not one
    reading.

    This is NOT ``a20_overlap_power.estimate_signal_loading`` (kappa) and the two
    must not be substituted for each other. Kappa fits
    ``|residual| = a + b*z(signal)`` and returns ``b / a``, a LINEAR widening
    applied only to the SHARED overlap component. ``gam`` is the slope of a LOG
    spread, an exponential widening applied to the WHOLE residual, and it comes
    in two levels. The row arm here is the closest relative of kappa and still a
    different link function.

    Reads the BURNT panel only — the frame :func:`burnt_panel` returns. It never
    globs the label store and never compares a date against
    :data:`DISCOVERY_CUTOFF`, so it cannot reach a held-out row;
    ``tests/test_a20_power_preflight.py`` asserts that over the module's source.
    """
    if feature not in SIGNALS:
        raise ValueError(f"unknown feature {feature!r}; the hypotheses are {list(SIGNALS)}")
    column = SIGNALS[feature]

    y, X = _standardised_design(panel)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    z_feature = _z(panel[column].astype(float).to_numpy())

    row_gam, row_se, row_t = _ols_slope(np.log(np.abs(resid) + _LOG_ABS_FLOOR), z_feature)

    arrival = panel["arrival"].astype(str).to_numpy()
    # Sessions enter UNWEIGHTED, matching the section 7.3 construction. Weighting
    # by episode count would be a different estimator and a different number.
    session_sd, session_mean = [], []
    for session in sorted(set(arrival)):
        rows = arrival == session
        if int(np.count_nonzero(rows)) < min_episodes_per_session:
            continue
        session_sd.append(float(np.std(resid[rows], ddof=1)))
        session_mean.append(float(np.mean(z_feature[rows])))

    points = len(session_sd)
    if points < MIN_SESSIONS_FOR_GAM_SLOPE:
        # INSUFFICIENT, not estimated. Reported, not dropped — the doctrine
        # `volatility_sufficiency` sets in the frozen #1227 registration. The
        # real surviving count is handed back so the printer can say how far
        # short it fell.
        return AlignmentEstimate(
            feature=feature,
            session_gam=None,
            session_se=None,
            session_t=None,
            session_points=points,
            row_gam=row_gam,
            row_se=row_se,
            row_t=row_t,
            row_points=len(resid),
        )

    session_gam, session_se, session_t = _ols_slope(
        np.log(np.asarray(session_sd) + _LOG_ABS_FLOOR), _z(np.asarray(session_mean))
    )
    return AlignmentEstimate(
        feature=feature,
        session_gam=session_gam,
        session_se=session_se,
        session_t=session_t,
        session_points=points,
        row_gam=row_gam,
        row_se=row_se,
        row_t=row_t,
        row_points=len(resid),
    )


def print_alignment_estimate(estimate: AlignmentEstimate) -> None:
    """The burnt-panel reading, with the non-identification stated beside it."""
    print(
        f"  BURNT-PANEL ALIGNMENT of the noise with {estimate.feature} "
        f"(both levels; neither identifies the other)"
    )
    if estimate.session_gam is None:
        print(
            f"    session: {estimate.session_points} sessions with "
            f">={MIN_EPISODES_PER_SESSION_FOR_GAM} episodes - INSUFFICIENT, not estimated "
            f"(floor {MIN_SESSIONS_FOR_GAM_SLOPE}). Reported, not dropped."
        )
    else:
        print(
            f"    session: gam {estimate.session_gam:+.3f}  se {estimate.session_se:.3f}  "
            f"t {estimate.session_t:+.2f}  ({estimate.session_points} sessions with "
            f">={MIN_EPISODES_PER_SESSION_FOR_GAM} episodes)"
        )
    print(
        f"    row:     gam {estimate.row_gam:+.3f}  se {estimate.row_se:.3f}  "
        f"t {estimate.row_t:+.2f}  ({estimate.row_points} episodes)"
    )
    # The published reading beside this panel's own, so a number that drifted
    # from the memo's is visible rather than quietly replacing it.
    print(
        f"    memo 2026-11-07, burnt panel: session gam {MEASURED_GAM_SESSION:+.3f} se "
        f"{MEASURED_GAM_SESSION_SE:.3f} ({MEASURED_GAM_SESSION_POINTS} sessions); "
        f"row gam {MEASURED_GAM_ROW:+.3f} se {MEASURED_GAM_ROW_SE:.3f} "
        f"({MEASURED_GAM_ROW_POINTS} episodes)"
    )
    print(
        f"    NOT IDENTIFIED: a row-level truth of {MEASURED_GAM_SESSION:.2f} reads "
        f"{ESTIMATOR_READING_OF_A_ROW_TRUTH:+.3f} on the session arm and is significant in "
        f"72.4% of panels; a session truth of {MEASURED_GAM_SESSION:.2f} is OVER-READ as "
        f"{ESTIMATOR_READING_OF_A_SESSION_TRUTH:+.3f}; the session arm is liberal under the "
        f"null ({ESTIMATOR_SIZE_UNDER_HOMOSKEDASTICITY:.1%} at a nominal 5%). "
        f"({ALIGNMENT_MEMO} section 7.5)"
    )


def effect_ci(
    panel: pd.DataFrame, *, n_boot: int = 999, seed: int = 20260922
) -> dict[str, tuple[float, float]]:
    """Cluster-bootstrap 90% interval for each standardised effect.

    Resamples ARRIVAL SESSIONS, not rows: the dependence that matters is
    same-day co-movement, and a row bootstrap would report an interval far too
    tight and let one point estimate carry the whole gate.
    """
    rng = np.random.default_rng(seed)
    clusters = panel["arrival"].to_numpy()
    groups = [panel.iloc[np.flatnonzero(clusters == c)] for c in pd.unique(clusters)]
    draws: dict[str, list[float]] = {name: [] for name in SIGNALS}
    for _ in range(n_boot):
        picked = rng.integers(0, len(groups), len(groups))
        sample = pd.concat([groups[i] for i in picked], ignore_index=True)
        for name, value in standardised_effects(sample).items():
            draws[name].append(value)
    return {
        name: (float(np.percentile(v, 5)), float(np.percentile(v, 95))) for name, v in draws.items()
    }


# ------------------------------------------------------------------- driver
def report(
    *,
    labels_dir: Any,
    briefs_dir: Any,
    population: str,
    n_sims: int,
    wcb_boot: int,
    seed: int,
    max_clusters: int = 200,
    family_size: int | None = None,
    alignment_bracket: bool = False,
    alignment_gam: float | None = None,
    alignment_feature: str = ALIGNMENT_FEATURE,
    measure_alignment: bool = False,
) -> dict[str, Any]:
    """One population's answer: effects, power at each shrinkage, and the date.

    ``family_size`` is carried into the result so a memo quoting a power figure
    can never lose track of which multiplicity family produced it.

    There is deliberately no ``alignment`` parameter. One level is not identified
    (:data:`ALIGNMENT_MEMO` section 7.5), so the only aligned answer this
    function will produce is the three-corner :class:`PowerBracket` that
    ``alignment_bracket`` asks for.
    """
    burnt = burnt_panel(labels_dir, briefs_dir, population=population)
    effects = standardised_effects(burnt)
    cis = effect_ci(burnt, seed=seed)

    held = load_held_out(labels_dir)

    # One read for the whole panel. The sizes, the cluster count and the accrual
    # rate must describe the same episodes on the same population; deriving the
    # accrual rate from raw rows instead counted arrival sessions that hold only
    # a chained repeat of an earlier episode.
    by_anchor = held_out_episodes_by_arrival(held, population=population)
    sizes = list(by_anchor.values())
    accrual = measured_accrual(by_anchor)

    # The arrival LIST is deliberately wider than the matured panel: it dates
    # the gate off every session the brief has reached, matured or not.
    in_population = population_mask(held, population)
    anchors = held["anchor_session"].astype(str).to_numpy()
    observed_arrivals = sorted({a[:10] for a in anchors[in_population]})

    y_b = burnt[OUTCOME].astype(float).to_numpy()
    sd_y = float(np.std(y_b))
    icc = estimate_icc_local(y_b, burnt["arrival"].to_numpy())
    signals = np.column_stack([_z(burnt[c].astype(float).to_numpy()) for c in SIGNALS.values()])

    powers: dict[float, dict[str, float]] = {}
    for factor in SHRINKAGE_GRID:
        powers[factor] = simulate_power(
            burnt_signals=signals,
            cluster_sizes=sizes,
            effects={n: shrink(effects[n], factor) * sd_y for n in SIGNALS},
            sd_y=sd_y,
            icc=icc,
            n_sims=n_sims,
            wcb_boot=wcb_boot,
            seed=seed,
            family_size=family_size,
        )

    needed = None
    gate_when = None
    table: dict[int, dict[str, float]] = {}
    cluster_dates: dict[int, dt.date] = {}
    if powers[GATE_SHRINKAGE]["atr"] < GATE_POWER:
        needed, table = clusters_for_power(
            burnt_signals=signals,
            observed_sizes=sizes,
            effects={n: shrink(effects[n], GATE_SHRINKAGE) * sd_y for n in SIGNALS},
            sd_y=sd_y,
            icc=icc,
            n_sims=max(n_sims // 2, 60),
            wcb_boot=wcb_boot,
            seed=seed,
            max_clusters=max_clusters,
            family_size=family_size,
        )
        if needed is not None and (accrual > 0 or len(observed_arrivals) >= needed):
            gate_when = gate_date(
                observed_arrivals=observed_arrivals,
                clusters_needed=needed,
                accrual_per_session=accrual,
            )
            for count in table:
                if accrual > 0 or len(observed_arrivals) >= count:
                    cluster_dates[count] = gate_date(
                        observed_arrivals=observed_arrivals,
                        clusters_needed=count,
                        accrual_per_session=accrual,
                    )

    bracket = None
    if alignment_bracket:
        bracket = power_bracket(
            burnt_signals=signals,
            cluster_sizes=sizes,
            effects={n: shrink(effects[n], GATE_SHRINKAGE) * sd_y for n in SIGNALS},
            sd_y=sd_y,
            icc=icc,
            n_sims=n_sims,
            wcb_boot=wcb_boot,
            seed=seed,
            family_size=family_size,
            target=alignment_feature,
            feature=alignment_feature,
            gam=alignment_gam,
        )

    # The estimate reads the panel already in hand, so --measure-alignment costs
    # one fit and never a second trip to the store.
    estimate = (
        measure_noise_alignment(burnt, feature=alignment_feature) if measure_alignment else None
    )

    return {
        "alignment_bracket": bracket,
        "alignment_estimate": estimate,
        "clusters_needed_for_gate": needed,
        "gate_date": gate_when,
        "power_by_cluster_count": table,
        "date_by_cluster_count": cluster_dates,
        "search_sims": max(n_sims // 2, 60),
        "family_size": family_size if family_size is not None else len(SIGNALS),
        "population": population,
        "burnt_episodes": len(burnt),
        "held_out_clusters": len(sizes),
        "held_out_episodes": int(sum(sizes)),
        "accrual_per_session": accrual,
        "sd_y": sd_y,
        "icc": icc,
        "effects": effects,
        "effect_ci": cis,
        "power": powers,
    }


def estimate_icc_local(y: np.ndarray, clusters: np.ndarray) -> float:
    """One-way ANOVA ICC, clamped to [0, 0.9]. Same estimator as the neighbour."""
    frame = pd.DataFrame({"y": y, "c": clusters})
    groups = [g["y"].to_numpy() for _, g in frame.groupby("c") if len(g) > 0]
    k, n = len(groups), sum(len(g) for g in groups)
    if k < 2 or n <= k:
        return 0.0
    grand = np.concatenate(groups).mean()
    ssb = sum(len(g) * (g.mean() - grand) ** 2 for g in groups)
    ssw = sum(((g - g.mean()) ** 2).sum() for g in groups)
    msb, msw = ssb / (k - 1), ssw / (n - k)
    n0 = (n - sum(len(g) ** 2 for g in groups) / n) / (k - 1)
    icc = (msb - msw) / (msb + (n0 - 1) * msw) if msw > 0 else 0.0
    return float(min(max(icc, 0.0), 0.9))


def clusters_for_power(
    *,
    burnt_signals: np.ndarray,
    observed_sizes: list[int],
    effects: dict[str, float],
    sd_y: float,
    icc: float,
    target: str = "atr",
    target_power: float = GATE_POWER,
    n_sims: int,
    wcb_boot: int,
    seed: int,
    max_clusters: int = 200,
    family_size: int | None = None,
) -> tuple[int | None, dict[int, dict[str, float]]]:
    """First cluster count reaching ``target_power``, AND every count it tried.

    Returning the table is what lets the memo quote the search's own numbers
    rather than a second computation that merely ought to agree with it. The
    search steps by :data:`_SEARCH_STEP`, so the answer is the first count ON
    THE GRID that clears, not the true minimum.

    Extra clusters are drawn from the OBSERVED size distribution rather than
    given an average size: the panel's clusters are far from uniform, and
    pretending they are would make the projection optimistic in exactly the
    direction that matters.

    Takes NO ``alignment``, so the cluster search and its table are the
    homoskedastic corner. If alignment is ever threaded in here it goes into
    :func:`power_at_cluster_counts` in the SAME commit: the two are compared to
    12 places by ``TestTheSearchAndTheTableAreOneComputation``, so changing one
    alone turns that test red for the right reason and the memo's table and
    verdict stop being one computation.
    """
    seen: dict[int, dict[str, float]] = {}
    n = len(observed_sizes)
    while n <= max_clusters:
        power = simulate_power(
            burnt_signals=burnt_signals,
            cluster_sizes=grown_cluster_sizes(observed_sizes, n, seed=seed),
            effects=effects,
            sd_y=sd_y,
            icc=icc,
            n_sims=n_sims,
            wcb_boot=wcb_boot,
            seed=seed,
            family_size=family_size,
        )
        seen[n] = power
        if power[target] >= target_power:
            return n, seen
        n += _SEARCH_STEP
    return None, seen


def grown_cluster_sizes(observed_sizes: list[int], n_clusters: int, *, seed: int) -> list[int]:
    """``n_clusters`` cluster sizes: the observed ones plus draws from them.

    Seeded per call, so the same cluster count always yields the same shape.
    An unseeded generator advancing across a search made two runs at the same
    count disagree, which is not something a memo table can be built on.
    """
    if n_clusters <= len(observed_sizes):
        return list(observed_sizes[:n_clusters])
    rng = np.random.default_rng(seed + n_clusters)
    extra = rng.integers(0, len(observed_sizes), n_clusters - len(observed_sizes))
    return list(observed_sizes) + [int(observed_sizes[i]) for i in extra]


def power_at_cluster_counts(
    *,
    burnt_signals: np.ndarray,
    observed_sizes: list[int],
    effects: dict[str, float],
    sd_y: float,
    icc: float,
    counts: list[int],
    n_sims: int,
    wcb_boot: int,
    seed: int,
) -> dict[int, dict[str, float]]:
    """Power at each named cluster count — the table the memo quotes."""
    return {
        n: simulate_power(
            burnt_signals=burnt_signals,
            cluster_sizes=grown_cluster_sizes(observed_sizes, n, seed=seed),
            effects=effects,
            sd_y=sd_y,
            icc=icc,
            n_sims=n_sims,
            wcb_boot=wcb_boot,
            seed=seed,
        )
        for n in counts
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
    parser.add_argument("--seed", type=int, default=20260922)
    parser.add_argument("--max-clusters", type=int, default=200)
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument(
        "--family-size",
        type=int,
        default=None,
        help=(
            "multiplicity family: 1 for the registered ATR-only test, "
            f"{len(SIGNALS)} (the default) for Holm across all three"
        ),
    )
    # One population per process is how this gets run on a machine with cores
    # to spare: the two arms share no state, so splitting them halves the wall
    # time and changes nothing about the numbers.
    parser.add_argument(
        "--population",
        choices=[POPULATION_BRIEFED, POPULATION_ALL],
        action="append",
        default=None,
        help="repeatable; default is both",
    )
    parser.add_argument(
        "--alignment-bracket",
        action="store_true",
        help=(
            "also print power at all three corners of the identified set "
            "(homoskedastic, session-aligned, row-aligned) at the gate shrinkage"
        ),
    )
    parser.add_argument(
        "--alignment-gam",
        type=float,
        default=None,
        help=(
            "run BOTH aligned arms of the bracket at this magnitude instead of each "
            "level's own measured value; reproduces the memo's section 7.5 table"
        ),
    )
    parser.add_argument(
        "--alignment-feature",
        choices=tuple(SIGNALS),
        default=ALIGNMENT_FEATURE,
        help="which hypothesis the noise aligns with (default: %(default)s)",
    )
    parser.add_argument(
        "--measure-alignment",
        action="store_true",
        help="print the burnt-panel alignment estimate, at both levels, with its se",
    )
    args = parser.parse_args(argv)

    # Argument errors, caught where --ci-boot's are in the operating-characteristics
    # script: before the burnt read, so a wrong combination costs no minutes.
    if args.alignment_gam is not None:
        if not args.alignment_bracket:
            parser.error(
                "--alignment-gam sets the magnitude of the bracket arms and has no effect "
                "without --alignment-bracket; a single aligned power figure is not identified "
                f"({ALIGNMENT_MEMO} section 7.5)"
            )
        if not math.isfinite(args.alignment_gam) or args.alignment_gam <= 0:
            parser.error(
                f"--alignment-gam must be a finite, strictly positive magnitude, got "
                f"{args.alignment_gam}. A zero magnitude is the homoskedastic corner, not a "
                f"second arm: it makes both aligned corners bit-identical to it, the span "
                f"zero-wide, and the one scalar accessor hand back the corner measured to be "
                f"{SIMULATOR_OPTIMISM_RATIO}x optimistic."
            )
    if args.alignment_feature != ALIGNMENT_FEATURE and not (
        args.alignment_bracket or args.measure_alignment
    ):
        parser.error(
            "--alignment-feature has no consumer without --alignment-bracket or --measure-alignment"
        )

    out = {}
    for population in args.population or [POPULATION_BRIEFED, POPULATION_ALL]:
        r = report(
            labels_dir=args.labels_dir,
            briefs_dir=args.briefs_dir,
            population=population,
            n_sims=args.n_sims,
            wcb_boot=args.wcb_boot,
            seed=args.seed,
            max_clusters=args.max_clusters,
            family_size=args.family_size,
            alignment_bracket=args.alignment_bracket,
            alignment_gam=args.alignment_gam,
            alignment_feature=args.alignment_feature,
            measure_alignment=args.measure_alignment,
        )
        out[population] = r
        print(f"\n=== population: {population} ===  family of {r['family_size']}")
        print(
            f"burnt episodes {r['burnt_episodes']} | held-out {r['held_out_episodes']} episodes "
            f"in {r['held_out_clusters']} clusters | accrual {r['accrual_per_session']:.2f}/session "
            f"| sd(y) {r['sd_y']:.4f} icc {r['icc']:.2f}"
        )
        for name in SIGNALS:
            lo, hi = r["effect_ci"][name]
            print(
                f"  burnt A20 effect {name:5s}: {r['effects'][name]:+.3f}  [90% {lo:+.3f}, {hi:+.3f}]"
            )
        for factor in SHRINKAGE_GRID:
            row = "  ".join(f"{n}={r['power'][factor][n]:.0%}" for n in SIGNALS)
            mark = "  <-- the gate" if factor == GATE_SHRINKAGE else ""
            print(f"  power @ {factor:.0%} of the discovery effect: {row}{mark}")
        atr_gate = r["power"][GATE_SHRINKAGE]["atr"]
        se = monte_carlo_se(atr_gate, args.n_sims)
        print(
            f"  VERDICT: ATR power at the gate = {atr_gate:.1%} +/-{se:.1%} "
            f"(needs {GATE_POWER:.0%})"
        )
        if not verdict_is_resolved(atr_gate, args.n_sims):
            print(
                f"  NO VERDICT: the Monte Carlo interval straddles the bar. "
                f"Re-run with more than --n-sims {args.n_sims}."
            )
        # Printed on EVERY default run, including the reproductions of the
        # published figures, and it moves no number. This is what makes the
        # optimistic path hard to take by accident.
        print(
            f"  *** this is the HOMOSKEDASTIC corner. Measured 2026-11-07, this simulator is\n"
            f"      {SIMULATOR_OPTIMISM_RATIO:.2f}x optimistic on the slope standard error "
            f"({SIMULATED_SLOPE_SE_PUBLISHED:.4f} simulated against a\n"
            f"      realized {REALIZED_SLOPE_SE_CR2:.4f}) because the real noise ALIGNS with "
            f"the feature under\n"
            f"      test. Re-run with --alignment-bracket for the identified set.\n"
            f"      ({ALIGNMENT_MEMO})"
        )
        if r["clusters_needed_for_gate"] and r["gate_date"] is not None:
            print(
                f"  needs ~{r['clusters_needed_for_gate']} clusters (has {r['held_out_clusters']}) "
                f"-> gate reached about {r['gate_date']}"
            )
        elif r["clusters_needed_for_gate"]:
            # A count without a date: too few arrival sessions on record to
            # measure an accrual rate, so there is nothing to project from.
            print(
                f"  needs ~{r['clusters_needed_for_gate']} clusters "
                f"(has {r['held_out_clusters']}) -> no date: accrual not measurable"
            )
        elif atr_gate < GATE_POWER:
            print("  the gate is not reached within the search ceiling")
        for count, row in sorted(r["power_by_cluster_count"].items()):
            lo, hi = wilson_interval(row["atr"], r["search_sims"])
            when = r["date_by_cluster_count"].get(count, "-")
            print(
                f"    {count:3d} clusters: atr {row['atr']:.1%} "
                f"[95% {lo:.1%}, {hi:.1%}] reached {when}"
            )
        # Last, so the verdict, the cluster count and the cluster table stay one
        # contiguous block: these two are additional readings, not part of it.
        if r["alignment_bracket"] is not None:
            print_power_bracket(r["alignment_bracket"], seed=args.seed)
        if r["alignment_estimate"] is not None:
            print_alignment_estimate(r["alignment_estimate"])

    if args.out_json:
        # The machine-readable artifact is the one a memo or a ledger row quotes,
        # so it carries the disclosure too. A power figure recorded here without
        # it is exactly the accident this module exists to prevent: the human
        # output prints the warning and the JSON would have been silent.
        #
        # It goes INSIDE each population's record, not beside them. The top level
        # is keyed by population and consumers iterate it, so a sibling key would
        # read as a third population (`TestThePopulationFlag` pins that contract).
        # The cost is one duplicated block per population; the benefit is that
        # every recorded figure carries its condition locally.
        disclosure = precision_disclosure()
        for record in out.values():
            record[PRECISION_DISCLOSURE_KEY] = disclosure

        # Dataclasses are written as STRUCTURED data, not as a stringified repr:
        # `asdict` recurses into the per-arm NoiseAlignment values, so a recorded
        # bracket keeps the magnitude that produced each corner. Everything else
        # (dates) still falls through to str.
        def _as_json(value: Any) -> Any:
            if dataclasses.is_dataclass(value) and not isinstance(value, type):
                return dataclasses.asdict(value)
            return str(value)

        args.out_json.write_text(json.dumps(out, indent=2, default=_as_json) + "\n")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
