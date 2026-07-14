"""ML exercise: predict below-SPY TERMINAL trades from 30 pre-trade features.

Last run (2026-07-14, frame of 134 closed trades / 82 episodes): L1 (C=0.1)
keeps exactly technical_atr_pct + technical_ma50_distance_pct; HistGB depth-2
train AUC 1.000 vs grouped-CV 0.758 vs single-feature ATR 0.730 (memorization
demo). NOTE: builds its frame from population_ladders terminal rows joined to
briefs (the /edge telemetry population).

EXPLORATORY / EDUCATIONAL ONLY — burnt, fill-dependent panel (N=134, 45 below).
The honest headline is the gap between train AUC and grouped-CV AUC.
Leakage control: features are ONLY columns present in the thematic_briefs store
(pre-trade by construction); everything from the ladder store (outcomes,
classifications, holding, charts) is excluded. CV groups = brief_date so
overlapping holding windows / same-day cohorts never straddle train/validation.
"""

import warnings

import numpy as np
from alphalens_research.diagnostics import edge_stores
from alphalens_research.diagnostics.options_retro import ticker_episode_dedup
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# Only silence sklearn's noisy FutureWarnings; convergence warnings stay visible.
warnings.filterwarnings("ignore", category=FutureWarning)
RNG = 0

_out = edge_stores.load_store(edge_stores.HOME / "population_ladders")
_term = _out[(_out.get("terminal") == True) & _out["market_excess_return"].notna()].copy()  # noqa: E712
_term["ticker"] = _term["ticker"].astype(str).str.upper()
_briefs_all = edge_stores.load_store(edge_stores.HOME / "thematic_briefs")
_briefs_all["ticker"] = _briefs_all["ticker"].astype(str).str.upper()
frame = _term.merge(_briefs_all, on=["brief_date", "ticker"], how="left", suffixes=("", "_brief"))

# ---- feature whitelist: briefs-side numeric columns only (pre-trade) ----
briefs = edge_stores.load_store(edge_stores.HOME / "thematic_briefs")
brief_cols = set(briefs.columns)
drop_always = {"brief_date", "ticker", "asof", "generated_at"}
candidates = [c for c in frame.columns if c in brief_cols and c not in drop_always]
num = frame[candidates].select_dtypes(include=[np.number]).columns.tolist()
# drop all-null / near-empty columns (options_* pre-07-07, mstate_* pre-07-05 etc.)
# NOTE: this coverage filter peeks at the FULL panel's X (not y) before CV —
# a common exploratory shortcut; results are mildly optimistic vs per-fold filtering.
keep = [c for c in num if frame[c].notna().mean() >= 0.6 and frame[c].nunique(dropna=True) > 1]
print(f"features: {len(keep)} numeric pre-trade columns (of {len(candidates)} brief cols)")

# ---- episode dedup, target, groups ----
dd = ticker_episode_dedup(frame)
y = (dd["market_excess_return"] < 0).astype(int).to_numpy()
X = dd[keep].to_numpy(dtype=float)
groups = dd["brief_date"].astype(str).to_numpy()
print(f"episodes: {len(dd)} | below-SPY: {y.sum()} | brief-day groups: {len(set(groups))}")

cv = GroupKFold(n_splits=5)


def run_model(name, model):
    tr_aucs, cv_aucs = [], []
    imps = np.zeros(len(keep))
    for tr, va in cv.split(X, y, groups):
        model.fit(X[tr], y[tr])
        tr_aucs.append(roc_auc_score(y[tr], model.predict_proba(X[tr])[:, 1]))
        if len(set(y[va])) > 1:
            cv_aucs.append(roc_auc_score(y[va], model.predict_proba(X[va])[:, 1]))
            pi = permutation_importance(
                model, X[va], y[va], scoring="roc_auc", n_repeats=20, random_state=RNG
            )
            imps += pi.importances_mean
    print(f"\n{name}:")
    print(
        f"  train AUC {np.mean(tr_aucs):.3f} | grouped-CV AUC {np.mean(cv_aucs):.3f} "
        f"(fold spread {min(cv_aucs):.3f}..{max(cv_aucs):.3f}) | GAP {np.mean(tr_aucs) - np.mean(cv_aucs):+.3f}"
    )
    top = sorted(zip(keep, imps / cv.get_n_splits(), strict=True), key=lambda t: -abs(t[1]))[:8]
    print("  permutation importance (held-out, top 8):")
    for c, v in top:
        print(f"    {c:38s} {v:+.4f}")
    return np.mean(cv_aucs)


l1 = Pipeline(
    [
        ("imp", SimpleImputer(strategy="median")),
        ("sc", StandardScaler()),
        ("lr", LogisticRegression(l1_ratio=1.0, solver="liblinear", C=0.1, random_state=RNG)),
    ]
)
gb = Pipeline(
    [
        ("imp", SimpleImputer(strategy="median")),
        (
            "gb",
            HistGradientBoostingClassifier(
                max_depth=2, max_iter=150, learning_rate=0.05, min_samples_leaf=15, random_state=RNG
            ),
        ),
    ]
)
auc_l1 = run_model("L1 logistic (C=0.1)", l1)
auc_gb = run_model("HistGradientBoosting (depth 2)", gb)

# ---- L1 surviving coefficients on the full (deduped) sample, for reading ----
l1.fit(X, y)
coefs = l1.named_steps["lr"].coef_[0]
nz = [(c, w) for c, w in zip(keep, coefs, strict=True) if abs(w) > 1e-6]
print(f"\nL1 fit on full sample: {len(nz)} of {len(keep)} features survive the penalty:")
for c, w in sorted(nz, key=lambda t: -abs(t[1])):
    print(f"    {c:38s} {w:+.3f}")

# ---- single-feature baselines, same CV scheme ----
print("\nsingle-feature baselines (grouped-CV AUC, same folds):")
for feat in [
    "technical_atr_pct",
    "technical_ma50_distance_pct",
    "n_gates_passed",
    "llm_confidence",
    "market_cap",
]:
    if feat not in keep:
        print(f"    {feat:38s} (absent)")
        continue
    xa = dd[[feat]].to_numpy(dtype=float)
    aucs = []
    for tr, va in cv.split(xa, y, groups):
        med = np.nanmedian(xa[tr])
        col = np.where(np.isnan(xa[va][:, 0]), med, xa[va][:, 0])
        if len(set(y[va])) > 1:
            aucs.append(max(roc_auc_score(y[va], col), roc_auc_score(y[va], -col)))
    print(f"    {feat:38s} {np.mean(aucs):.3f}")
