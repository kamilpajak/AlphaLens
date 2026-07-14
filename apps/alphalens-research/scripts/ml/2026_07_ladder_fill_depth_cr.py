"""Fill-DEPTH v2 for the LADDER layer (T5) — continuation-ratio (CR) model.

Layer note: this predicts a LADDER outcome (how DEEP the entry ladder fills),
not an EDGE excess outcome, so it sits outside the hypothesis-budget selection
family (rule 11 targets EDGE-outcome looks); recorded here as a T5-layer
exploratory look. Geometry features are ALLOWED for ladder-outcome models
(ADR 0013 R2 forbids them only as SELECTION inputs). Methodology per external
review: continuation-ratio on stacked per-transition rows (Tobit rejected at
this N); depth is a SEQUENTIAL mechanism — tier k+1 can only fill after tier k
— so per-transition risk sets are the correct decomposition.

DEPTH DECIDEDNESS: fills can only occur inside the entry TTL, so depth is
FINAL when the entry window is closed — decided = terminal OR
advance_trading_sessions(arrival, order_ttl_days, XNYS) <= newest grouped
session. An ongoing POSITION past its entry TTL has final depth.

STACKING (immortal-time trap avoided): episode enters transition-k row
(k in {0,1,2}) iff its ladder HAS a tier k+1 AND depth >= k; y=1 iff
depth >= k+1. A row is EXCLUDED (censored) iff depth == k AND not decided —
an ongoing-at-depth-k episode counts YES only for transitions < k (already
decided facts) and is censored at k. Informative-censoring caveat: the
ongoing-at-k set may be volatility-selected (calm names stay pending longer),
so censoring is not guaranteed ignorable at this N.

MODEL (pre-committed BEFORE any output): GLM Binomial on the stacked rows,
logit link primary (cloglog printed as secondary), covariates =
{spacing_to_next_atr = (level_k - limit_{k+1})/atr with level_0 = asof_close,
technical_ma50_distance_pct} + transition dummies k1/k2 (k0 reference),
NOTHING else; continuous covariates standardized on the stacked data.
Primary covariance cluster-robust by EPISODE; plus an arrival-session-cluster
bootstrap (resample sessions of episodes with replacement, refit, B=2000,
seed 0) 95% CI for the two shared covariate coefficients. No Firth in deps;
the pooled model sharing coefficients across transitions IS the Firth-free
mitigation for the sparse t2 — its raw contingency is printed and the t2
dummy is flagged if unstable. Mechanical honesty: tier spacing is ATR-scaled
BY BUILDER construction, so spacing effects partially re-express volatility.

Last run (2026-07-14, post-verifier decidedness fix — replay-frontier
last_resolved_session must cover the entry window, 2 rows re-censored):
245 plannable episodes / 31 arrival clusters; decided 172 / censored 73;
435 stacked rows, 0 covariate drops. Transitions: t0 n=223 (advance 194,
rate .870, 22 excluded), t1 n=153 (77, .503, 41 excluded), t2 n=59 (25,
.424, 5 excluded). PRIMARY logit (episode-cluster): spacing_to_next_atr
-1.096 z -5.51 (wider gap to the next tier -> less likely to advance =
mechanics, strongly confirmed); ma50_distance +0.842 z +4.00 (extended
names fill DEEPER — depth-side cousin of the tiny script's fill-faster
HR 1.23); k1 -0.348 z -0.81; k2 -1.308 z -3.10. Cloglog agrees (spacing
-0.719 z -5.74; ma50 +0.429 z +4.24). Session-cluster bootstrap 95% CI:
spacing [-1.637, -0.756], ma50 [+0.448, +1.425] — both exclude 0. t2 raw
contingency: advance 25 / stop 34; spacing <= median (0.90 ATR) advances
17/30 vs 8/29 above; t2 dummy STABLE. corr(spacing, technical_atr_pct)
+0.004 in-sample. Verifier drop-3-clusters check: both headline
coefficients sign-stable, |z| >= 2.7 in every variant. Exploratory,
T5-layer only; no selection implications.
"""

