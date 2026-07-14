"""Continuous-target upgrade of the tiny SIGNAL exercise (house rules 6-9).

Applies the 2026-07-14 external methodological review: the outcome is the
CONTINUOUS car_10 (no dichotomization), the predictive model is elastic net
(correlated ATR/ma50 pair), CV folds are built from contiguous 5-session
arrival blocks (halves the overlapping-label leakage vs per-day groups at
this history length; a runt final block is merged into its predecessor so no
fold rests on a single session), and the model-vs-ATR comparison uses an
arrival-session-cluster bootstrap instead of an invalid naive test.
Inference side reuses the attribution stack (cluster_ols CR2 + wild cluster
bootstrap), clustered on arrival sessions.

Validation caveat: the CV folds are ANTI-CAUSAL — each block is predicted
from a model trained on sessions BOTH before and after it. Acceptable for a
cross-sectional claim ("do these features rank same-day candidates?") only;
any time-series / deployment claim needs walk-forward folds.

Last run (2026-07-14, matured through brief_date 2026-06-26; stores synced
07-13/14; 160 episodes / 21 arrival-session clusters (29 brief-days) /
4 blocks; split guard dropped 0):
- inference (CR2+WCB, clusters = arrival sessions, 7 coefficients, family
  bar .05/7 = .0071): ATR beta -0.0167 p_wcb .038; log10_mcap beta -0.0584
  p_wcb .024 (bigger cap -> worse car_10) — INVISIBLE to the binary model
  (L1 zeroed mcap), i.e. the continuous target recovered a candidate, but
  NOTHING clears the family bar: exploratory notes only, matches the June
  first-look null on mcap. Complete-case sensitivity (n=123, 18 clusters):
  ATR p_wcb .093, log10_mcap p_wcb .025 — the mcap coefficient is not an
  imputation artifact.
- prediction (corrected metric): PRIMARY alpha=0.1 pooled rank-within-fold
  Spearman +0.032 (folds: degenerate, +0.12, +0.26, -0.24); alpha=0.03
  +0.265; alpha=0.3 NaN (all folds degenerate); baseline -ATR +0.185.
  Model-vs-baseline delta -0.153, arrival-cluster bootstrap 95% CI
  [-0.374, +0.073], 0 skipped draws -> the CI includes 0: UNINFORMATIVE at
  this N. The previously reported "primary significantly worse than
  baseline, CI excludes 0" was the pooled-OOF artifact (primary predictions
  near-constant in 2 of the old 5 folds), not a real difference. No test
  was run for the secondaries. Headline unchanged: nothing beats ATR alone.

METRIC REPAIR (adversarial review 2026-07-14): the previously reported
pooled-OOF Spearman was an ARTIFACT — when a fold's predictions are
near-constant (over-shrunk alpha), pooling raw predictions across folds lets
between-fold level differences masquerade as (anti-)signal. The prior
headline "primary significantly worse than baseline, CI excludes 0" was that
artifact, not a real difference. Corrected protocol: per-fold Spearman
reported per alpha (NaN when a fold's predictions are constant), and the
pooled statistic ranks predictions AND outcomes WITHIN each validation fold
before pooling (ranks normalized by fold size — folds are unequal, so raw
within-fold ranks would leak fold size into the pooled correlation).

Pre-committed BEFORE looking at any output: the same 7 features as
2026_07_signal_below_spy_tiny.py; elastic net l1_ratio=0.5, PRIMARY
alpha=0.1 (0.03 / 0.3 secondary, no selection); Spearman rank correlation
as the prediction metric; B=4000 cluster-bootstrap resamples.
- LESSON (kept from the first run): the alpha grid was carried over from the
  classification exercise and is mis-scaled for a target with sd~0.12
  (alpha=0.1 over-shrinks toward a near-constant predictor); pre-commit alpha
  relative to target variance next time (the August spec does).

This is a third look at the SAME burnt panel (not an independent
confirmation).
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
FAMILY_BAR = 0.05 / len(FEATURES)
# Split guard: the grouped store is split-adjusted only as-of its own
# download date, NOT retroactively, so a split inside the outcome window
# shows up as a raw day-over-day close jump.
SPLIT_RATIO_LO, SPLIT_RATIO_HI = 0.55, 1.8

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


def has_split_jump(ticker, anchor, horizon):
    """True when any day-over-day close ratio in [anchor, horizon] leaves
    [SPLIT_RATIO_LO, SPLIT_RATIO_HI] — an unadjusted split jump."""
    prev_close = None
    s = anchor
    while s <= horizon:
        c = close(grouped.get(s), ticker)
        if c is not None:
            if prev_close is not None and not (SPLIT_RATIO_LO <= c / prev_close <= SPLIT_RATIO_HI):
                return True
            prev_close = c
        s = advance_trading_sessions(s, 1, EX)
    return False


rows = []
n_split_dropped = 0
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
    if has_split_jump(tk, anc, hor):
        n_split_dropped += 1
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

print(f"split-guard: dropped {n_split_dropped} episodes (unadjusted split jump in window)")
panel = pd.DataFrame(rows)
dd = ticker_episode_dedup(panel)
# Contiguous BLOCK_SESSIONS-session arrival blocks as CV groups (house rule 9);
# a runt final block (< BLOCK_SESSIONS sessions) is merged into its predecessor.
sessions = sorted(dd["arrival"].unique())
n_blocks = math.ceil(len(sessions) / BLOCK_SESSIONS)
if len(sessions) % BLOCK_SESSIONS and n_blocks > 1:
    n_blocks -= 1
block_of = {s: min(i // BLOCK_SESSIONS, n_blocks - 1) for i, s in enumerate(sessions)}
dd = dd.assign(block=dd["arrival"].map(block_of))
y = dd["car_10"].to_numpy(dtype=float)
X = dd[FEATURES].astype(float).to_numpy()
blocks = dd["block"].to_numpy()
# Independence unit = arrival session (weekend briefs collapse into Monday;
# several brief_dates can share one session).
clusters = dd["arrival"].astype(str).to_numpy()
print(
    f"episodes: {len(dd)} | arrival-session clusters: {len(set(clusters))} "
    f"(brief-day count: {dd['brief_date'].nunique()}) | "
    f"{BLOCK_SESSIONS}-session blocks: {len(set(blocks))} | "
    f"car_10 mean {y.mean():+.4f} sd {y.std():.4f}"
)

# ---- INFERENCE (attribution stack): multivariate OLS, CR2 + WCB per feature ----
Xi = np.column_stack([np.ones(len(dd)), X])
# Median-impute for the inference matrix. NOTE: llm_confidence missingness is
# STRUCTURAL — entire early brief_dates lack the column — so imputation mixes
# regimes; see the complete-case sensitivity block below.
col_med = np.nanmedian(Xi, axis=0)
inds = np.where(np.isnan(Xi))
Xi[inds] = np.take(col_med, inds[1])
res = cluster_ols(y, Xi, clusters)
print(
    f"\ninferencja (car_10 ~ 7 cech, klastry = sesje przybycia, CR2 + WCB; "
    f"prog rodziny .05/7 = {FAMILY_BAR:.4f}):"
)
for j, f in enumerate(FEATURES, start=1):
    pw = wild_cluster_bootstrap_p(y, Xi, clusters, j, n_boot=1999, seed=0)
    verdict = "CLEARS family bar" if pw < FAMILY_BAR else "below family bar"
    print(
        f"  {f:32s} beta {res.beta[j]:+.4f}  t_cr2 {res.t_cr2[j]:+.2f}  p_wcb {pw:.3f}  [{verdict}]"
    )

# Complete-case sensitivity: rows with ALL 7 features present (no imputation),
# for the two notable coefficients from the imputed fit.
cc = dd[FEATURES].notna().all(axis=1).to_numpy()
y_cc = y[cc]
Xi_cc = np.column_stack([np.ones(int(cc.sum())), X[cc]])
cl_cc = clusters[cc]
res_cc = cluster_ols(y_cc, Xi_cc, cl_cc)
print(
    f"\ncomplete-case sensitivity (wiersze z kompletem 7 cech: n={int(cc.sum())}, "
    f"klastry={res_cc.n_clusters}):"
)
for f in ("technical_atr_pct", "log10_mcap"):
    j = FEATURES.index(f) + 1
    pw = wild_cluster_bootstrap_p(y_cc, Xi_cc, cl_cc, j, n_boot=1999, seed=0)
    print(f"  {f:32s} beta {res_cc.beta[j]:+.4f}  t_cr2 {res_cc.t_cr2[j]:+.2f}  p_wcb {pw:.3f}")

# ---- PREDICTION: elastic net, grouped CV by session blocks ----
cv = GroupKFold(n_splits=min(5, len(set(blocks))))
fold_indices = list(cv.split(X, y, blocks))


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
    for tr, va in fold_indices:
        m = model_fn()
        m.fit(X[tr], y[tr])
        pred[va] = m.predict(X[va])
    return pred


def rank_within_fold(values):
    """Replace values by their NORMALIZED ranks (rank/(n+1)) WITHIN each fold.

    Pooling raw OOF predictions across folds is an artifact factory when a
    fold's predictions are near-constant (between-fold level differences leak
    into the pooled correlation); within-fold ranking removes fold levels.
    Ranks are normalized to (0,1) because folds have unequal sizes — raw
    ranks would leak fold SIZE into the pooled correlation (a constant-pred
    fold's tied rank is (n+1)/2, which differs per fold).
    """
    ranked = np.full(len(values), np.nan)
    for _, va in fold_indices:
        ranked[va] = scipy_stats.rankdata(values[va]) / (len(va) + 1)
    return ranked


def pooled_spearman(a_rank, b_rank):
    """Spearman on pooled within-fold ranks; NaN when either side is constant."""
    if np.ptp(a_rank) == 0 or np.ptp(b_rank) == 0:
        return float("nan")
    return float(scipy_stats.spearmanr(a_rank, b_rank)[0])


def per_fold_spearman(pred):
    out = []
    for _, va in fold_indices:
        if np.ptp(pred[va]) == 0:
            out.append(float("nan"))
        else:
            out.append(float(scipy_stats.spearmanr(pred[va], y[va])[0]))
    return out


y_rank = rank_within_fold(y)
print("\npredykcja (elastic net l1_ratio=0.5, foldy = bloki sesyjne):")
print("  pooled = Spearman na rangach LICZONYCH W OBREBIE FOLDU (metric repair 2026-07-14)")
oof_primary = None
for alpha in (0.03, 0.1, 0.3):
    pred = oof_predictions(lambda a=alpha: enet(a))
    folds = per_fold_spearman(pred)
    fold_txt = " ".join("fold degenerate" if math.isnan(r) else f"{r:+.2f}" for r in folds)
    pooled = pooled_spearman(rank_within_fold(pred), y_rank)
    pooled_txt = "NaN (all folds degenerate)" if math.isnan(pooled) else f"{pooled:+.3f}"
    tag = " <- PRIMARY (pre-committed)" if alpha == 0.1 else ""
    if alpha == 0.1:
        oof_primary = pred
    print(
        f"  alpha={alpha:<5} pooled rank-within-fold Spearman = {pooled_txt} | foldy: {fold_txt}{tag}"
    )

# ATR baseline on the same folds (train-median-imputed; a-priori direction:
# high ATR -> worse car_10, so the baseline score is -ATR).
atr = dd[["technical_atr_pct"]].astype(float).to_numpy()
atr_score = np.full(len(y), np.nan)
for tr, va in fold_indices:
    med = np.nanmedian(atr[tr])
    atr_score[va] = -np.where(np.isnan(atr[va][:, 0]), med, atr[va][:, 0])
atr_rank = rank_within_fold(atr_score)
rho_atr = pooled_spearman(atr_rank, y_rank)
print(f"  baseline -ATR (kierunek a priori): pooled rank-within-fold Spearman = {rho_atr:+.3f}")

# ---- model vs baseline: arrival-session-cluster bootstrap of the difference ----
assert oof_primary is not None
pred_rank = rank_within_fold(oof_primary)
rho_model = pooled_spearman(pred_rank, y_rank)
by_cluster: dict[str, np.ndarray] = {c: np.where(clusters == c)[0] for c in sorted(set(clusters))}
rng = np.random.default_rng(0)
cluster_keys = list(by_cluster)
diffs = []
n_skipped = 0
for _ in range(N_BOOT):
    take = rng.choice(len(cluster_keys), size=len(cluster_keys), replace=True)
    ix = np.concatenate([by_cluster[cluster_keys[t]] for t in take])
    # Guard: a resample where any pooled array is constant has no defined
    # Spearman — skip it (counted below) instead of poisoning the CI.
    if np.ptp(pred_rank[ix]) == 0 or np.ptp(atr_rank[ix]) == 0 or np.ptp(y_rank[ix]) == 0:
        n_skipped += 1
        continue
    rm = float(scipy_stats.spearmanr(pred_rank[ix], y_rank[ix])[0])
    ra = float(scipy_stats.spearmanr(atr_rank[ix], y_rank[ix])[0])
    diffs.append(rm - ra)
diffs_arr = np.array(diffs)
lo, hi = np.percentile(diffs_arr, [2.5, 97.5])
print(
    f"\nmodel vs -ATR: delta pooled Spearman = {rho_model - rho_atr:+.3f} "
    f"| bootstrap klastrowy (sesje przybycia) 95% CI [{lo:+.3f}, {hi:+.3f}] "
    f"({'CI zawiera 0 -> nierozroznialne' if lo <= 0 <= hi else 'CI poza 0'}) "
    f"| skipped degenerate draws: {n_skipped}/{N_BOOT}"
)
