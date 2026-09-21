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


def held_out_structure(frame: pd.DataFrame, *, population: str) -> list[int]:
    """Episodes per arrival session on the held-out side. Structure only.

    Reads nothing outside :data:`HELD_OUT_COLUMNS`, and takes every column as a
    named Series rather than masking the frame, so a test can watch exactly what
    was asked for.

    The population filter is not cosmetic: taking cluster sizes from every
    held-out row while the confirmation runs on the briefed subset would inflate
    power by the ratio between them, and no assertion about label columns would
    catch it.
    """
    status = frame["sel_label_status_20"].astype(str).to_numpy()
    briefed = frame["briefed_any_theme"].fillna(False).astype(bool).to_numpy()
    anchor = frame["anchor_session"].astype(str).to_numpy()
    brief_date = frame["brief_date"].astype(str).to_numpy()
    ticker = frame["ticker"].astype(str).to_numpy()

    keep = status == _RESOLVED
    if population == POPULATION_BRIEFED:
        keep = keep & briefed
    elif population != POPULATION_ALL:
        raise ValueError(f"unknown population {population!r}")

    # Ticker-episode is the unit of independence (ledger rule 5).
    episodes = pd.DataFrame(
        {"anchor": anchor[keep], "brief_date": brief_date[keep], "ticker": ticker[keep]}
    ).drop_duplicates(subset=["brief_date", "ticker"])
    return episodes.groupby("anchor").size().tolist()


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
    clusters_now: int,
    clusters_needed: int,
    accrual_per_session: float,
    maturity_lag_sessions: int = A20_MATURITY_SESSIONS,
    today: dt.date | None = None,
) -> dt.date:
    """When the held-out panel will hold ``clusters_needed`` MATURED clusters.

    Two terms, and leaving out the second is the mistake #1227's own Wake
    derivation makes after D5: the missing arrivals have to happen, and then
    they have to mature.
    """
    from alphalens_pipeline.paper.calendar import advance_trading_sessions

    today = today or dt.date.today()
    missing = max(clusters_needed - clusters_now, 0)
    if missing == 0:
        return today
    if accrual_per_session <= 0:
        raise ValueError("accrual_per_session must be positive")
    sessions = math.ceil(missing / accrual_per_session) + maturity_lag_sessions
    return advance_trading_sessions(today, sessions)


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
) -> dict[str, float]:
    """Power for each hypothesis under Holm, at the given injected effects.

    ``burnt_signals`` is a real (rows x hypotheses) matrix from the BURNT panel.
    Rows are resampled whole into the held-out cluster structure, so the
    cross-signal correlation is the one the data actually has — simulating
    independent normals instead would overstate power for three technical
    signals on the same names.
    """
    from alphalens_research.diagnostics.options_retro import wild_cluster_bootstrap_p

    names = list(effects)
    rng = np.random.default_rng(seed)
    var_u = icc * sd_y**2
    var_e = (1.0 - icc) * sd_y**2
    wins = dict.fromkeys(names, 0)

    for _ in range(n_sims):
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
        cl = np.concatenate(cls).astype(str)
        pvalues = {
            name: wild_cluster_bootstrap_p(
                y, X, cl, i + 1, n_boot=wcb_boot, seed=int(rng.integers(1 << 30))
            )
            for i, name in enumerate(names)
        }
        for name in holm_reject(pvalues):
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