import math

import numpy as np
import pandas as pd
import statsmodels.api as sm
from alphalens_pipeline.data import rs_history
from alphalens_pipeline.paper.calendar import advance_trading_sessions, session_on_or_after
from alphalens_research.diagnostics import edge_stores
from alphalens_research.diagnostics.options_retro import ticker_episode_dedup

EX = "XNYS"
SHARED_COVARIATES = ["spacing_to_next_atr", "technical_ma50_distance_pct"]
DESIGN_COLUMNS = ["const", *SHARED_COVARIATES, "k1", "k2"]
MAX_TIERS = 3
B_BOOT = 2000
BOOT_SEED = 0
# Instability flag thresholds for the sparse-t2 dummy (logit scale).
T2_SE_UNSTABLE = 1.5
T2_COEF_UNSTABLE = 4.0

outcomes = edge_stores.load_store(edge_stores.HOME / "population_ladders")
setups = edge_stores.setup_index(edge_stores.HOME / "thematic_briefs")
briefs = edge_stores.load_store(edge_stores.HOME / "thematic_briefs")
briefs["ticker"] = briefs["ticker"].astype(str).str.upper()
bix = briefs.set_index(["brief_date", "ticker"])[
    ["technical_ma50_distance_pct", "technical_atr_pct"]
]
newest = edge_stores.newest_session(rs_history.DEFAULT_RS_HISTORY_ROOT)
assert newest is not None, "grouped-daily store is empty — rsync ~/.alphalens from the VPS"

plannable = outcomes[outcomes["plannable"] == True].copy()  # noqa: E712
plannable["ticker"] = plannable["ticker"].astype(str).str.upper()
dd = ticker_episode_dedup(plannable)

episodes = []
n_no_setup = 0
for _, r in dd.iterrows():
    bd, tk = r["brief_date"], r["ticker"]
    setup = setups.get((bd, tk))
    if not setup or setup.get("status") != "OK":
        n_no_setup += 1
        continue
    tiers = (setup.get("entry_tiers") or [])[:MAX_TIERS]
    close0, atr = setup.get("asof_close"), setup.get("atr")
    # NaN is truthy and NaN <= 0 is False — guard explicitly, not via falsiness.
    if (
        not tiers
        or close0 is None
        or atr is None
        or math.isnan(float(close0))
        or math.isnan(float(atr))
        or float(atr) <= 0
    ):
        n_no_setup += 1
        continue
    depth_raw = r["tiers_filled_count"]
    if depth_raw is None or (isinstance(depth_raw, float) and math.isnan(depth_raw)):
        n_no_setup += 1
        continue
    depth = int(float(depth_raw))
    limits = [float(t["limit"]) for t in tiers]
    assert depth <= len(limits), f"depth {depth} exceeds n_tiers {len(limits)} for {bd} {tk}"
    # The outcome store's entry_ttl_days is what the monitor actually enforced;
    # assert parity with the setup so silent divergence fails loud.
    ttl = int(setup.get("order_ttl_days") or 7)
    store_ttl = r.get("entry_ttl_days")
    if store_ttl is not None and not pd.isna(store_ttl):
        assert int(store_ttl) == ttl, f"ttl mismatch {bd} {tk}: setup {ttl} vs store {store_ttl}"
    arrival = session_on_or_after(bd, EX)
    ttl_end = advance_trading_sessions(arrival, ttl, EX)
    # Decidedness needs the fill REPLAY to have covered the whole entry window,
    # not just the grouped store: for non-terminal rows the monitor's
    # last_resolved_session is the replay frontier and can lag the grouped
    # newest (verified 2026-07-14: 2 OPEN rows would otherwise land censored
    # observations in decided stop cells).
    lrs = r.get("last_resolved_session")
    replay_covers = (
        pd.isna(lrs) or (pd.Timestamp(lrs).date() >= ttl_end) if lrs is not None else True
    )
    ttl_closed = ttl_end <= newest and replay_covers
    rec = {
        "episode": f"{bd.isoformat()}|{tk}",
        "arrival": arrival,
        "depth": depth,
        "decided": bool(r["terminal"]) or ttl_closed,
        "close0": float(close0),
        "atr": float(atr),
        "limits": limits,
        "technical_ma50_distance_pct": None,
        "technical_atr_pct": None,
    }
    try:
        b = bix.loc[(bd, tk)]
        if isinstance(b, pd.DataFrame):
            b = b.iloc[0]
        rec["technical_ma50_distance_pct"] = b.get("technical_ma50_distance_pct")
        rec["technical_atr_pct"] = b.get("technical_atr_pct")
    except KeyError:
        pass
    episodes.append(rec)

