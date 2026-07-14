"""Tiny SIGNAL model — L1 logistic on pre-trade brief features (ADR 0013 T2).

Last run (2026-07-14, matured through brief_date 2026-06-26; stores synced
07-13/14): 160 episodes / 21 arrival-session clusters (29 brief-days) / 81
below (50.6%); split guard dropped 0. PRIMARY C=0.1 grouped-CV AUC 0.584
(folds 0.53-0.66, GAP +0.066) vs fixed-direction ATR baseline 0.598
(sign-agnostic pessimistic bound 0.619) -> model TIES the fixed-direction
ATR baseline (0.584 vs 0.598 after the unit fix; a difference this size is
not distinguishable under session clustering); both are marginal vs a
clustered null (~0.58 at this N, not 0.50). L1 keeps only ma50_distance
(+0.253) and ATR (+0.104).

Pre-committed BEFORE any CV result was seen:
- target: car_10 < 0 (fill-independent market-adjusted BHAR vs SPY, k=10)
- unit: ticker-episode (chained 5-session dedup); independence unit for the
  CV groups = ARRIVAL SESSION (`session_on_or_after(brief_date, "XNYS")`) —
  weekend briefs collapse into Monday, so several brief_dates can share one
  session and grouping by raw brief_date under-clusters
- features (7, the EPV cap): technical_atr_pct, technical_ma50_distance_pct,
  technical_pct_off_52w_high, technical_rsi, llm_confidence, log10_mcap,
  n_gates_passed
- validation: 5-fold GroupKFold by arrival session; PRIMARY C=0.1 (secondary
  0.03, 0.3 reported, no selection)
- baselines to beat: base rate; univariate ATR with the a-priori direction
  (higher ATR -> below-SPY) on the same folds; the sign-agnostic
  max(auc, 1-auc) is a labelled pessimistic bound only
Use: display / hypothesis-generation ONLY (ADR 0013 R2). This is a third look
at the SAME burnt panel (not an independent confirmation).
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
from alphalens_research.diagnostics.options_retro import ticker_episode_dedup
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
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
# Split guard: the grouped store is split-adjusted only as-of its own
# download date, NOT retroactively, so a split inside the outcome window
# shows up as a raw day-over-day close jump. Bounds are deliberately wide
# (no legitimate close/close ratio should leave them in one session).
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
    rec = {"brief_date": bd, "ticker": tk, "arrival": arr, "below": 1.0 if car < 0 else 0.0}
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
y = dd["below"].to_numpy()
X = dd[FEATURES].astype(float).to_numpy()
# Independence unit = arrival session (weekend briefs collapse into Monday).
groups = dd["arrival"].astype(str).to_numpy()
print(
    f"episodes: {len(dd)} | below-SPY: {int(y.sum())} ({y.mean():.1%}) | "
    f"arrival-session clusters: {len(set(groups))} "
    f"(brief-day count: {dd['brief_date'].nunique()}) | features: {len(FEATURES)}"
)
print("feature coverage:", {f: f"{dd[f].notna().mean():.0%}" for f in FEATURES})

cv = GroupKFold(n_splits=5)


def pipe(c):
    return Pipeline(
        [
            ("imp", SimpleImputer(strategy="median")),
            ("sc", StandardScaler()),
            ("lr", LogisticRegression(l1_ratio=1.0, solver="liblinear", C=c, random_state=0)),
        ]
    )


print("\nmodel (7 cech, L1):")
for c in (0.03, 0.1, 0.3):
    tr_a, cv_a, briers = [], [], []
    for tr, va in cv.split(X, y, groups):
        m = pipe(c).fit(X[tr], y[tr])
        tr_a.append(roc_auc_score(y[tr], m.predict_proba(X[tr])[:, 1]))
        p = m.predict_proba(X[va])[:, 1]
        cv_a.append(roc_auc_score(y[va], p))
        briers.append(brier_score_loss(y[va], p))
    tag = " <- PRIMARY (pre-committed)" if c == 0.1 else ""
    print(
        f"  C={c:<5} train {np.mean(tr_a):.3f} | CV {np.mean(cv_a):.3f} "
        f"(foldy {' '.join(f'{a:.2f}' for a in cv_a)}) | GAP {np.mean(tr_a) - np.mean(cv_a):+.3f} "
        f"| Brier {np.mean(briers):.3f}{tag}"
    )

print("\nbaseliny (te same foldy):")
atr = dd[["technical_atr_pct"]].astype(float).to_numpy()
aucs_fixed, aucs_pess = [], []
for tr, va in cv.split(atr, y, groups):
    med = np.nanmedian(atr[tr])
    col = np.where(np.isnan(atr[va][:, 0]), med, atr[va][:, 0])
    # PRIMARY: a-priori direction — higher ATR -> more likely below-SPY.
    auc = roc_auc_score(y[va], col)
    aucs_fixed.append(auc)
    aucs_pess.append(max(auc, 1 - auc))
print(
    f"  ATR (kierunek a priori, PRIMARY): CV AUC {np.mean(aucs_fixed):.3f} "
    f"(foldy {' '.join(f'{a:.2f}' for a in aucs_fixed)})"
)
print(
    f"  ATR pessimistic bound (sign-agnostic, secondary): CV AUC {np.mean(aucs_pess):.3f} "
    f"(foldy {' '.join(f'{a:.2f}' for a in aucs_pess)})"
)
print(f"  base rate (zawsze 'powyzej'): accuracy {1 - y.mean():.1%}, AUC 0.500")

m = pipe(0.1).fit(X, y)
coefs = m.named_steps["lr"].coef_[0]
print("\nwspolczynniki L1 (C=0.1, pelna probka, znak + = wieksze ryzyko ponizej SPY):")
for f, w in sorted(zip(FEATURES, coefs, strict=True), key=lambda t: -abs(t[1])):
    print(f"  {f:32s} {w:+.3f}{'  (wyzerowana)' if abs(w) < 1e-6 else ''}")
