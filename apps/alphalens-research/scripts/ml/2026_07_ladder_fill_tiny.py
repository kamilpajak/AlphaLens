"""Tiny LADDER (T5) model — will the first entry tier fill within its TTL?

Layer note: this predicts a LADDER outcome (fill), not an EDGE excess outcome,
so it sits outside the hypothesis-budget selection family (rule 11 targets
EDGE-outcome looks); recorded here as a T5-layer exploratory look. Geometry
features are ALLOWED for ladder-outcome models (ADR 0013 R2 forbids them only
as SELECTION inputs). Mechanical honesty: E1 sits a FIXED 0.5*ATR below the
close by builder construction, so `e1_dist_pct` is ~0.5*ATR%% — the model can
only learn how volatility, trend state and size modulate the mechanical
expectation, and the univariate `e1_dist_pct` baseline IS that expectation.

PRIMARY = survival (time-to-E1-touch, sessions from arrival; right-censored at
TTL or at the newest priced session for pending rows) — the fill EVENT is the
majority class, and pending rows contribute as censored instead of being
dropped, so survival extracts strictly more from the same panel than the
binary view (house rule 6 spirit). Cox PH via statsmodels PHReg with
arrival-session cluster-robust covariance. SECONDARY = 3-feature L1 logistic
for NO_FILL on decided episodes only (29 minority events -> EPV cap 3).

Last run (2026-07-14; 228 episodes / 30 arrival clusters / 195 fills / 22
decided NO_FILL / 11 pending-censored): Cox PH — ma50_distance HR 1.23 per
+1sd, z +5.54 (extended names fill FASTER: the extension keeps falling into
the limit — fill-dynamics cousin of the known extension fade); e1_dist_pct
HR 0.87, z -2.23 (deeper limit -> slower fill = the mechanical expectation,
confirmed); mcap null. Binary secondary is uninformative at 22 minority
events (primary C=0.1 degenerate AUC ~0.5; C=0.3's 0.706 has fold spread
0.47-1.00 = noise) — exactly why survival is the primary view. Exploratory,
T5-layer only; no selection implications.

Pre-committed BEFORE any output: features = {e1_dist_pct, log10_mcap,
technical_ma50_distance_pct}; logistic C=0.1 primary (0.03/0.3 secondary);
baseline = univariate e1_dist_pct with a-priori direction (deeper limit ->
more NO_FILL); GroupKFold by arrival session.
"""

import math

import numpy as np
import pandas as pd
from alphalens_pipeline.data import rs_history
from alphalens_pipeline.paper.calendar import advance_trading_sessions, session_on_or_after
from alphalens_research.diagnostics import edge_stores
from alphalens_research.diagnostics.nofill import TOUCH_EPS
from alphalens_research.diagnostics.options_retro import ticker_episode_dedup
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from statsmodels.duration.hazard_regression import PHReg

FEATURES = ["e1_dist_pct", "log10_mcap", "technical_ma50_distance_pct"]
EX = "XNYS"

outcomes = edge_stores.load_store(edge_stores.HOME / "population_ladders")
setups = edge_stores.setup_index(edge_stores.HOME / "thematic_briefs")
briefs = edge_stores.load_store(edge_stores.HOME / "thematic_briefs")
briefs["ticker"] = briefs["ticker"].astype(str).str.upper()
bix = briefs.set_index(["brief_date", "ticker"])[["technical_ma50_distance_pct", "market_cap"]]
grouped = edge_stores.GroupedDailyCache(rs_history.DEFAULT_RS_HISTORY_ROOT)
newest = edge_stores.newest_session(rs_history.DEFAULT_RS_HISTORY_ROOT)


def bar_field(snap, t, key):
    if not snap:
        return None
    b = snap.get(t.upper())
    if not b:
        return None
    try:
        v = float(b[key])
    except (KeyError, TypeError, ValueError):
        return None
    return v if v > 0 else None


