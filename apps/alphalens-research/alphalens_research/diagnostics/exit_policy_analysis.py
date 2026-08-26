"""§10.4/§10.5 analysis machinery for the exit-policy pre-registration (#1115).

Governing document: ``docs/research/exit_policy_comparison_prereg_2026_08_24.md``
(LOCKED). Where a clause and this module disagree, the clause wins.

Pure statistical primitives over per-candidate differences ``d_i = net_B −
net_A`` (USD): the five pre-specified inference arms (§6.2), the §6.3/§6.4
floors, the §8.1 reporting helpers, and the §10.5 extract-hash protocol. The
driver script wires them to data; nothing here reads a store.

Pre-committed decision rule (§8.2/§6.2), hard-coded: the verdict is read from
the WIDEST interval among arms 2-5 (never the iid arm), two-sided α = 0.05,
one look. The bootstrap seed is fixed and recorded in every payload.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

__status__ = "RESEARCH_ONLY"

# Frozen analysis constants (§6.2/§8.2). The seed is recorded in the results
# memo; 10k resamples per arm.
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20260824
ALPHA_TWO_SIDED = 0.05
BLOCK_LEN_SESSIONS = 42
BLOCK_FLOOR = 10

_Z_975 = 1.959963984540054
_Z_80 = 0.8416212335729143

# §10.5: the cohort extract is committed BEFORE the outcome join and is
# structurally input-only — no outcome column can exist in it.
EXTRACT_COLUMNS: tuple[str, ...] = (
    "brief_date",
    "ticker",
    "trade_setup_json",
    "pct_off_52w_high",
)


@dataclass(frozen=True)
class InferenceArm:
    ci_low: float
    ci_high: float
    n_clusters: int


def _percentile_ci(samples: list[float]) -> tuple[float, float]:
    low, high = np.percentile(samples, [100 * ALPHA_TWO_SIDED / 2, 100 * (1 - ALPHA_TWO_SIDED / 2)])
    return float(low), float(high)


def _resample_means_one_way(
    d: np.ndarray, clusters: np.ndarray, n_boot: int, rng: np.random.Generator
) -> list[float]:
    unique = np.unique(clusters)
    index_by_cluster = {c: np.flatnonzero(clusters == c) for c in unique}
    means: list[float] = []
    for _ in range(n_boot):
        drawn = rng.choice(unique, size=len(unique), replace=True)
        idx = np.concatenate([index_by_cluster[c] for c in drawn])
        means.append(float(d[idx].mean()))
    return means


def _resample_means_two_way(
    d: np.ndarray,
    days: np.ndarray,
    tickers: np.ndarray,
    n_boot: int,
    rng: np.random.Generator,
) -> list[float]:
    """§6.2 arm 4: days and tickers drawn independently with replacement; a
    row's weight is (times its day was drawn) × (times its ticker was drawn);
    zero-total-weight resamples are skipped."""
    unique_days, day_index = np.unique(days, return_inverse=True)
    unique_tickers, ticker_index = np.unique(tickers, return_inverse=True)
    means: list[float] = []
    for _ in range(n_boot):
        day_counts = np.bincount(
            rng.integers(0, len(unique_days), size=len(unique_days)), minlength=len(unique_days)
        )
        ticker_counts = np.bincount(
            rng.integers(0, len(unique_tickers), size=len(unique_tickers)),
            minlength=len(unique_tickers),
        )
        weights = day_counts[day_index] * ticker_counts[ticker_index]
        total = weights.sum()
        if total == 0:
            continue
        means.append(float((weights * d).sum() / total))
    return means


def _resample_means_moving_block(
    d: np.ndarray, days: np.ndarray, block_len: int, n_boot: int, rng: np.random.Generator
) -> list[float]:
    """§6.2 arm 5: moving-block bootstrap over the ORDERED day sequence with
    block length H sessions. Overlapping start positions; enough blocks are
    drawn to cover the day count; rows of the drawn days are concatenated."""
    ordered_days = np.array(sorted(np.unique(days)))
    n_days = len(ordered_days)
    index_by_day = {day: np.flatnonzero(days == day) for day in ordered_days}
    effective_len = min(block_len, n_days)
    n_starts = n_days - effective_len + 1
    blocks_needed = math.ceil(n_days / effective_len)
    means: list[float] = []
    for _ in range(n_boot):
        starts = rng.integers(0, n_starts, size=blocks_needed)
        drawn_days: list[str] = []
        for start in starts:
            drawn_days.extend(ordered_days[start : start + effective_len])
        drawn_days = drawn_days[:n_days]
        idx = np.concatenate([index_by_day[day] for day in drawn_days])
        means.append(float(d[idx].mean()))
    return means


def inference_arms(
    frame: pd.DataFrame,
    *,
    n_boot: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    block_len: int = BLOCK_LEN_SESSIONS,
) -> dict[str, InferenceArm]:
    """The five pre-specified §6.2 arms over columns ``d``, ``brief_date``,
    ``ticker``. All five are computed and reported side by side; the iid arm
    exists so the cost of the dependence is visible — it is never the verdict.
    """
    d = frame["d"].to_numpy(dtype=float)
    days = frame["brief_date"].to_numpy()
    tickers = frame["ticker"].to_numpy()
    rng = np.random.default_rng(seed)

    iid_means = [float(d[rng.integers(0, len(d), size=len(d))].mean()) for _ in range(n_boot)]
    day_means = _resample_means_one_way(d, days, n_boot, rng)
    ticker_means = _resample_means_one_way(d, tickers, n_boot, rng)
    two_way_means = _resample_means_two_way(d, days, tickers, n_boot, rng)
    block_means = _resample_means_moving_block(d, days, block_len, n_boot, rng)

    def arm(means: list[float], n_clusters: int) -> InferenceArm:
        low, high = _percentile_ci(means)
        return InferenceArm(ci_low=low, ci_high=high, n_clusters=n_clusters)

    n_days = len(np.unique(days))
    return {
        "iid": arm(iid_means, len(d)),
        "cluster_day": arm(day_means, n_days),
        "cluster_ticker": arm(ticker_means, len(np.unique(tickers))),
        "cluster_day_ticker": arm(two_way_means, n_days),
        "moving_block": arm(block_means, max(n_days - min(block_len, n_days) + 1, 1)),
    }


def primary_verdict(arms: dict[str, InferenceArm]) -> tuple[str, str]:
    """§8.2: the verdict comes from the WIDEST interval among arms 2-5.

    Returns ``(arm_name, verdict)`` with verdict one of ``arm_b_better``
    (interval excludes 0, positive), ``arm_a_better`` (negative), or
    ``not_distinguishable``.
    """
    candidates = {name: arm for name, arm in arms.items() if name != "iid"}
    name = max(candidates, key=lambda n: candidates[n].ci_high - candidates[n].ci_low)
    widest = candidates[name]
    if widest.ci_low > 0:
        return name, "arm_b_better"
    if widest.ci_high < 0:
        return name, "arm_a_better"
    return name, "not_distinguishable"


# --------------------------------------------------------------------------
# Floors (§6.3 / §6.4).
# --------------------------------------------------------------------------


def non_overlapping_blocks(days: list[str], *, block_len: int = BLOCK_LEN_SESSIONS) -> int:
    """Whole non-overlapping ``block_len``-session blocks the cohort spans."""
    return len(set(days)) // block_len


def pair_floor(*, sd_d: float, delta_min: float) -> int:
    """§6.4: ``n = (z_.975 + z_.80)^2 * (sd_d / delta_min)^2``, rounded up."""
    if delta_min <= 0 or sd_d <= 0:
        raise ValueError("sd_d and delta_min must be positive")
    return math.ceil((_Z_975 + _Z_80) ** 2 * (sd_d / delta_min) ** 2)


# --------------------------------------------------------------------------
# §8.1 reporting helpers.
# --------------------------------------------------------------------------


def distribution_report(d: np.ndarray) -> dict:
    """Full paired distribution — histogram, deciles, min, max (§8.1 item 1)."""
    counts, edges = np.histogram(d, bins=20)
    return {
        "n": len(d),
        "deciles": [float(x) for x in np.percentile(d, np.arange(0, 101, 10))],
        "min": float(d.min()),
        "max": float(d.max()),
        "histogram": {
            "bin_edges": [float(x) for x in edges],
            "counts": [int(c) for c in counts],
        },
    }


def tail_report(d: np.ndarray) -> dict:
    """§8.1 item 2: the share of the total sum carried by the largest 5% by
    absolute value, and Δ recomputed with the single largest positive and the
    single largest negative pair removed."""
    total = float(d.sum())
    k = max(math.ceil(0.05 * len(d)), 1)
    top_abs = d[np.argsort(np.abs(d))][-k:]
    without = np.delete(d, [int(np.argmax(d)), int(np.argmin(d))]) if len(d) > 2 else d
    return {
        "delta": float(d.mean()),
        "top5pct_abs_share_of_sum": float(top_abs.sum() / total) if total != 0 else None,
        "delta_without_extremes": float(without.mean()) if len(without) else None,
    }


# --------------------------------------------------------------------------
# §10.5 extract-hash protocol.
# --------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_extract_hash(path: Path, expected: str) -> None:
    """Refuse to run against a mismatching cohort extract (§10.5)."""
    actual = sha256_of(path)
    if actual != expected:
        raise SystemExit(
            f"cohort extract hash mismatch: expected {expected[:12]}..., got "
            f"{actual[:12]}... — the analysis refuses to run (memo section 10.5)"
        )
