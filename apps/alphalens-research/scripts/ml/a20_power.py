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
"""

from __future__ import annotations

import datetime as dt
import math
import re
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
    from alphalens_pipeline.paper.calendar import trading_days_elapsed

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
    from alphalens_pipeline.paper.calendar import advance_trading_sessions

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


def simulate_panel(
    *,
    burnt_signals: np.ndarray,
    cluster_sizes: list[int],
    effects: dict[str, float],
    sd_y: float,
    icc: float,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One simulated panel: ``(y, X, clusters)``.

    Split out of :func:`simulate_power` so a test can measure the spread of the
    outcome it produces. That is the check that would have caught the variance
    bug: assert ``std(y)`` against the ``sd_y`` the panel was calibrated to.

    ``icc`` is applied to the RESIDUAL variance. It was estimated on the real
    outcome, which carries the signal's own cluster structure, so some
    within-day dependence is counted twice — that direction costs power rather
    than manufacturing it, which is the safe way round for a gate.
    """
    names = list(effects)
    var_resid = residual_variance(burnt_signals, effects, sd_y)
    var_u = icc * var_resid
    var_e = (1.0 - icc) * var_resid

    xs, ys, cls = [], [], []
    for j, m in enumerate(cluster_sizes):
        idx = rng.integers(0, len(burnt_signals), m)
        x = burnt_signals[idx]  # (m, k) REAL rows, correlation intact
        u = rng.normal(0.0, math.sqrt(var_u))
        e = rng.normal(0.0, math.sqrt(var_e), m)
        signal = sum(effects[name] * x[:, i] for i, name in enumerate(names))
        ys.append(signal + u + e)
        xs.append(x)
        cls.append(np.full(m, j))
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
) -> dict[str, float]:
    """Power for each hypothesis at the given injected effects.

    ``family_size`` picks the multiplicity family; see
    :func:`rejected_under_family`. The default is Holm across all of them,
    which is what every merged number was computed under.

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


def standardised_effects(panel: pd.DataFrame) -> dict[str, float]:
    """The three partial slopes of z(outcome) on z(signal), jointly.

    Standardised on both sides so a slope reads as a correlation and the
    25/50/75% shrinkage grid means what it says. Fitted jointly rather than one
    at a time because the July memo's own verdict for MA50 and the press gate is
    the ATR-partial one — a marginal slope would re-import the confound the
    discovery already controlled for.
    """
    y = _z(panel[OUTCOME].astype(float).to_numpy())
    X = np.column_stack(
        [np.ones(len(panel))]
        + [_z(panel[col].astype(float).to_numpy()) for col in SIGNALS.values()]
    )
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return {name: float(beta[i + 1]) for i, name in enumerate(SIGNALS)}


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
) -> dict[str, Any]:
    """One population's answer: effects, power at each shrinkage, and the date.

    ``family_size`` is carried into the result so a memo quoting a power figure
    can never lose track of which multiplicity family produced it.
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

    return {
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
    args = parser.parse_args(argv)

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

    if args.out_json:
        args.out_json.write_text(json.dumps(out, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
