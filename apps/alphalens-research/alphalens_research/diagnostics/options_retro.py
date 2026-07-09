"""Options retro pilot helpers (``options_retro_ivol_smd_v1``).

Ledger look ``options_retro_pilot_2026_07`` (design memo
``docs/research/options_retro_firstlook_design_2026_07_09.md``): reconstruct
the v9D options-implied feature stack from the immutable iVolatility smd
cache as-of each banked brief date and test it against matured EDGE
market-excess outcomes. Retro features NEVER pool with the forward yfinance
telemetry (different vendor, different construction).

Four pieces, each mirrored by a pinned memo rule:

- :func:`smd_features_asof` — v9D as-of convention: US primary exchanges
  only, weekend/calendar padding dropped via the ``ivp30``-non-null filter,
  most recent trading row at or before the brief date, vendor percent units
  converted to decimal vol (``ivp30`` stays 0-100).
- :func:`ticker_episode_dedup` — the July 2026 attribution doctrine:
  persisting candidates are ONE episode; chained rolling 5-session window,
  keep the first row per episode.
- :func:`wild_cluster_bootstrap_p` — primary inference: restricted
  (null-imposed) wild cluster bootstrap with Rademacher weights, clusters =
  brief days (~51 clusters sits where plain CR1 is downward-biased).
- :func:`cluster_ols` / :func:`vif_table` — CR2-corrected SEs reported
  alongside, VIF gate before inference.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd
from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    session_on_or_after,
)
from scipy import stats as scipy_stats

from alphalens_research.screeners.options_implied.features import US_PRIMARY_EXCHANGES

OPTIONS_RETRO_VERSION = "options_retro_ivol_smd_v1"

FEATURE_COLUMNS = ("ivx30", "ivx180_minus_ivx30", "hv20", "ivp30")

_EPISODE_WINDOW_SESSIONS = 5


def smd_features_asof(history: pd.DataFrame | None, asof: dt.date) -> dict[str, float] | None:
    """The 4 pilot features from the last smd trading row at or before ``asof``.

    Vendor smd pads calendar days (Sat/Sun carry Friday's OHLCV with NaN
    ``ivp30``); filtering on ``ivp30`` non-null keeps trading rows only —
    the same PIT convention the v9D joiner validated (0.9990). IV/HV arrive
    in percent and convert to decimal vol; ``ivp30`` stays on the vendor's
    0-100 percentile scale. Returns ``None`` when any of the four fields is
    missing on the selected row (caller counts the pair as not covered).
    """
    if history is None or history.empty or "tradeDate" not in history.columns:
        return None
    rows = history
    if "exchange" in rows.columns:
        rows = rows.loc[rows["exchange"].astype(str).isin(US_PRIMARY_EXCHANGES)]
    if "ivp30" not in rows.columns:
        return None
    rows = rows.loc[rows["ivp30"].notna()]
    rows = rows.loc[pd.to_datetime(rows["tradeDate"]) <= pd.Timestamp(asof)]
    if rows.empty:
        return None
    row = rows.sort_values("tradeDate").iloc[-1]
    values: list[float] = []
    for col in ("ivx30", "ivx180", "hv20", "ivp30"):
        v = row.get(col)
        if v is None or pd.isna(v):
            return None
        values.append(float(v))
    ivx30, ivx180, hv20, ivp30 = values
    return {
        "ivx30": ivx30 / 100.0,
        "ivx180_minus_ivx30": (ivx180 - ivx30) / 100.0,
        "hv20": hv20 / 100.0,
        "ivp30": ivp30,
    }


def ticker_episode_dedup(
    panel: pd.DataFrame,
    *,
    window_sessions: int = _EPISODE_WINDOW_SESSIONS,
    exchange: str = DEFAULT_EXCHANGE,
) -> pd.DataFrame:
    """Collapse same-ticker rows into episodes; keep the first row per episode.

    Chained rule: a row extends the current episode when its brief date's
    arrival session is within ``window_sessions`` trading sessions of the
    PREVIOUS appearance's arrival session (so a candidate persisting for
    weeks stays one episode even though its span exceeds the window).
    Weekend brief dates map to their next trading session first.
    """
    if panel.empty:
        return panel.copy()
    df = panel.sort_values(["ticker", "brief_date"], kind="stable")
    arrivals = {d: session_on_or_after(d, exchange) for d in df["brief_date"].unique()}
    keep: list[bool] = []
    last_arrival: dict[str, dt.date] = {}
    for _, row in df.iterrows():
        ticker = row["ticker"]
        arrival = arrivals[row["brief_date"]]
        prev = last_arrival.get(ticker)
        chained = prev is not None and arrival <= advance_trading_sessions(
            prev, window_sessions, exchange
        )
        keep.append(not chained)
        last_arrival[ticker] = arrival
    return df.loc[keep].sort_index()


@dataclass(frozen=True)
class ClusterOlsResult:
    """OLS with cluster-robust (CR1 and CR2) covariance, clusters = brief days."""

    beta: np.ndarray
    se_cr1: np.ndarray
    se_cr2: np.ndarray
    t_cr2: np.ndarray
    p_cr2: np.ndarray
    n_clusters: int


def _ols_beta(y: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    xtx_inv = np.linalg.pinv(X.T @ X)
    beta = xtx_inv @ (X.T @ y)
    return beta, xtx_inv


def _cluster_indices(clusters: np.ndarray) -> list[np.ndarray]:
    order = {}
    for i, g in enumerate(clusters):
        order.setdefault(g, []).append(i)
    return [np.asarray(ix) for ix in order.values()]


def _cr1_cov(
    X: np.ndarray, resid: np.ndarray, xtx_inv: np.ndarray, groups: list[np.ndarray]
) -> np.ndarray:
    n, k = X.shape
    g = len(groups)
    meat = np.zeros((k, k))
    for ix in groups:
        s = X[ix].T @ resid[ix]
        meat += np.outer(s, s)
    # Stata-style CR1 small-sample factor.
    factor = (g / (g - 1)) * ((n - 1) / (n - k))
    return factor * xtx_inv @ meat @ xtx_inv


def _cr2_cov(
    X: np.ndarray, resid: np.ndarray, xtx_inv: np.ndarray, groups: list[np.ndarray]
) -> np.ndarray:
    k = X.shape[1]
    meat = np.zeros((k, k))
    for ix in groups:
        xg = X[ix]
        # (I - H_gg)^{-1/2} leverage adjustment via symmetric eigendecomposition.
        h = xg @ xtx_inv @ xg.T
        eigval, eigvec = np.linalg.eigh(np.eye(len(ix)) - h)
        # Near-zero eigenvalues (leverage -> 1, e.g. a high-leverage singleton
        # cluster) would amplify finite-precision noise through the inverse
        # square root; zero their contribution instead of inverting them.
        mask = eigval >= 1e-8
        inv_sqrt = np.where(mask, np.divide(1.0, np.sqrt(np.where(mask, eigval, 1.0))), 0.0)
        adj = eigvec @ np.diag(inv_sqrt) @ eigvec.T
        s = xg.T @ (adj @ resid[ix])
        meat += np.outer(s, s)
    return xtx_inv @ meat @ xtx_inv


def cluster_ols(y: np.ndarray, X: np.ndarray, clusters: np.ndarray) -> ClusterOlsResult:
    """OLS with CR1 and CR2 cluster-robust SEs; p-values from t(G-1) on CR2."""
    y = np.asarray(y, dtype=float)
    X = np.asarray(X, dtype=float)
    clusters = np.asarray(clusters)
    beta, xtx_inv = _ols_beta(y, X)
    resid = y - X @ beta
    groups = _cluster_indices(clusters)
    g = len(groups)
    se_cr1 = np.sqrt(np.diag(_cr1_cov(X, resid, xtx_inv, groups)))
    se_cr2 = np.sqrt(np.diag(_cr2_cov(X, resid, xtx_inv, groups)))
    # A zero-variance regressor (e.g. an indicator constant within a
    # sub-window) has SE 0 — its t is honestly NaN, not a warning.
    with np.errstate(divide="ignore", invalid="ignore"):
        t_cr2 = np.where(se_cr2 > 0, beta / np.where(se_cr2 > 0, se_cr2, 1.0), np.nan)
    p_cr2 = 2.0 * scipy_stats.t.sf(np.abs(t_cr2), df=max(g - 1, 1))
    return ClusterOlsResult(
        beta=beta, se_cr1=se_cr1, se_cr2=se_cr2, t_cr2=t_cr2, p_cr2=p_cr2, n_clusters=g
    )


def wild_cluster_bootstrap_p(
    y: np.ndarray,
    X: np.ndarray,
    clusters: np.ndarray,
    coef_idx: int,
    *,
    n_boot: int = 4999,
    seed: int = 0,
) -> float:
    """Restricted wild cluster bootstrap p-value for one coefficient.

    Cameron-Gelbach-Miller: impose the null (drop the tested column), build
    bootstrap samples by flipping restricted residuals with per-cluster
    Rademacher weights, refit the FULL model each draw, and compare the
    studentized |t*| distribution (CR1-studentized, consistent on both
    sides) against the observed |t|.
    """
    y = np.asarray(y, dtype=float)
    X = np.asarray(X, dtype=float)
    clusters = np.asarray(clusters)
    groups = _cluster_indices(clusters)

    beta, xtx_inv = _ols_beta(y, X)
    resid = y - X @ beta
    se = np.sqrt(np.diag(_cr1_cov(X, resid, xtx_inv, groups)))
    t_obs = beta[coef_idx] / se[coef_idx]

    keep = [j for j in range(X.shape[1]) if j != coef_idx]
    Xr = X[:, keep]
    beta_r, _ = _ols_beta(y, Xr)
    fitted_r = Xr @ beta_r
    resid_r = y - fitted_r

    rng = np.random.default_rng(seed)
    hits = 0
    valid = 0
    proj = xtx_inv @ X.T  # fixed design: refits are one matmul per draw
    for _ in range(n_boot):
        w = rng.choice((-1.0, 1.0), size=len(groups))
        y_star = fitted_r.copy()
        for gi, ix in enumerate(groups):
            y_star[ix] += resid_r[ix] * w[gi]
        beta_star = proj @ y_star
        resid_star = y_star - X @ beta_star
        se_star = np.sqrt(_cr1_cov(X, resid_star, xtx_inv, groups)[coef_idx, coef_idx])
        if se_star <= 0:  # degenerate draw: excluded from the p-value denominator
            continue
        valid += 1
        # Null imposed: beta* is centered on 0 for the tested coefficient.
        if abs(beta_star[coef_idx] / se_star) >= abs(t_obs):
            hits += 1
    return (1.0 + hits) / (1.0 + valid)


def vif_table(df: pd.DataFrame, columns: list[str]) -> dict[str, float]:
    """Variance inflation factors (with intercept) for the given columns."""
    X = df[columns].to_numpy(dtype=float)
    X = np.column_stack([np.ones(len(X)), X])
    out: dict[str, float] = {}
    for j, col in enumerate(columns, start=1):
        others = [i for i in range(X.shape[1]) if i != j]
        beta, _ = _ols_beta(X[:, j], X[:, others])
        resid = X[:, j] - X[:, others] @ beta
        tss = float(np.sum((X[:, j] - X[:, j].mean()) ** 2))
        rss = float(resid @ resid)
        r2 = 1.0 - rss / tss if tss > 0 else 0.0
        out[col] = float("inf") if r2 >= 1.0 else 1.0 / (1.0 - r2)
    return out
