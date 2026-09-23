"""ATR held-out confirmation — the registered ONE-SHOT look on ledger cluster 1.

Fills the #1227 slot of `docs/research/edge_hypothesis_budget_2026_07.md` §4.
Cluster 1 has spent 2 of its lifetime looks (June sweep, July re-run, both
DISCOVERY). This is the single confirmatory look required by ledger rule 3
before ATR may enter brief SELECTION or ORDERING. Ledger rule 4 makes it
terminal: a cluster that clears discovery and then fails its held-out
confirmation is retired and is NOT re-tested. Companion memo with the full
rationale, the operating characteristics and the limitations:
`docs/research/atr_confirmation_prereg_2026_09.md`.

REGISTRATION (frozen 2026-09-23, BEFORE any held-out feature-vs-outcome
statistic was computed). Everything read on the held-out side to date is
outcome-blind and disclosed in the memo: episode and cluster counts, label
STATUS values, and the population flag. No held-out `sel_ar_20` value has been
read. `--run` refuses to execute before RUN_NOT_BEFORE without an explicit
`--override-run-date`, which is a logged protocol deviation; the gap exists so
this registration is merged before the look, never alongside it.

WHY IT MAY RUN NOW
The unblocking condition #1227 wrote for itself is ">= 80% power at 50% of the
July discovery effect for the ATR test". Measured outcome-blind and recorded in
`a20_power_preflight_2026_09.md`: 83.9% under a three-member Holm family, 92.9%
at the family of one this registration uses. The inference method's SIZE was
then verified on an overlap-aware simulator
(`a20_overlap_inference_2026_09.md`): 4.0-7.1% against a nominal 5%, worst
corner 7.125% with a 95% interval [6.87, 7.38] under a pre-specified 7.5% bar.
Both pre-run data-integrity gates cleared: mixed price adjustment
(`atr_split_adjustment_gate_2026_09.md`, premise withdrawn - both sources are
split-adjusted) and the yfinance stale-OHLCV cache census (three logged
fallbacks, all in the burnt window, none in the panel).

FAMILY = 1. ATR only. MA50 extension (69% power) and the press gate (5% power,
a burnt effect of +0.026 whose interval straddles zero) are NOT in this look and
keep their ledger looks unspent. No Holm correction applies to a family of one;
the bar is the plain one-sided 0.05.

PANEL (frozen)
- Label store `sel_ar_20` rows with `brief_date > 2026-07-05` (the discovery
  freeze), joined to `thematic_briefs` on (brief_date, ticker).
- Population: BRIEFED only (`briefed_any_theme`), pre-committed in
  `a20_power_preflight_2026_09.md` §2.1 before any power curve existed.
  Discovery found that ATR predicts outcomes AMONG BRIEFED NAMES; confirming on
  names the brief did not take is a different claim wearing the same label. The
  wider LLM-proposed arm is better powered, which is exactly why this choice
  could not be made after seeing the curves.
- Maturity: `sel_label_status_20 == "ok"`. A row whose horizon did not resolve
  is excluded, whatever the reason (`split_guard`, `split_unchecked`, missing
  price). Counts per status are printed, because an excluded row is a
  disclosure, not a detail.
- Unit: ticker-episode (ledger rule 5), `ticker_episode_dedup`, chained
  5-session collapse. Clusters are ARRIVAL SESSIONS throughout.
- ORDER: the status and completeness filters run BEFORE the episode collapse,
  so an episode is represented by its first USABLE row rather than by a row
  that was dropped. The order is deliberate and is the one `a20_power.py`
  already used to read the panel structure the power gate was computed on;
  changing it here would measure a different panel from the one that cleared
  the gate. It has a known cost: dropping a middle row can split one chained
  episode in two, which counts a ticker twice. Recorded rather than fixed.

ESTIMAND (frozen)
The standardised partial slope of z(`technical_atr_pct`) on z(`sel_ar_20`),
fitted JOINTLY with `technical_ma50_distance_pct` and `n_gates_passed` as
covariates. Jointly, because the July discovery verdict for ATR is the
partialled one; a marginal slope would re-import a confound discovery already
controlled for. Episode-weighted, one vote per episode. No winsorization -
discovery reported a rank statistic, and adding a trim here would be a new
choice invisible in the comparison. Signals are the discovery columns
unchanged; re-deriving any of them would be a second definition.

TEST (frozen)
One-sided restricted wild cluster bootstrap on arrival-session clusters,
alternative "less" (the direction is fixed from discovery: higher ATR, worse
outcome), alpha 0.05, B = 9999, seed 20260924.

THE SMALLEST ACTIONABLE EFFECT
DELTA = |rho| 0.10, frozen as an owner decision on 2026-09-23. On this
estimand's own scale it means 1.65 percentage points of 20-session
market-adjusted return per one standard deviation of ATR (DELTA x sd(y) 0.1653);
a realistic tilt that drops the top ATR quintile moves the book's mean z(ATR) by
about 0.4 sd, so roughly 0.66 pp per episode. It is stated on this scale
deliberately: the sibling registration `2026_09_experts_last_look.py` froze the
same 0.10 for an ATR-partialled SPEARMAN, which is NOT the same statistic as a
standardised partial slope, so the number stands on its own economic legs here
and the sibling is corroboration rather than the source.

VERDICT (computed and printed by code, never by the analyst)
PROMOTE iff one-sided p < 0.05 AND the point estimate <= -0.10. Otherwise
RETIRE.

WHAT A PASS DOES AND DOES NOT PROVE - stated here, before the run, because the
honest reading is not the flattering one. The binding condition is the FLOOR,
not the p-value: at the measured precision the one-sided significance boundary
is about -0.091, so any estimate that clears -0.10 is already significant. A
PASS therefore means "the held-out estimate crossed the actionable line with a
slope of the registered sign". It does NOT mean "the true effect is at least
0.10 in magnitude"; that claim needs a shifted null (H0: rho >= -0.10), which
has about 49% power on this panel and was rejected for that reason. The
stricter form was rejected in the other direction too: it would retire a true
effect of -0.15 about 60% of the time, and under rule 4 that retirement is
permanent.

OPERATING CHARACTERISTICS, disclosed in advance (normal approximation at the
implied SE 0.0552, derived from the recorded power curve rather than measured
directly; the memo says so):

    true |rho|   0     0.05   0.10   0.15   0.189   0.25
    P(promote)   3.5%  18%    50%    82%    95%     99.7%

The programme accepts those numbers. At exactly the threshold the decision is a
coin flip whatever the sample size, which is a property of any threshold rule
and not a defect of this one.

CONCLUSION LANGUAGE (pre-committed three-way, on the 90% two-sided
cluster-bootstrap interval - the TOST-consistent level for a one-sided 0.05
test; a 95% interval would make arm (iii) unreachable by construction):
  (i) cleared -> evidence of association under the registered estimand;
 (ii) not cleared but the interval still includes |rho| >= 0.10 ->
      "inconclusive; cluster retired OPERATIONALLY, not scientifically
      falsified";
(iii) the interval lies entirely inside (-0.10, +0.10) -> evidence against
      actionable effects of this signal, as instrumented.
DISCLOSED IN ADVANCE: arm (iii) needs a point estimate inside about
(-0.009, +0.009) and so has roughly a 13% chance even if the true effect is
exactly zero. In practice this three-way will almost always land on (i) or (ii).
Saying that now is the difference between a limitation and an excuse.

WHAT RETIREMENT DOES NOT TOUCH. Cluster 1 retiring closes ATR for brief
SELECTION and ORDERING under ledger §3. ATR's use in execution geometry (the
bracket and stop sizing, the registered `atr_bracket_1p5` lenses) is a different
estimand on a different budget (§4.1) and is untouched by any outcome here.

THE LIVE SCORER TILT - the July kill line, folded in. The live scorer carries
`selection_score = layer4_weighted_score - atr_penalty(technical_atr_pct)`
under `SCORER_CONFIG_VERSION = "scorer-v1-atrtilt-lam1.0-lo5.77-hi8.37"`.
`selection_score_v2_ext_tilt_decision_2026_07_06.md` §5 registered a kill line
for it whose trigger is a fresh-data ATR effect of the WRONG sign. The
2026-09-17 decision on #1227 folded that line into this look so it cannot spend
the same held-out episodes twice. Three outcomes, and they are not the same:
  confirmed -> PROMOTE. The tilt keeps its support.
  unchanged -> no promotion, but the slope is still negative. Cluster 1 retires
               for selection; the live tilt KEEPS RUNNING, because its own
               registered kill trigger did not fire. Reverting it here would
               retire a live scorer on evidence its registration does not call
               a failure.
  retired   -> the slope is >= 0. The July trigger fires. The tilt is retired,
               which is a `SCORER_CONFIG_VERSION` bump with its own cohort
               reset, executed under that memo, not this one.

VOLATILITY INTERACTION. The July registration also carried ONE interaction
hypothesis: the ATR effect conditional on the market-state volatility axis. It
is run MECHANICALLY here as a sufficiency check and its result is printed
whatever it is. The unit is independent regime EPISODES, not days, and the floor
is 4. "Insufficient, not estimated" is an honest and expected outcome, and it is
DISCLOSED rather than dropped: declining to spend the charge is legitimate,
declining to say so is not.

VOID CLAUSE. The look may be abandoned and re-registered UNSPENT only for
defects that leave no verdict to report: the panel falling below MIN_EPISODES or
MIN_CLUSTERS, a join that comes back empty, a signal column with no variance
(which standardises to zeros and, because the shared OLS uses a pseudo-inverse,
would quietly return a slope of zero rather than raise), or a NON-FINITE
statistic. That last one is a VOID and never a verdict: every comparison in
`decide` is False against NaN, so a numerical failure would otherwise fall
through to "the July kill trigger fired, revert the live scorer". A statistic
that does not exist is not a finding about ATR.
A VOID is not a RETIRE - it is the absence of a run.
The moment any feature-vs-outcome statistic is emitted, the look is spent.
Precedent: `exit_policy_comparison_prereg_2026_08_24.md` voided itself before
its cohort opened and its slot was returned.

POST-PROMOTION MONITORING (the frame, frozen now; the numeric thresholds are
NOT, and are explicitly a post-confirmation operational specification rather
than pre-registered evidence - the owner decision of 2026-09-23):
  - Monitoring estimand: this same partial slope, on the FULL stamped
    population (briefed plus LLM-proposed), never on the live book alone. Once
    the tilt is live the book is conditioned on ATR, so the in-book slope is
    attenuated and cannot identify the effect; the names the tilt demotes are
    what restores identification.
  - Unit and horizon: ticker-episode, `sel_ar_20`, unchanged.
  - Baseline: the estimate this look produces, not zero and not the discovery
    estimate.
  - Intervention is ONE-SIDED: monitoring may only ever REMOVE the tilt, never
    add or strengthen one. That bounds the cost of a false alarm to a reversion
    to today's state. It does NOT remove multiplicity: the promotion
    registration must fix a FIXED number of scheduled looks and a per-look
    level, so the cumulative false-removal probability is a stated number.
  - HARD PRE-COMMITMENT, made now because it is the part that cannot be
    recovered later: the tilt must act at a pipeline stage AFTER the candidate
    proposal stage. The label stamper writes `sel_ar_*` for every name in
    `proposal_shadow` whether or not the brief carried it, and the shadow is
    written at the mapping stage, before scoring and before brief selection
    (verified in the tree on 2026-09-23). A tilt acting at or before proposal
    would delete its own counterfactual, and no later registration could get it
    back.

LIMITATIONS (stated up front; details in the memo): the shrunken effect the
power curve used is an in-sample quantity and the three signals were selected on
the burnt panel; the July ATR figure was row-level and early-weighted while this
estimate is episode-level on a different label; `sel_ar_20` is beta-adjusted
against IWM with a raw 250-session beta and no intercept; the implied SE quoted
above is a normal approximation back-derived from a bootstrap power curve, not a
measured standard error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ------------------------------------------------------------------ frozen
#: The smallest actionable effect (owner decision 2026-09-23). See the
#: docstring for what a pass does and does not prove.
DELTA = 0.10

#: One-sided, direction fixed from discovery: higher ATR, worse outcome.
ALPHA = 0.05
ALTERNATIVE = "less"

#: Two-sided level of the interval that grades a NON-promotion. TOST convention
#: for a one-sided 0.05 test; 95% would make the equivalence arm unreachable.
CI_LEVEL = 0.90

N_BOOT = 9999
CI_BOOT = 2999
SEED = 20260924

#: Below either of these the run is VOID, not a verdict. Both sit comfortably
#: under the outcome-blind panel measured on 2026-09-23 (419 episodes, 35
#: clusters), so only a broken join or a mass maturity failure trips them.
MIN_EPISODES = 300
MIN_CLUSTERS = 30

#: Independent market-state regime episodes needed before the one registered
#: interaction is estimated at all.
MIN_VOL_EPISODES = 4

#: Arrival clusters a single regime arm needs before its slope is printed. A
#: registration should not hide a number: this used to be MIN_CLUSTERS // 3
#: computed inline, invisible to anyone reading the frozen constants.
MIN_REGIME_CLUSTERS = 10

#: The registration is merged before the look runs; the gap is what enforces it.
RUN_NOT_BEFORE = dt.date(2026, 9, 24)

DISCOVERY_CUTOFF = "2026-07-05"
POPULATION = "briefed"
OUTCOME = "sel_ar_20"
STATUS_RESOLVED = "ok"

ATR = "technical_atr_pct"
COVARIATES = ("technical_ma50_distance_pct", "n_gates_passed")
REGIME_COLUMN = "market_state"


class VoidError(RuntimeError):
    """The run cannot be computed as registered. The look is NOT spent."""


@dataclass(frozen=True)
class Decision:
    """The registered verdict. Every field is computed, none is chosen."""

    promoted: bool
    cluster: str
    conclusion: str
    tilt: str
    beta: float
    p_value: float
    ci: tuple[float, float]


def decide(*, beta: float, p_value: float, ci: tuple[float, float]) -> Decision:
    """Apply the frozen rule. This function is the registration in code.

    A non-finite input is a VOID, never a verdict. Every comparison below is
    False against NaN, so a numerical failure would otherwise fall through to
    ``tilt = "retired"`` — a broken computation reported as "the July kill
    trigger fired, revert the live scorer". A statistic that does not exist is
    not a finding about ATR.
    """
    lo, hi = ci
    if not all(math.isfinite(float(v)) for v in (beta, p_value, lo, hi)):
        raise VoidError(
            f"non-finite statistic (slope {beta}, p {p_value}, interval {ci}); "
            f"the run did not compute and is NOT a verdict"
        )
    promoted = p_value < ALPHA and beta <= -DELTA
    inside = lo > -DELTA and hi < DELTA
    if promoted:
        conclusion = "cleared"
    elif inside:
        conclusion = "evidence-against"
    else:
        conclusion = "inconclusive"
    if promoted:
        tilt = "confirmed"
    elif beta < 0:
        # The July kill trigger is a sign flip, and it did not fire.
        tilt = "unchanged"
    else:
        tilt = "retired"
    return Decision(
        promoted=promoted,
        cluster="PROMOTE" if promoted else "RETIRE",
        conclusion=conclusion,
        tilt=tilt,
        beta=float(beta),
        p_value=float(p_value),
        ci=(float(lo), float(hi)),
    )


def refuse_early_run(today: dt.date) -> str | None:
    """The message refusing a run before the registration date, or None."""
    if today >= RUN_NOT_BEFORE:
        return None
    return (
        f"refusing to run before {RUN_NOT_BEFORE}: the registration is merged "
        f"first, never alongside the look. --override-run-date is a logged "
        f"protocol deviation."
    )


# ------------------------------------------------------------------- panel
def status_census(labels_dir: Any) -> dict[str, int]:
    """Held-out label statuses, counts only. Outcome-blind and disclosable."""
    frames = [
        pd.read_parquet(path, columns=["sel_label_status_20", "briefed_any_theme"])
        for path in sorted(Path(labels_dir).glob("*.parquet"))
        if path.stem > DISCOVERY_CUTOFF
    ]
    if not frames:
        return {}
    frame = pd.concat(frames, ignore_index=True)
    frame = frame[frame["briefed_any_theme"].fillna(False).astype(bool)]
    return {str(k): int(v) for k, v in frame["sel_label_status_20"].value_counts().items()}


def held_out_panel(labels_dir: Any, briefs_dir: Any) -> pd.DataFrame:
    """The frozen confirmation panel, or :class:`VoidError` if it cannot be built.

    Reads outcome values, so calling this IS the start of the look. The
    stringified join key is deliberate: the two stores stamp ``brief_date`` with
    different types and a silent type mismatch would empty the join and hand
    back a clean-looking empty panel.
    """
    from alphalens_research.diagnostics.options_retro import ticker_episode_dedup

    label_cols = [
        "brief_date",
        "ticker",
        "anchor_session",
        "briefed_any_theme",
        "sel_label_status_20",
        OUTCOME,
    ]
    label_paths = [
        path for path in sorted(Path(labels_dir).glob("*.parquet")) if path.stem > DISCOVERY_CUTOFF
    ]
    brief_paths = [
        path for path in sorted(Path(briefs_dir).glob("*.parquet")) if path.stem > DISCOVERY_CUTOFF
    ]
    if not label_paths or not brief_paths:
        raise VoidError(
            f"no held-out parquets past {DISCOVERY_CUTOFF} "
            f"({len(label_paths)} label, {len(brief_paths)} brief)"
        )

    labels = pd.concat(
        [pd.read_parquet(path, columns=label_cols) for path in label_paths], ignore_index=True
    )
    briefs = pd.concat(
        [_read_brief(path) for path in brief_paths],
        ignore_index=True,
    )
    labels["brief_date"] = labels["brief_date"].astype(str)
    labels["ticker"] = labels["ticker"].astype(str).str.upper()
    briefs["ticker"] = briefs["ticker"].astype(str).str.upper()

    panel = labels.merge(briefs, on=["brief_date", "ticker"], how="inner", validate="m:1")
    panel = panel[panel["sel_label_status_20"].astype(str) == STATUS_RESOLVED]
    panel = panel[panel["briefed_any_theme"].fillna(False).astype(bool)]
    panel = panel.dropna(subset=[OUTCOME, ATR, *COVARIATES])
    panel = panel.rename(columns={"anchor_session": "arrival"})
    panel["arrival"] = panel["arrival"].astype(str)
    panel = ticker_episode_dedup(panel)

    for column in (ATR, *COVARIATES):
        # A zero-variance column standardises to zeros, and the shared OLS uses
        # a pseudo-inverse, so this does not raise: it quietly returns a slope
        # of zero, which the rule would read as a sign flip.
        if float(np.std(panel[column].astype(float).to_numpy())) <= 0.0:
            raise VoidError(f"{column} has no variance on the panel; the design is degenerate")

    clusters = panel["arrival"].nunique()
    if len(panel) < MIN_EPISODES or clusters < MIN_CLUSTERS:
        raise VoidError(
            f"panel is {len(panel)} episodes in {clusters} arrival clusters; the "
            f"registration requires at least {MIN_EPISODES} and {MIN_CLUSTERS}. "
            f"The look is NOT spent."
        )
    return panel.reset_index(drop=True)


def _read_brief(path: Path) -> pd.DataFrame:
    """One brief parquet with the signal columns, plus the regime if present."""
    import pyarrow.parquet as pq

    available = set(pq.ParquetFile(path).schema.names)
    wanted = ["ticker", ATR, *COVARIATES]
    if REGIME_COLUMN in available:
        wanted.append(REGIME_COLUMN)
    return pd.read_parquet(path, columns=wanted).assign(brief_date=path.stem)


# --------------------------------------------------------------- estimator
def _z(values: np.ndarray) -> np.ndarray:
    sd = float(np.std(values))
    return (values - float(np.mean(values))) / sd if sd > 0 else np.zeros_like(values)


def _design(panel: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """z(outcome) and [const, z(atr), z(covariates...)]. ATR is column 1."""
    y = _z(panel[OUTCOME].astype(float).to_numpy())
    columns = [np.ones(len(panel)), _z(panel[ATR].astype(float).to_numpy())]
    columns += [_z(panel[col].astype(float).to_numpy()) for col in COVARIATES]
    return y, np.column_stack(columns)


def atr_slope(panel: pd.DataFrame) -> float:
    """The registered estimand: the standardised partial slope of ATR.

    Uses the SAME fit as the bootstrap (`_ols_beta`, a pseudo-inverse of X'X)
    rather than a second solver. `np.linalg.lstsq` agrees with it on a
    full-rank design and disagrees on a rank-deficient one, and the printed
    estimate and the tested estimate must be one number, not two.
    """
    from alphalens_research.diagnostics.options_retro import _ols_beta

    y, X = _design(panel)
    beta, _ = _ols_beta(y, X)
    return float(beta[1])


def one_sided_p(panel: pd.DataFrame, *, n_boot: int = N_BOOT, seed: int = SEED) -> float:
    """The registered p-value: one-sided restricted WCB on arrival clusters."""
    from alphalens_research.diagnostics.options_retro import wild_cluster_bootstrap_p

    y, X = _design(panel)
    return float(
        wild_cluster_bootstrap_p(
            y,
            X,
            panel["arrival"].to_numpy(),
            coef_idx=1,
            n_boot=n_boot,
            seed=seed,
            alternative=ALTERNATIVE,
        )
    )


def slope_ci(
    panel: pd.DataFrame,
    *,
    n_boot: int = CI_BOOT,
    seed: int = SEED,
    level: float = CI_LEVEL,
) -> tuple[float, float]:
    """Cluster-bootstrap interval for the slope, used only to GRADE a null.

    Resamples arrival SESSIONS, not rows: the dependence that matters is
    same-day co-movement, and a row bootstrap would report an interval far too
    tight and hand the equivalence arm a confidence it has not earned.

    Each draw is re-standardised, because :func:`atr_slope` standardises what it
    is given. That is the consistent choice: the estimand is a standardised
    slope, and the point estimate is standardised in-sample too, so the interval
    covers the same quantity the point estimate reports. Freezing the original
    sample's mean and scale would give an interval for a different statistic and
    a slightly narrower one.
    """
    rng = np.random.default_rng(seed)
    arrivals = panel["arrival"].to_numpy()
    # Sorted, not first-appearance order: with a fixed seed the draw maps
    # integers onto this list, so a differently ordered panel would give a
    # different interval for the same data.
    groups = [panel.iloc[np.flatnonzero(arrivals == c)] for c in sorted(pd.unique(arrivals))]
    draws = []
    for _ in range(n_boot):
        picked = rng.integers(0, len(groups), len(groups))
        draws.append(atr_slope(pd.concat([groups[i] for i in picked], ignore_index=True)))
    tail = 100.0 * (1.0 - level) / 2.0
    return float(np.percentile(draws, tail)), float(np.percentile(draws, 100.0 - tail))


def volatility_sufficiency(panel: pd.DataFrame) -> tuple[int, bool]:
    """Independent market-state regime episodes, and whether that is enough.

    An EPISODE is a maximal run of consecutive arrival sessions sharing one
    regime label, which is the honest unit: forty days inside one high-vol
    stretch are one observation of "high vol", not forty.

    Runs are counted over the sessions PRESENT in the panel, so a calendar gap
    between two same-state stretches is invisible and they merge into one
    episode. That direction is deliberate: it can only UNDERcount episodes, and
    an undercount makes the sufficiency check refuse to estimate rather than
    estimate on less independence than it thinks it has.
    """
    if REGIME_COLUMN not in panel.columns:
        return 0, False
    by_session = (
        panel[["arrival", REGIME_COLUMN]]
        .dropna()
        .drop_duplicates(subset=["arrival"])
        .sort_values("arrival")
    )
    states = by_session[REGIME_COLUMN].astype(str).tolist()
    if not states:
        return 0, False
    episodes = 1 + sum(1 for a, b in itertools.pairwise(states) if a != b)
    return episodes, episodes >= MIN_VOL_EPISODES


# ------------------------------------------------------------------ driver
def preflight(labels_dir: Any, briefs_dir: Any) -> None:
    """Outcome-blind checks. Emits no feature-vs-outcome statistic."""
    print("PREFLIGHT — outcome-blind. Nothing here spends the look.\n")
    census = status_census(labels_dir)
    total = sum(census.values())
    print(f"held-out briefed label rows past {DISCOVERY_CUTOFF}: {total}")
    for status, count in sorted(census.items()):
        share = 100.0 * count / total if total else 0.0
        print(f"  {status:<20} {count:>6}  ({share:.1f}%)")
    resolved = census.get(STATUS_RESOLVED, 0)
    briefs = [p for p in sorted(Path(briefs_dir).glob("*.parquet")) if p.stem > DISCOVERY_CUTOFF]
    print(f"\nheld-out brief parquets available to join: {len(briefs)}")
    print(f"resolved rows available to the panel: {resolved}")
    print(f"registration floors: >= {MIN_EPISODES} episodes, >= {MIN_CLUSTERS} arrival clusters")
    print("A panel below either floor is a VOID, not a RETIRE — the look returns unspent.")


def full_run(labels_dir: Any, briefs_dir: Any) -> Decision:
    """THE look. Burns cluster 1's confirmation."""
    from alphalens_research.diagnostics.options_retro import cluster_ols, vif_table

    panel = held_out_panel(labels_dir, briefs_dir)
    clusters = panel["arrival"].nunique()
    print("=" * 72)
    print("ATR HELD-OUT CONFIRMATION — #1227, ledger cluster 1, one-shot")
    print("=" * 72)
    print(f"panel: {len(panel)} ticker-episodes in {clusters} arrival clusters")
    print(f"population: {POPULATION}; outcome: {OUTCOME}; held-out > {DISCOVERY_CUTOFF}")

    beta = atr_slope(panel)
    p_value = one_sided_p(panel)
    ci = slope_ci(panel)
    decision = decide(beta=beta, p_value=p_value, ci=ci)

    print(f"\nstandardised partial slope (ATR): {beta:+.4f}")
    print(f"one-sided wild cluster bootstrap p: {p_value:.4f}  (bar {ALPHA})")
    print(f"{int(CI_LEVEL * 100)}% cluster-bootstrap interval: [{ci[0]:+.4f}, {ci[1]:+.4f}]")
    print(f"smallest actionable effect: |rho| >= {DELTA}")

    print("\nDIAGNOSTICS (reported, never a clearing path):")
    y, X = _design(panel)
    fit = cluster_ols(y, X, panel["arrival"].to_numpy())
    print(f"  CR2 t on ATR: {fit.t_cr2[1]:+.3f} over {fit.n_clusters} clusters")
    print(f"  condition number of the design: {np.linalg.cond(X):.1f}")
    vif = vif_table(panel, [ATR, *COVARIATES])
    print("  VIF: " + ", ".join(f"{k}={v:.2f}" for k, v in vif.items()))
    uni = pd.DataFrame(
        {OUTCOME: panel[OUTCOME], ATR: panel[ATR], "arrival": panel["arrival"]}
    ).assign(**dict.fromkeys(COVARIATES, 0.0))
    print(f"  univariate slope (SENSITIVITY ONLY, not a second chance): {atr_slope(uni):+.4f}")

    episodes, estimable = volatility_sufficiency(panel)
    print("\nVOLATILITY INTERACTION (the one registered interaction):")
    if estimable:
        print(f"  {episodes} independent regime episodes — estimated below")
        _report_interaction(panel)
    else:
        print(
            f"  {episodes} independent regime episodes — INSUFFICIENT, not estimated "
            f"(floor {MIN_VOL_EPISODES}). Reported, not dropped."
        )

    print("\n" + "=" * 72)
    print(f"VERDICT: {decision.cluster} — conclusion '{decision.conclusion}'")
    print(f"LIVE SCORER TILT: {decision.tilt}")
    if decision.promoted:
        print("Cluster 1 clears its held-out confirmation. A PASS means the held-out")
        print("estimate crossed the actionable line with the registered sign; it is")
        print("NOT evidence that the true effect is at least 0.10. Promotion into")
        print("selection requires the separate promotion registration that carries")
        print("the monitoring thresholds.")
    else:
        print("Cluster 1 is RETIRED for selection and ordering under ledger rule 4.")
        print("ATR's use in execution geometry is a different estimand on the §4.1")
        print("budget and is untouched. See the pre-committed three-way conclusion")
        print("language in the docstring for what this null does and does not say.")
    print("=" * 72)
    return decision


def _report_interaction(panel: pd.DataFrame) -> None:
    """Per-regime slopes, printed only when the sufficiency check passed."""
    for state, part in panel.groupby(panel[REGIME_COLUMN].astype(str)):
        if part["arrival"].nunique() < MIN_REGIME_CLUSTERS:
            print(f"  {state}: {part['arrival'].nunique()} clusters — not estimated")
            continue
        print(f"  {state}: slope {atr_slope(part):+.4f} on {len(part)} episodes")


def main(argv: list[str] | None = None) -> int:
    home = Path.home() / ".alphalens"
    parser = argparse.ArgumentParser(
        description="ATR held-out confirmation — the registered one-shot look on cluster 1"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true", help="outcome-blind checks only")
    mode.add_argument("--run", action="store_true", help="THE look — burns cluster 1's slot")
    parser.add_argument("--labels-dir", type=Path, default=home / "selection_labels")
    parser.add_argument("--briefs-dir", type=Path, default=home / "thematic_briefs")
    parser.add_argument(
        "--override-run-date",
        action="store_true",
        help="run before RUN_NOT_BEFORE (logged protocol deviation)",
    )
    args = parser.parse_args(argv)

    if args.preflight:
        preflight(args.labels_dir, args.briefs_dir)
        return 0

    refusal = refuse_early_run(dt.date.today())
    if refusal and not args.override_run_date:
        sys.exit(refusal)
    try:
        full_run(args.labels_dir, args.briefs_dir)
    except VoidError as exc:
        print(f"VOID — {exc}", file=sys.stderr)
        return 8
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