ep = pd.DataFrame(episodes)
decided_mask = ep["decided"]
print(
    f"plannable episodes after dedup: {len(dd)} | usable (OK setup + depth): {len(ep)} "
    f"(skipped {n_no_setup}) | arrival-session clusters: {ep['arrival'].nunique()}"
)
print(
    f"decided: {int(decided_mask.sum())} | censored (entry window still open): "
    f"{int((~decided_mask).sum())}"
)
for label, sub in (("decided", ep[decided_mask]), ("censored", ep[~decided_mask])):
    dist = sub["depth"].value_counts().sort_index()
    print(f"  depth distribution [{label}]: " + "  ".join(f"{d}:{n}" for d, n in dist.items()))

# ---- stacked continuation-ratio rows ----
stacked = []
n_censored_rows = {0: 0, 1: 0, 2: 0}
for _, e in ep.iterrows():
    limits = e["limits"]
    for k in range(MAX_TIERS):
        if len(limits) < k + 1 or e["depth"] < k:
            continue  # ladder has no tier k+1 / episode never reached depth k
        if e["depth"] == k and not e["decided"]:
            n_censored_rows[k] += 1
            continue  # ongoing at depth k: transition-k outcome not yet decided
        level_k = e["close0"] if k == 0 else limits[k - 1]
        stacked.append(
            {
                "episode": e["episode"],
                "arrival": e["arrival"],
                "k": k,
                "y": int(e["depth"] >= k + 1),
                "spacing_to_next_atr": (level_k - limits[k]) / e["atr"],
                "technical_ma50_distance_pct": e["technical_ma50_distance_pct"],
                "technical_atr_pct": e["technical_atr_pct"],
            }
        )
st = pd.DataFrame(stacked)

print("\nper-transition rows (post censor-exclusion):")
for k in range(MAX_TIERS):
    sk = st[st["k"] == k]
    n_yes, n_no = int(sk["y"].sum()), int((1 - sk["y"]).sum())
    rate = n_yes / len(sk) if len(sk) else float("nan")
    print(
        f"  t{k}: n={len(sk):3d}  advance(y=1)={n_yes:3d}  stop(y=0)={n_no:3d}  "
        f"raw rate {rate:.3f}  (censor-excluded {n_censored_rows[k]})"
    )
counts = [int((st["k"] == k).sum()) for k in range(MAX_TIERS)]
assert counts[0] >= counts[1] >= counts[2] > 0, f"risk sets must shrink monotonically: {counts}"
for k in range(MAX_TIERS):
    sk = st[st["k"] == k]
    assert 0 < int(sk["y"].sum()) < len(sk), f"transition t{k} needs both outcomes"

model_df = st.dropna(subset=SHARED_COVARIATES).reset_index(drop=True)
print(
    f"\nmodeled rows: {len(model_df)} (dropped {len(st) - len(model_df)} with missing covariates)"
)
print(
    "  descriptive: corr(spacing_to_next_atr, technical_atr_pct) = "
    f"{model_df['spacing_to_next_atr'].corr(model_df['technical_atr_pct']):+.3f} "
    "(spacing is ATR-scaled by builder)"
)