rows = []
for _, r in outcomes[outcomes["plannable"] == True].iterrows():  # noqa: E712
    bd, tk = r["brief_date"], str(r["ticker"]).upper()
    setup = setups.get((bd, tk))
    if not setup or setup.get("status") != "OK":
        continue
    tiers = setup.get("entry_tiers") or []
    close0 = setup.get("asof_close")
    if not tiers or not close0:
        continue
    e1 = float(tiers[0]["limit"])
    ttl = int(setup.get("order_ttl_days") or 7)
    arrival = session_on_or_after(bd, EX)

    # Survival walk: first session in [arrival, arrival+ttl) whose low touches E1.
    duration, event = None, 0
    for i in range(ttl):
        s = advance_trading_sessions(arrival, i, EX)
        if s > newest:
            duration, event = i, 0  # pending: censored at sessions observed so far
            break
        low = bar_field(grouped.get(s), tk, "l")
        if low is None:
            duration, event = i, 0  # data gap: censor at last observable session
            break
        if low <= e1 * (1.0 + TOUCH_EPS):
            duration, event = i + 1, 1
            break
    if duration is None:
        duration, event = ttl, 0  # full TTL elapsed, never touched (decided NO_FILL)
    if duration == 0:
        continue  # zero observed sessions: uninformative for survival

    rec = {
        "brief_date": bd,
        "ticker": tk,
        "arrival": arrival,
        "duration": duration,
        "event": event,
        "decided": bool(event or duration >= ttl),
        "e1_dist_pct": 100.0 * (float(close0) - e1) / float(close0),
    }
    try:
        b = bix.loc[(bd, tk)]
        if isinstance(b, pd.DataFrame):
            b = b.iloc[0]
        rec["technical_ma50_distance_pct"] = b.get("technical_ma50_distance_pct")
        mc = b.get("market_cap")
        try:
            rec["log10_mcap"] = math.log10(float(mc)) if mc and float(mc) > 0 else None
        except (TypeError, ValueError):
            rec["log10_mcap"] = None
    except KeyError:
        pass
    rows.append(rec)

panel = pd.DataFrame(rows)
dd = ticker_episode_dedup(panel)
groups = dd["arrival"].astype(str).to_numpy()
n_fill = int(dd["event"].sum())
decided = dd[dd["decided"]]
n_nofill = int((~decided["event"].astype(bool)).sum())
print(
    f"episodes: {len(dd)} | arrival-session clusters: {len(set(groups))} | "
    f"fills (events): {n_fill} | decided: {len(decided)} (NO_FILL minority: {n_nofill}) | "
    f"pending censored: {len(dd) - len(decided)}"
)

# ---- PRIMARY: Cox PH, time-to-fill, cluster-robust by arrival session ----
surv = dd.dropna(subset=FEATURES)
Xs = surv[FEATURES].astype(float).to_numpy()
Xs = (Xs - Xs.mean(axis=0)) / Xs.std(axis=0)
model = PHReg(
    surv["duration"].to_numpy(dtype=float),
    Xs,
    status=surv["event"].to_numpy(dtype=int),
)
fit = model.fit(groups=surv["arrival"].astype(str).to_numpy())
print(
    f"\nCox PH time-to-fill (n={len(surv)}, events={int(surv['event'].sum())}, "
    "cluster-robust by arrival session; HR>1 = fills FASTER per +1sd):"
)
for f, beta, se in zip(FEATURES, fit.params, fit.bse, strict=True):
    z = beta / se
    print(f"  {f:32s} HR {math.exp(beta):5.2f}  beta {beta:+.3f}  z {z:+.2f}")

# ---- SECONDARY: 3-feature L1 logistic for NO_FILL (decided only) ----
dec = decided.dropna(subset=FEATURES)
yb = (~dec["event"].astype(bool)).astype(float).to_numpy()  # 1 = NO_FILL
Xb = dec[FEATURES].astype(float).to_numpy()
gb = dec["arrival"].astype(str).to_numpy()
n_groups = len(set(gb))
cv = GroupKFold(n_splits=min(5, n_groups))
print(f"\nNO_FILL logistic (decided n={len(dec)}, minority {int(yb.sum())}, {n_groups} clusters):")
for c in (0.03, 0.1, 0.3):
    pipe = Pipeline(
        [
            ("imp", SimpleImputer(strategy="median")),
            ("sc", StandardScaler()),
            ("lr", LogisticRegression(l1_ratio=1.0, solver="liblinear", C=c, random_state=0)),
        ]
    )
    aucs = []
    for tr, va in cv.split(Xb, yb, gb):
        if len(set(yb[va])) < 2 or len(set(yb[tr])) < 2:
            continue
        pipe.fit(Xb[tr], yb[tr])
        aucs.append(roc_auc_score(yb[va], pipe.predict_proba(Xb[va])[:, 1]))
    tag = " <- PRIMARY (pre-committed)" if c == 0.1 else ""
    folds = " ".join(f"{a:.2f}" for a in aucs)
    print(f"  C={c:<5} CV AUC {np.mean(aucs):.3f} (foldy {folds}){tag}")

# Baseline: univariate e1_dist_pct, a-priori direction (deeper limit -> NO_FILL).
base = []
xa = dec[["e1_dist_pct"]].astype(float).to_numpy()
for tr, va in cv.split(xa, yb, gb):
    if len(set(yb[va])) < 2:
        continue
    med = np.nanmedian(xa[tr])
    col = np.where(np.isnan(xa[va][:, 0]), med, xa[va][:, 0])
    base.append(roc_auc_score(yb[va], col))
print(f"  baseline e1_dist_pct (a-priori: glebiej -> NO_FILL): CV AUC {np.mean(base):.3f}")
