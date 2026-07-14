"""Continuous-target upgrade of the tiny SIGNAL exercise (house rules 6-9).

Applies the 2026-07-14 external methodological review: the outcome is the
CONTINUOUS car_10 (no dichotomization), the predictive model is elastic net
(correlated ATR/ma50 pair), CV folds are built from contiguous 5-session
arrival blocks (halves the overlapping-label leakage vs per-day groups at
this history length), and the model-vs-ATR comparison uses a day-cluster
bootstrap instead of an invalid naive test. Inference side reuses the
attribution stack (cluster_ols CR2 + wild cluster bootstrap).

Last run (2026-07-14, 160 episodes / 29 day-clusters / 5 blocks):
- inference (CR2+WCB, 7 coefficients, Bonferroni bar .05/7=.007): ATR beta
  -0.0167 p_wcb .073; log10_mcap beta -0.0584 p_wcb .033 (bigger cap -> worse
  car_10) - INVISIBLE to the binary model (L1 zeroed mcap), i.e. the continuous
  target recovered a candidate, but it does NOT clear the family bar:
  exploratory note only, matches the June first-look null on mcap.
- prediction: PRIMARY alpha=0.1 OOF Spearman -0.127; secondary alpha=0.03
  +0.183 vs -ATR baseline +0.194; cluster-bootstrap delta (primary vs
  baseline) -0.322, 95% CI [-0.59, -0.07] -> the pre-committed model is
  significantly WORSE than the univariate baseline; the best secondary is
  indistinguishable from it. Headline unchanged: nothing beats ATR alone.
- LESSON: the alpha grid was carried over from the classification exercise and
  is mis-scaled for a target with sd~0.12 (alpha=0.1 over-shrinks toward a
  near-constant predictor); pre-commit alpha relative to target variance next
  time. Reported as pre-committed anyway - that is the point of pre-commit.

Pre-committed BEFORE looking at any output: the same 7 features as
2026_07_signal_below_spy_tiny.py; elastic net l1_ratio=0.5, PRIMARY
alpha=0.1 (0.03 / 0.3 secondary, no selection); Spearman rank correlation
as the prediction metric; B=4000 cluster-bootstrap resamples.
"""

import math

import numpy as np
import pandas as pd
from alphalens_pipeline.data import rs_history
from alphalens_pipeline.paper.calendar import (
    advance_trading_sessions,
    previous_trading_day,
    session_on_or_after,
)
from alphalens_research.diagnostics import edge_stores, fixed_horizon
from alphalens_research.diagnostics.options_retro import (
    cluster_ols,
    ticker_episode_dedup,
    wild_cluster_bootstrap_p,
)
from scipy import stats as scipy_stats
from sklearn.impute import SimpleImputer
from sklearn.linear_model import ElasticNet
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

FEATURES = [
    "technical_atr_pct",
    "technical_ma50_distance_pct",
    "technical_pct_off_52w_high",
    "technical_rsi",
    "llm_confidence",
    "log10_mcap",
    "n_gates_passed",
]
EX = "XNYS"
BLOCK_SESSIONS = 5
N_BOOT = 4000

outcomes = edge_stores.load_store(edge_stores.HOME / "population_ladders")
briefs = edge_stores.load_store(edge_stores.HOME / "thematic_briefs")
briefs["ticker"] = briefs["ticker"].astype(str).str.upper()
raw_cols = [
    "technical_atr_pct",
    "technical_ma50_distance_pct",
    "technical_pct_off_52w_high",
    "technical_rsi",
    "llm_confidence",
    "market_cap",
    "n_gates_passed",
]
bix = briefs.set_index(["brief_date", "ticker"])[raw_cols]
grouped = edge_stores.GroupedDailyCache(rs_history.DEFAULT_RS_HISTORY_ROOT)
newest = edge_stores.newest_session(rs_history.DEFAULT_RS_HISTORY_ROOT)


def close(snap, t):
    if not snap:
        return None
    b = snap.get(t.upper())
    if not b:
        return None
    try:
        c = float(b["c"])
    except Exception:
        return None
    return c if c > 0 else None


rows = []
for _, r in outcomes[outcomes["plannable"] == True].iterrows():  # noqa: E712
    bd, tk = r["brief_date"], str(r["ticker"]).upper()
    arr = session_on_or_after(bd, EX)
    hor = advance_trading_sessions(arr, 9, EX)
    if hor > newest:
        continue
    anc = previous_trading_day(arr, EX)
    car = fixed_horizon.car_for_event(
        stock_anchor=close(grouped.get(anc), tk),
        stock_horizon=close(grouped.get(hor), tk),
        spy_anchor=close(grouped.get(anc), "SPY"),
        spy_horizon=close(grouped.get(hor), "SPY"),
    )
    if car is None:
        continue
    rec = {"brief_date": bd, "ticker": tk, "arrival": arr, "car_10": car}
    try:
        b = bix.loc[(bd, tk)]
        if isinstance(b, pd.DataFrame):
            b = b.iloc[0]
        for c in raw_cols:
            rec[c] = b.get(c)
    except KeyError:
        pass
    mc = rec.pop("market_cap", None)
    try:
        rec["log10_mcap"] = math.log10(float(mc)) if mc and float(mc) > 0 else None
    except (TypeError, ValueError):
        rec["log10_mcap"] = None
    rows.append(rec)

