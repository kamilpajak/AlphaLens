"""ML exercise: predict below-SPY TERMINAL trades from pre-trade features.

Last run (2026-07-14, frame capped at DISCOVERY_FREEZE 2026-07-05): N = 80
EPISODES (132 terminal rows, max brief_date 2026-07-01, 22 arrival-session
clusters, 27 below-SPY); 23 realized features. L1 (C=0.1) grouped-CV AUC
0.643 and keeps exactly technical_atr_pct + technical_ma50_distance_pct;
HistGB depth-2 train AUC 1.000 vs grouped-CV 0.606 (GAP +0.394,
memorization demo); fixed-direction ATR baseline alone 0.747 — BOTH models
lose to the univariate baseline. NOTE: builds its frame from
population_ladders terminal rows joined to briefs (the /edge telemetry
population).

RECORDED BURN: the first run of this script (before the freeze cap existed)
consumed 2 terminal rows with brief_date PAST the 2026-07-05 discovery
freeze; those episodes are burnt for any later confirmation window. The cap
below prevents further leakage, it does not un-burn them.

EXPLORATORY / EDUCATIONAL ONLY — burnt, fill-dependent panel; N is reported
in EPISODES (the independence unit), not trades. The honest headline is the
gap between train AUC and grouped-CV AUC.
Leakage control: features are ONLY columns present in the thematic_briefs
store (pre-trade by construction); everything from the ladder store
(outcomes, classifications, holding, charts) is excluded. CV groups =
ARRIVAL SESSION (`session_on_or_after(brief_date, "XNYS")`) so weekend
briefs collapse into their shared Monday session and same-session cohorts
never straddle train/validation.
*Deliberately breaks house rules 3-4 (fill-dependent target, EPV ignored) as
the overfitting demo; its coverage pre-filter runs on the full panel, so
treat numbers as mildly optimistic.*
"""

import datetime as dt
import warnings

import numpy as np
from alphalens_pipeline.paper.calendar import session_on_or_after
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
EX = "XNYS"
# Hypothesis-budget ledger rule 3: the panel up to 2026-07-05 is DISCOVERY.
# Episodes after the freeze are reserved for confirmation and must not be
# consumed by exploratory looks.
DISCOVERY_FREEZE = dt.date(2026, 7, 5)
# The ONLY columns the two stores may share: the join keys plus known
# non-numeric metadata. A new shared column would be silently suffixed
# "_brief" by the merge and dropped from the feature whitelist — fail loudly
# instead so the overlap is handled consciously.
_MERGE_KEYS = {"brief_date", "ticker"}
_KNOWN_SHARED_NON_NUMERIC = {"theme", "scorer_config_version"}

_out = edge_stores.load_store(edge_stores.HOME / "population_ladders")
_term = _out[(_out.get("terminal") == True) & _out["market_excess_return"].notna()].copy()  # noqa: E712
_term = _term[_term["brief_date"] <= DISCOVERY_FREEZE]
_term["ticker"] = _term["ticker"].astype(str).str.upper()
_briefs_all = edge_stores.load_store(edge_stores.HOME / "thematic_briefs")
_briefs_all["ticker"] = _briefs_all["ticker"].astype(str).str.upper()
_shared = set(_term.columns) & set(_briefs_all.columns)
assert _shared == _MERGE_KEYS | _KNOWN_SHARED_NON_NUMERIC, (
    f"unexpected column overlap between stores: {sorted(_shared)} "
    f"(expected {sorted(_MERGE_KEYS | _KNOWN_SHARED_NON_NUMERIC)})"
)
frame = _term.merge(_briefs_all, on=["brief_date", "ticker"], how="left", suffixes=("", "_brief"))
frame["arrival"] = frame["brief_date"].map(lambda d: session_on_or_after(d, EX))

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
print("realized feature list:", keep)

# ---- episode dedup, target, groups ----
dd = ticker_episode_dedup(frame)
y = (dd["market_excess_return"] < 0).astype(int).to_numpy()
X = dd[keep].to_numpy(dtype=float)
# Independence unit = arrival session (weekend briefs collapse into Monday).
groups = dd["arrival"].astype(str).to_numpy()
print(
    f"frame fingerprint: {len(frame)} terminal rows | {len(dd)} episodes | "
    f"max brief_date {frame['brief_date'].max()} (freeze {DISCOVERY_FREEZE})"
)
print(f"episodes: {len(dd)} | below-SPY: {y.sum()} | arrival-session clusters: {len(set(groups))}")

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

# ---- PRIMARY baseline: ATR with the a-priori direction, same CV scheme ----
# (higher ATR -> more likely below-SPY; the direction is committed a priori)
print("\nbaseline PRIMARY — ATR, kierunek a priori (grouped-CV AUC, same folds):")
xa = dd[["technical_atr_pct"]].to_numpy(dtype=float)
aucs_fixed = []
for tr, va in cv.split(xa, y, groups):
    med = np.nanmedian(xa[tr])
    col = np.where(np.isnan(xa[va][:, 0]), med, xa[va][:, 0])
    if len(set(y[va])) > 1:
        aucs_fixed.append(roc_auc_score(y[va], col))
print(f"    technical_atr_pct (+ATR score)     {np.mean(aucs_fixed):.3f}")

# ---- single-feature pessimistic bounds (sign-agnostic, secondary) ----
print("\nsingle-feature pessimistic bounds (sign-agnostic max(auc, 1-auc), secondary):")
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