mu = model_df[SHARED_COVARIATES].mean()
sd = model_df[SHARED_COVARIATES].std(ddof=0)
X = pd.DataFrame(
    {
        "const": 1.0,
        SHARED_COVARIATES[0]: (model_df[SHARED_COVARIATES[0]] - mu.iloc[0]) / sd.iloc[0],
        SHARED_COVARIATES[1]: (model_df[SHARED_COVARIATES[1]] - mu.iloc[1]) / sd.iloc[1],
        "k1": (model_df["k"] == 1).astype(float),
        "k2": (model_df["k"] == 2).astype(float),
    }
)[DESIGN_COLUMNS]
y = model_df["y"].to_numpy(dtype=float)
episode_codes = pd.factorize(model_df["episode"])[0]


def fit_glm(link, label: str):
    fit = sm.GLM(y, X, family=sm.families.Binomial(link=link)).fit(
        cov_type="cluster", cov_kwds={"groups": episode_codes}
    )
    print(f"\n{label} (episode-cluster robust; continuous covariates per +1sd stacked):")
    for name in DESIGN_COLUMNS:
        beta, se = float(fit.params[name]), float(fit.bse[name])
        z = beta / se
        p = float(fit.pvalues[name])
        print(f"  {name:28s} beta {beta:+.3f}  se {se:.3f}  z {z:+.2f}  p {p:.3f}")
    return fit


fit_logit = fit_glm(sm.families.links.Logit(), "PRIMARY: CR pooled logit")
fit_glm(sm.families.links.CLogLog(), "SECONDARY: CR pooled cloglog")

# ---- sparse-t2 honesty block (Firth-free mitigation = pooled shared coefficients) ----
t2 = model_df[model_df["k"] == 2]
t2_yes, t2_no = int(t2["y"].sum()), int((1 - t2["y"]).sum())
t2_med = t2["spacing_to_next_atr"].median()
below = t2[t2["spacing_to_next_atr"] <= t2_med]
above = t2[t2["spacing_to_next_atr"] > t2_med]
print(f"\nt2 raw contingency (n={len(t2)}): advance {t2_yes} / stop {t2_no}")
print(
    f"  spacing <= median ({t2_med:.2f} ATR): advance {int(below['y'].sum())}/{len(below)} | "
    f"spacing > median: advance {int(above['y'].sum())}/{len(above)}"
)
print(
    "  caveat: no Firth penalty available in deps; the pooled model shares covariate"
    " coefficients across transitions, which is the Firth-free mitigation for this sparse cell."
)
k2_beta, k2_se = float(fit_logit.params["k2"]), float(fit_logit.bse["k2"])
k2_flag = "UNSTABLE" if (k2_se > T2_SE_UNSTABLE or abs(k2_beta) > T2_COEF_UNSTABLE) else "STABLE"
print(f"  t2 dummy stability: coef {k2_beta:+.3f} se {k2_se:.3f} -> {k2_flag}")

# ---- day-cluster (arrival-session) bootstrap CI for the two shared covariates ----
sessions = sorted(model_df["arrival"].unique())
blocks = {
    s: (X[model_df["arrival"] == s].to_numpy(), y[(model_df["arrival"] == s).to_numpy()])
    for s in sessions
}
rng = np.random.default_rng(BOOT_SEED)
boot = []
for _ in range(B_BOOT):
    draw = rng.choice(len(sessions), size=len(sessions), replace=True)
    Xb = np.vstack([blocks[sessions[i]][0] for i in draw])
    yb = np.concatenate([blocks[sessions[i]][1] for i in draw])
    if yb.min() == yb.max():
        continue
    try:
        params = sm.GLM(yb, Xb, family=sm.families.Binomial()).fit().params
    except Exception:
        continue
    if np.all(np.isfinite(params)):
        boot.append(params[1:3])
boot_arr = np.asarray(boot)
print(
    f"\narrival-session bootstrap (B={B_BOOT}, ok fits {len(boot_arr)}, seed {BOOT_SEED}) "
    "95% CI, logit link:"
)
for j, name in enumerate(SHARED_COVARIATES):
    lo, hi = np.percentile(boot_arr[:, j], [2.5, 97.5])
    print(f"  {name:28s} [{lo:+.3f}, {hi:+.3f}]")