panel = pd.DataFrame(rows)
dd = ticker_episode_dedup(panel)
# Contiguous BLOCK_SESSIONS-session arrival blocks as CV groups (house rule 9).
sessions = sorted(dd["arrival"].unique())
block_of = {s: i // BLOCK_SESSIONS for i, s in enumerate(sessions)}
dd = dd.assign(block=dd["arrival"].map(block_of))
y = dd["car_10"].to_numpy(dtype=float)
X = dd[FEATURES].astype(float).to_numpy()
blocks = dd["block"].to_numpy()
days = dd["brief_date"].astype(str).to_numpy()
print(
    f"episodes: {len(dd)} | day-clusters: {len(set(days))} | "
    f"{BLOCK_SESSIONS}-session blocks: {len(set(blocks))} | car_10 mean {y.mean():+.4f} sd {y.std():.4f}"
)

# ---- INFERENCE (attribution stack): multivariate OLS, CR2 + WCB per feature ----
Xi = np.column_stack([np.ones(len(dd)), X])
# median-impute for the inference matrix (tiny missingness; llm_confidence ~77%)
col_med = np.nanmedian(Xi, axis=0)
inds = np.where(np.isnan(Xi))
Xi[inds] = np.take(col_med, inds[1])
res = cluster_ols(y, Xi, days)
print("\ninferencja (car_10 ~ 7 cech, klastry=dni, CR2 + WCB):")
for j, f in enumerate(FEATURES, start=1):
    pw = wild_cluster_bootstrap_p(y, Xi, days, j, n_boot=1999, seed=0)
    print(f"  {f:32s} beta {res.beta[j]:+.4f}  t_cr2 {res.t_cr2[j]:+.2f}  p_wcb {pw:.3f}")

# ---- PREDICTION: elastic net, grouped CV by session blocks ----
cv = GroupKFold(n_splits=min(5, len(set(blocks))))


def enet(alpha):
    return Pipeline(
        [
            ("imp", SimpleImputer(strategy="median")),
            ("sc", StandardScaler()),
            ("en", ElasticNet(alpha=alpha, l1_ratio=0.5, random_state=0, max_iter=10_000)),
        ]
    )


def oof_predictions(model_fn):
    pred = np.full(len(y), np.nan)
    for tr, va in cv.split(X, y, blocks):
        m = model_fn()
        m.fit(X[tr], y[tr])
        pred[va] = m.predict(X[va])
    return pred


print("\npredykcja (elastic net l1_ratio=0.5, foldy = bloki sesyjne):")
oof_primary = None
for alpha in (0.03, 0.1, 0.3):
    pred = oof_predictions(lambda a=alpha: enet(a))
    rho = float(scipy_stats.spearmanr(pred, y)[0])
    tag = " <- PRIMARY (pre-committed)" if alpha == 0.1 else ""
    if alpha == 0.1:
        oof_primary = pred
    print(f"  alpha={alpha:<5} out-of-fold Spearman(pred, car_10) = {rho:+.3f}{tag}")

# ATR baseline on the same folds (train-median-imputed, raw feature as score).
atr = dd[["technical_atr_pct"]].astype(float).to_numpy()
atr_oof = np.full(len(y), np.nan)
for tr, va in cv.split(atr, y, blocks):
    med = np.nanmedian(atr[tr])
    atr_oof[va] = np.where(np.isnan(atr[va][:, 0]), med, atr[va][:, 0])
rho_atr = float(scipy_stats.spearmanr(atr_oof, y)[0])
# a priori direction: high ATR -> worse car_10, so the baseline score is -ATR
rho_atr = -rho_atr
print(f"  baseline -ATR (kierunek a priori): Spearman = {rho_atr:+.3f}")

# ---- model vs baseline: day-cluster bootstrap of the Spearman difference ----
assert oof_primary is not None
rho_model = float(scipy_stats.spearmanr(oof_primary, y)[0])
by_day: dict[str, np.ndarray] = {d: np.where(days == d)[0] for d in sorted(set(days))}
rng = np.random.default_rng(0)
day_keys = list(by_day)
diffs = []
for _ in range(N_BOOT):
    take = rng.choice(len(day_keys), size=len(day_keys), replace=True)
    ix = np.concatenate([by_day[day_keys[t]] for t in take])
    if len(set(y[ix] > np.median(y[ix]))) < 2:
        continue
    rm = float(scipy_stats.spearmanr(oof_primary[ix], y[ix])[0])
    ra = float(scipy_stats.spearmanr(atr_oof[ix], y[ix])[0])
    diffs.append(rm - (-ra))
diffs_arr = np.array(diffs)
lo, hi = np.percentile(diffs_arr, [2.5, 97.5])
print(
    f"\nmodel vs -ATR: delta Spearman = {rho_model - rho_atr:+.3f} "
    f"| bootstrap klastrowy 95% CI [{lo:+.3f}, {hi:+.3f}] "
    f"({'CI zawiera 0 -> nierozroznialne' if lo <= 0 <= hi else 'CI poza 0'})"
)
