"""Experts x EDGE calibration — the pre-registered LAST look on ledger cluster 15.

Fills the PENDING row "~2026-09 | Experts ticker-episode re-look | 15 |
held-out | car_10" of `docs/research/edge_hypothesis_budget_2026_07.md` §4.
Cluster 15 has spent 2 of its 3 lifetime looks (June sweep, July re-run —
verdict "SPURIOUS or NULL across the board" at a pseudo-replicated unit); this
is the third and FINAL look, at the honest ticker-episode unit, on held-out
data no prior analysis has touched. A null RETIRES the cluster (operational
stop rule — see VERDICT LANGUAGE below). Companion memo with the full
rationale, power table and limitations:
`docs/research/experts_last_look_2026_09.md`.

REGISTRATION (frozen 2026-09-01, BEFORE any feature-vs-outcome statistic was
computed on the held-out panel; the only held-out reads to date were
outcome-blind coverage/N counts, recorded in the memo). The run itself is
scheduled for the sunset window 2026-09-29/30 to maximize power within the
cluster's 2026-10 sunset (~50 arrival-session clusters vs ~30 today).
`--run` refuses to execute before RUN_NOT_BEFORE without an explicit
`--override-run-date` (which is a logged protocol deviation).

PANEL (frozen)
- Episodes: `population_ladders` plannable rows joined to `thematic_briefs`
  on (brief_date, ticker), brief_date >= 2026-07-06 (held-out; the discovery
  freeze is 2026-07-05), `panel_config_version == "panel-v1r-absdiff-2x"`
  only (ADR 0013 R3 — no pooling across config versions).
- Outcome: continuous car_10 (stock BHAR − SPY BHAR, beta=1), anchor
  previous_trading_day(arrival), horizon arrival+9 sessions; episodes whose
  horizon has not closed by the run date are excluded (calendar maturity).
- Split guard [0.55, 1.8] on day-over-day closes in the window (drop + count).
- Unit: `ticker_episode_dedup` (chained 5-session collapse). Clusters =
  ARRIVAL SESSIONS everywhere (OLS CR2, WCB, bootstrap, CV blocks).
- PIT guard: the qual enrich runs by convention the NEXT morning
  (`experts enrich <yesterday>`, 00:30-08:30 UTC — before the D+1 session
  opens; measured at registration: computed_at - brief_date == +1 day on
  478/478 held-out rows). A row whose `buffett_qual_computed_at` date is
  LATER than brief_date + 1 day (a backfill outside that convention, unknown
  provenance) gets its qual-derived features (candor_ord, understandable_f)
  nulled (counted + printed). The standard D+1-pre-open stamp is accepted and
  disclosed as a limitation: the car_10 window includes day D, so the
  scuttlebutt web channel could in principle embed day-0 news — the same
  day-0-overlap class already recorded for ALL brief features (which are
  computed from day-D closes).

FAMILY = 7 TESTS, BAR = 0.05/7 ≈ 0.00714 (Bonferroni; program charge = 1
cluster-15 look regardless of member count; the denominator never shrinks
post-hoc — an infeasible member counts as null, its slot is NOT redistributed).

PART A — six per-member tests (cluster_ols of car_10 on [const, member,
technical_atr_pct], arrival-session clusters, restricted wild cluster
bootstrap two-sided p, B=10,000, complete-case per member):
  1. buffett_quality_score
  2. buffett_roic_3y_avg  — admissible ONLY as an ATR-partialled residual
     claim (ledger cluster 4 "Quality/ROIC" stays RETIRED; a clear here never
     un-retires ROIC-as-level)
  3. expert_spread
  4. oneil_earnings_growth_yoy_pct — subject to the mfr veto below
  5. candor_ord = buffett_management_candor encoded promotional=0 / mixed=1 /
     candid=2, "unclear" -> NaN (missing, not a level). This single primary
     encoding ASSUMES equal spacing between adjacent levels; no alternative
     encoding will be tried after seeing results.
  6. understandable_f = buffett_understandable as 0/1.
Excluded AT REGISTRATION on outcome-blind coverage grounds (12% / 18%
held-out coverage — cannot meet the feasibility floor): margin_of_safety,
owner_earnings_yield. They are closed UNTESTED ("not estimable under the
pre-specified minimum-information rule"), never described as null.
Prior clean nulls are NOT re-tested individually (oneil_score,
oneil_rs_approx_pct, moat_type, moat_trend, data_coverage, llm_confidence —
`edge_signal_attribution_2026_07_06.md`).

FEASIBILITY FLOOR (per member, frozen): >= 50 post-dedup complete-case
episodes AND >= 15 arrival-session clusters; otherwise the member is
INFEASIBLE -> counts as null for the verdict, family bar unchanged.

MFR VETO (member 4 only): the primary test is ATR-partialled on the member's
own complete-case set. The member can clear ONLY if additionally (a) the
subset with `magic_formula_rank` present meets the feasibility floor and
(b) refitting with magic_formula_rank as a CONTINUOUS covariate on that
subset retains the coefficient sign and >= 50% of the primary magnitude
(collider control mandated by `oneil_expert_design_2026_06_13.md` — shared
NET_INCOME concept with an ACTIVE sort key). If (a) fails, the mandated
control is unsatisfiable and the member CANNOT clear at this look
(conservative null). There is no mfr-free rescue path.

PART B — one ML model, the 7th family member, charged to cluster 15 (it
tests the same hypothesis — "does panel content rank car_10 beyond ATR" —
and is a function of panel-derived features + the ATR control only):
- Features (7, all ~>=60% held-out coverage per the registration-day
  measurement — oneil_earnings_growth_yoy_pct measured 58% on the car_10
  panel vs 61% on the matured-ladder join and is RETAINED deliberately (it is
  also family member 4; imputation is inside the fold pipeline); no
  clean-null composites):
  technical_atr_pct (control), oneil_pct_off_52w_high,
  oneil_ma200_slope_pct_per_day, oneil_ma200_distance_pct,
  oneil_earnings_growth_yoy_pct, candor_ord, understandable_f.
- Elastic net l1_ratio=0.5; alpha grid {0.05, 0.15, 0.5} x sd(y), PRIMARY
  0.15 x sd(y); the other two are descriptive sensitivities, never a
  selection. Median-impute + standardize INSIDE the fold pipeline.
- PURGED GroupKFold (pre-hoc deviation from the July template, adopted from
  the 2026-09-01 external review): folds = contiguous 5-session arrival
  blocks (runt merged into predecessor), up to 5 splits; for each validation
  fold, every training episode whose [arrival, horizon] outcome window
  intersects the fold's session span is DROPPED from training (the
  overlapping-label leak asymmetrically favors the fitted model over the
  fit-free ATR baseline). Purge counts are printed per fold.
- Metric: pooled rank-within-fold OOF Spearman (per-fold spread printed) vs
  the fixed-direction -ATR baseline (train-median imputed on the SAME purged
  folds). Test: delta, arrival-session cluster bootstrap B=10,000, two-sided
  bootstrap p = 2*min(P(delta<=0), P(delta>=0)) vs the SAME 0.00714 bar.
- Degenerate rule: primary-alpha predictions constant in >= 2 folds -> the
  model test is NULL; no alpha rescue. NO gradient boosting / LightGBM
  (an unbudgeted extra look at n_eff ~ 120).

VERIFICATION BATTERY (mandatory gates for any clearing test; ALL must pass):
- exact reproduce (same seeds, second execution in-process);
- leave-one-block-out worst-case WCB p < 0.05 (B=1,999 per refit);
- leave-one-theme-out worst-case WCB p < 0.05;
- ATR-partialled Spearman (member and car_10 both residualized on ATR)
  cluster-bootstrap two-sided p < 0.05 with sign retained; ROIC-shaped
  members additionally report the roic_latest-partialled coefficient as a
  SIGN-CONSISTENCY note (never a clearing path);
- ticker-collapse (first episode per ticker) refit: sign retained AND >= 50%
  of the full-panel magnitude;
- car_5 / car_20 sign-consistency on >= 1 of 2 horizons (descriptive, printed
  only for clearing members — no horizon shopping).
Model analog: worst-case single-block-drop delta > 0 AND ticker-collapsed
delta > 0.

VERDICT (computed and printed by code, never by the analyst):
SURVIVES iff [some Part-A member has p_wcb < 0.00714 AND passes the full
battery AND its member-specific veto] OR [the model delta has bootstrap
p < 0.00714 AND passes the model battery]. Otherwise RETIRE.

VERDICT LANGUAGE (pre-committed three-way, equivalence bound DELTA = |rho|
0.10 frozen now as the smallest actionable effect):
  (i) cleared -> evidence of association under the registered estimand;
 (ii) not cleared but the 99.286% CI of the ATR-partialled Spearman still
      includes |rho| >= 0.10 -> "inconclusive; family retired OPERATIONALLY,
      not scientifically falsified";
(iii) that CI lies entirely within (-0.10, +0.10) -> evidence against
      actionable effects of this family, as instrumented.
Retirement is a resource-allocation stop rule. The memo must never claim
"experts are useless" from an underpowered null. Either way the experts stay
display-only after this look; a SURVIVES only freezes the clearing object for
a separate future promotion pre-registration (owner decision).

ABORT CLAUSE: the look may be aborted and re-registered uncharged ONLY for
defects visible in outcome-blind checks (row counts, coverage, config-version
filter, split-guard drops, join integrity, PIT violations). The moment any
feature-vs-outcome statistic is emitted, the look is spent.

REALIZED_R: whole-panel composition (ladder_classification counts, overall
realized_r summary) is printed as a descriptive table only, never cut by any
signal — signal-conditioned realized_r inference is a separate §4.1 charge.

KNOWN LIMITATIONS (stated up front; details in the memo): screened-population
estimand (candidate-pipeline stocks only); car_10 is an active return
(beta=1, split-adjusted closes, no dividends — consistent with every prior
sweep); the two LLM-derived features carry non-classical measurement error
under a single frozen prompt regime (`buffett-pre-registry-v0`), so a null on
candor/understandable is a null for THIS instrument, not the construct.

Last run: NOT YET RUN (registration only; scheduled 2026-09-29/30).
"""

import argparse
import datetime as dt
import math
import sys

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
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

EX = "XNYS"
HELDOUT_START = "2026-07-06"  # discovery freeze is 2026-07-05 (ledger rule 3)
PANEL_CONFIG = "panel-v1r-absdiff-2x"
RUN_NOT_BEFORE = dt.date(2026, 9, 29)  # sunset-window run date (registration)
FAMILY_SIZE = 7  # 6 members + 1 model — frozen; never shrinks post-hoc
FAMILY_BAR = 0.05 / FAMILY_SIZE
N_BOOT_PRIMARY = 10_000
N_BOOT_VERIFY = 1_999
EQUIV_RHO = 0.10  # smallest actionable |rho| (frozen)
CI_LEVEL = 1 - FAMILY_BAR  # 99.286% family-consistent CI
MIN_EPISODES = 50  # feasibility floor
MIN_CLUSTERS = 15
BLOCK_SESSIONS = 5
MAX_SPLITS = 5
CAR_HORIZON_SESSIONS = 9  # arrival + 9 = 10 sessions inclusive
SPLIT_RATIO_LO, SPLIT_RATIO_HI = 0.55, 1.8
ALPHA_GRID_SD_MULTIPLES = (0.05, 0.15, 0.5)
ALPHA_PRIMARY_SD_MULTIPLE = 0.15
SEED = 0

MEMBERS = (
    # (name, panel column, needs_mfr_veto)
    ("buffett_quality_score", "buffett_quality_score", False),
    ("buffett_roic_3y_avg (residual-vs-ATR claim)", "buffett_roic_3y_avg", False),
    ("expert_spread", "expert_spread", False),
    ("oneil_earnings_growth_yoy_pct", "oneil_earnings_growth_yoy_pct", True),
    ("management_candor (ordinal)", "candor_ord", False),
    ("understandable (0/1)", "understandable_f", False),
)
MODEL_FEATURES = (
    "technical_atr_pct",
    "oneil_pct_off_52w_high",
    "oneil_ma200_slope_pct_per_day",
    "oneil_ma200_distance_pct",
    "oneil_earnings_growth_yoy_pct",
    "candor_ord",
    "understandable_f",
)
BRIEF_COLS = [
    "buffett_quality_score",
    "buffett_roic_3y_avg",
    "buffett_roic_latest",
    "expert_spread",
    "panel_config_version",
    "buffett_management_candor",
    "buffett_understandable",
    "buffett_qual_computed_at",
    "oneil_earnings_growth_yoy_pct",
    "oneil_pct_off_52w_high",
    "oneil_ma200_slope_pct_per_day",
    "oneil_ma200_distance_pct",
    "magic_formula_rank",
    "technical_atr_pct",
]
_CANDOR_ORD = {"promotional": 0.0, "mixed": 1.0, "candid": 2.0}  # "unclear" -> NaN


# ---------------------------------------------------------------- panel build
def load_grouped():
    grouped = edge_stores.GroupedDailyCache(rs_history.DEFAULT_RS_HISTORY_ROOT)
    newest = edge_stores.newest_session(rs_history.DEFAULT_RS_HISTORY_ROOT)
    return grouped, newest


def close(grouped, session, ticker):
    snap = grouped.get(session)
    if not snap:
        return None
    bar = snap.get(ticker.upper())
    if not bar:
        return None
    try:
        c = float(bar["c"])
    except (KeyError, TypeError, ValueError):
        return None
    return c if c > 0 else None


def has_split_jump(grouped, ticker, anchor, horizon):
    prev_close = None
    s = anchor
    while s <= horizon:
        c = close(grouped, s, ticker)
        if c is not None:
            if prev_close is not None and not (SPLIT_RATIO_LO <= c / prev_close <= SPLIT_RATIO_HI):
                return True
            prev_close = c
        s = advance_trading_sessions(s, 1, EX)
    return False


def build_panel():
    """Held-out episode panel with car_5/10/20 and expert features.

    Returns (dedup_frame, diagnostics_dict). All feature encodings and guards
    from the registration docstring are applied here.
    """
    grouped, newest = load_grouped()
    outcomes = edge_stores.load_store(edge_stores.HOME / "population_ladders")
    briefs = edge_stores.load_store(edge_stores.HOME / "thematic_briefs")
    outcomes["ticker"] = outcomes["ticker"].astype(str).str.upper()
    briefs["ticker"] = briefs["ticker"].astype(str).str.upper()
    shared = set(outcomes.columns) & set(briefs.columns)
    # Join-integrity tripwire (pattern of 2026_07_tail_filter_features_gkfold):
    # a NEW shared non-key column would be silently suffixed by the merge.
    assert shared <= {"brief_date", "ticker", "theme", "scorer_config_version"}, (
        f"unexpected shared columns across stores: {sorted(shared)}"
    )
    bix = briefs.set_index(["brief_date", "ticker"])[[c for c in BRIEF_COLS if c in briefs.columns]]

    plannable = outcomes[outcomes["plannable"] == True]  # noqa: E712
    plannable = plannable[plannable["brief_date"].astype(str) >= HELDOUT_START]
    rows = []
    diag = {"split_dropped": 0, "car_missing": 0, "immature": 0, "pit_nulled": 0}
    for _, r in plannable.drop_duplicates(subset=["brief_date", "ticker"]).iterrows():
        # brief_date stays the NATIVE datetime.date — both stores stamp dates,
        # and a str key here silently empties the bix join (KeyError -> None).
        bd, tk = r["brief_date"], str(r["ticker"]).upper()
        arr = session_on_or_after(bd, EX)
        hor = advance_trading_sessions(arr, CAR_HORIZON_SESSIONS, EX)
        if hor > newest:
            diag["immature"] += 1
            continue
        anc = previous_trading_day(arr, EX)
        cars = {}
        for label, k in (("car_5", 4), ("car_10", CAR_HORIZON_SESSIONS), ("car_20", 19)):
            h = advance_trading_sessions(arr, k, EX)
            if h > newest:
                cars[label] = None
                continue
            cars[label] = fixed_horizon.car_for_event(
                stock_anchor=close(grouped, anc, tk),
                stock_horizon=close(grouped, h, tk),
                spy_anchor=close(grouped, anc, "SPY"),
                spy_horizon=close(grouped, h, "SPY"),
            )
        if cars["car_10"] is None:
            diag["car_missing"] += 1
            continue
        if has_split_jump(grouped, tk, anc, hor):
            diag["split_dropped"] += 1
            continue
        rec = {
            "brief_date": bd,
            "ticker": tk,
            "arrival": arr,
            "horizon": hor,
            "theme": r.get("theme"),
            "ladder_classification": r.get("ladder_classification"),
            "realized_r": r.get("realized_r"),
            "market_excess_return": r.get("market_excess_return"),
            **cars,
        }
        try:
            b = bix.loc[(bd, tk)]
            if isinstance(b, pd.DataFrame):
                b = b.iloc[0]
        except KeyError:
            b = None
        for c in BRIEF_COLS:
            rec[c] = None if b is None else b.get(c)
        rows.append(rec)

    panel = pd.DataFrame(rows)
    panel = panel[panel["panel_config_version"] == PANEL_CONFIG].reset_index(drop=True)

    # Feature encodings (frozen).
    candor = panel["buffett_management_candor"].astype(object)
    panel["candor_ord"] = [
        _CANDOR_ORD.get(str(v).strip().lower()) if pd.notna(v) else None for v in candor
    ]
    und = panel["buffett_understandable"]
    panel["understandable_f"] = [float(bool(v)) if pd.notna(v) else None for v in und]
    # PIT guard: qual stamped after arrival -> null the qual-derived features.
    pit_bad = []
    for i, r in panel.iterrows():
        ts = r.get("buffett_qual_computed_at")
        if pd.isna(ts) or ts is None:
            continue
        try:
            d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
        except ValueError:
            continue
        # Standard timing is next-morning pre-open (computed = brief_date + 1,
        # 478/478 at registration); anything LATER is an out-of-convention
        # backfill of unknown provenance -> null the qual features.
        if d > r["brief_date"] + dt.timedelta(days=1):
            pit_bad.append(i)
    if pit_bad:
        panel.loc[pit_bad, ["candor_ord", "understandable_f"]] = None
        diag["pit_nulled"] = len(pit_bad)

    dd = ticker_episode_dedup(panel).reset_index(drop=True)
    return dd, diag


def make_blocks(dd):
    sessions = sorted(dd["arrival"].unique())
    n_blocks = math.ceil(len(sessions) / BLOCK_SESSIONS)
    if len(sessions) % BLOCK_SESSIONS and n_blocks > 1:
        n_blocks -= 1
    block_of = {s: min(i // BLOCK_SESSIONS, n_blocks - 1) for i, s in enumerate(sessions)}
    return dd["arrival"].map(block_of).to_numpy(), n_blocks


def print_sample_diagnostics(dd, diag):
    clusters = dd["arrival"].astype(str)
    sizes = clusters.value_counts()
    print(
        f"panel: {len(dd)} episodes | {clusters.nunique()} arrival-session clusters "
        f"({dd['brief_date'].nunique()} brief-dates) | "
        f"{dd['ticker'].nunique()} tickers | "
        f"{dd['brief_date'].min()} -> {dd['brief_date'].max()}"
    )
    print(
        f"guards: split-dropped {diag['split_dropped']} | car_10-missing {diag['car_missing']} | "
        f"immature {diag['immature']} | qual PIT-nulled rows {diag['pit_nulled']}"
    )
    print(
        f"cluster sizes: mean {sizes.mean():.1f} | max {sizes.max()} "
        f"({sizes.max() / len(dd):.0%} of episodes) | cv {sizes.std(ddof=0) / sizes.mean():.2f}"
    )
    print("coverage (post-dedup complete-case episodes / clusters):")
    for _, col, _ in MEMBERS:
        sub = dd[dd[col].notna() & dd["technical_atr_pct"].notna()]
        print(f"  {col:34s} {len(sub):4d} ep / {sub['arrival'].nunique():3d} clusters")
    for col in MODEL_FEATURES:
        print(f"  [model] {col:26s} {dd[col].notna().mean():5.0%}")
    mfr = dd[
        dd["oneil_earnings_growth_yoy_pct"].notna()
        & dd["magic_formula_rank"].notna()
        & dd["technical_atr_pct"].notna()
    ]
    print(
        f"  mfr-veto subset (earnings & mfr & atr)  {len(mfr):4d} ep / {mfr['arrival'].nunique():3d} clusters"
    )


# ------------------------------------------------------------------ preflight
def estimate_icc(y, clusters):
    """One-way ANOVA ICC estimate (clamped to [0, 0.9])."""
    frame = pd.DataFrame({"y": y, "c": clusters})
    groups = [g["y"].to_numpy() for _, g in frame.groupby("c") if len(g) > 0]
    k = len(groups)
    n = sum(len(g) for g in groups)
    if k < 2 or n <= k:
        return 0.0
    grand = np.concatenate(groups).mean()
    ssb = sum(len(g) * (g.mean() - grand) ** 2 for g in groups)
    ssw = sum(((g - g.mean()) ** 2).sum() for g in groups)
    msb, msw = ssb / (k - 1), ssw / (n - k)
    n0 = (n - sum(len(g) ** 2 for g in groups) / n) / (k - 1)
    icc = (msb - msw) / (msb + (n0 - 1) * msw) if msw > 0 else 0.0
    return float(min(max(icc, 0.0), 0.9))


def preflight_power_sim(dd, n_sims=300, wcb_boot=499):
    """Simulated power at the family bar, using the held-out CLUSTER STRUCTURE
    and outcome scale/ICC measured on the BURNT discovery panel (<=2026-07-05
    — already-seen data, so this stays outcome-blind for the held-out look)."""
    outcomes = edge_stores.load_store(edge_stores.HOME / "population_ladders")
    disc = outcomes[
        (outcomes["plannable"] == True)  # noqa: E712
        & (outcomes["brief_date"].astype(str) <= "2026-07-05")
        & outcomes["market_excess_return"].notna()
    ]
    y_d = disc["market_excess_return"].astype(float).to_numpy()
    cl_d = disc["brief_date"].astype(str).to_numpy()
    sd_y = float(np.std(y_d))
    icc = estimate_icc(y_d, cl_d)
    sizes = dd["arrival"].astype(str).value_counts().to_numpy()
    print(
        f"power sim inputs: discovery sd(y)={sd_y:.4f} icc={icc:.2f} | "
        f"held-out clusters={len(sizes)} episodes={sizes.sum()}"
    )
    rng = np.random.default_rng(SEED)
    var_u = icc * sd_y**2
    var_e = (1 - icc) * sd_y**2
    for rho in (0.1, 0.2, 0.3, 0.4):
        clears = 0
        for _ in range(n_sims):
            xs, ys, cls = [], [], []
            for j, m in enumerate(sizes):
                u = rng.normal(0, math.sqrt(var_u))
                x = rng.normal(0, 1, m)
                e = rng.normal(0, math.sqrt(var_e), m)
                # slope chosen so corr(x, y) = rho marginally
                b = rho * sd_y
                resid_scale = math.sqrt(max(1 - rho**2, 1e-9))
                ys.append(b * x + resid_scale * (u + e))
                xs.append(x)
                cls.append(np.full(m, j))
            y = np.concatenate(ys)
            X = np.column_stack([np.ones(len(y)), np.concatenate(xs)])
            cl = np.concatenate(cls).astype(str)
            p = wild_cluster_bootstrap_p(
                y, X, cl, 1, n_boot=wcb_boot, seed=int(rng.integers(1 << 30))
            )
            clears += p < FAMILY_BAR
        print(f"  rho={rho:.1f}: P(clear family bar) ~ {clears / n_sims:.0%}  ({n_sims} sims)")


def preflight():
    dd, diag = build_panel()
    print_sample_diagnostics(dd, diag)
    versions = dd["panel_config_version"].unique().tolist()
    assert versions == [PANEL_CONFIG], versions
    preflight_power_sim(dd)
    print("\npreflight complete — NO feature-vs-outcome statistic was computed.")


# ------------------------------------------------------------------ inference
def member_fit(dd, col, extra_controls=()):
    """cluster_ols + WCB p for car_10 ~ member + ATR (+ extras), complete-case."""
    cols = [col, "technical_atr_pct", *extra_controls]
    sub = dd[dd[cols].notna().all(axis=1) & dd["car_10"].notna()]
    y = sub["car_10"].astype(float).to_numpy()
    X = np.column_stack([np.ones(len(sub))] + [sub[c].astype(float).to_numpy() for c in cols])
    cl = sub["arrival"].astype(str).to_numpy()
    return sub, y, X, cl


def wcb(y, X, cl, j, n_boot, seed=SEED):
    return wild_cluster_bootstrap_p(y, X, cl, j, n_boot=n_boot, seed=seed)


def partial_spearman_ci(sub, col, n_boot=N_BOOT_PRIMARY, seed=SEED):
    """ATR-partialled Spearman of member vs car_10 + cluster-bootstrap CI at
    CI_LEVEL and a two-sided bootstrap-percentile p (H0: rho_partial = 0)."""
    y = sub["car_10"].astype(float).to_numpy()
    x = sub[col].astype(float).to_numpy()
    a = sub["technical_atr_pct"].astype(float).to_numpy()
    A = np.column_stack([np.ones(len(sub)), a])

    def resid(v):
        beta, *_ = np.linalg.lstsq(A, v, rcond=None)
        return v - A @ beta

    rx, ry = resid(x), resid(y)
    rho = float(scipy_stats.spearmanr(rx, ry)[0])
    cl = sub["arrival"].astype(str).to_numpy()
    by_cluster = {c: np.where(cl == c)[0] for c in sorted(set(cl))}
    keys = list(by_cluster)
    rng = np.random.default_rng(seed)
    boots = []
    for _ in range(n_boot):
        take = rng.choice(len(keys), size=len(keys), replace=True)
        ix = np.concatenate([by_cluster[keys[t]] for t in take])
        if np.ptp(rx[ix]) == 0 or np.ptp(ry[ix]) == 0:
            continue
        boots.append(float(scipy_stats.spearmanr(rx[ix], ry[ix])[0]))
    boots_arr = np.array(boots)
    lo, hi = np.percentile(boots_arr, [100 * (1 - CI_LEVEL) / 2, 100 * (1 + CI_LEVEL) / 2])
    frac_le = float((boots_arr <= 0).mean())
    p_boot = 2 * min(frac_le, 1 - frac_le)
    return rho, (float(lo), float(hi)), p_boot


def verify_member(dd, name, col, beta_full, extra_controls=()):
    """Run the frozen verification battery for a clearing member. Returns
    (passed: bool, notes: list[str])."""
    notes = []
    ok = True
    sub, y, X, cl = member_fit(dd, col, extra_controls)
    # exact reproduce
    p_again = wcb(y, X, cl, 1, N_BOOT_PRIMARY)
    notes.append(f"reproduce: p={p_again:.5f}")
    ok &= p_again < FAMILY_BAR
    # leave-one-block-out
    blocks, _ = make_blocks(sub.reset_index(drop=True))
    worst_lobo = 0.0
    for b in sorted(set(blocks)):
        keep = blocks != b
        p = wcb(y[keep], X[keep], cl[keep], 1, N_BOOT_VERIFY)
        worst_lobo = max(worst_lobo, p)
    notes.append(f"LOBO worst p={worst_lobo:.3f}")
    ok &= worst_lobo < 0.05
    # leave-one-theme-out
    themes = sub["theme"].astype(str).to_numpy()
    worst_loto = 0.0
    for t in sorted(set(themes)):
        keep = themes != t
        if keep.sum() < MIN_EPISODES:
            continue
        p = wcb(y[keep], X[keep], cl[keep], 1, N_BOOT_VERIFY)
        worst_loto = max(worst_loto, p)
    notes.append(f"LOTO worst p={worst_loto:.3f}")
    ok &= worst_loto < 0.05
    # ATR-partialled Spearman
    rho, ci, p_ps = partial_spearman_ci(sub, col)
    notes.append(
        f"partial Spearman rho={rho:+.3f} CI{CI_LEVEL:.5f}=[{ci[0]:+.3f},{ci[1]:+.3f}] p={p_ps:.4f}"
    )
    ok &= (p_ps < 0.05) and (np.sign(rho) == np.sign(beta_full))
    if "roic" in col:
        sub_r = sub[sub["buffett_roic_latest"].notna()] if "buffett_roic_latest" in sub else sub
        notes.append(f"roic_latest sign-consistency subset n={len(sub_r)} (reported, non-gating)")
    # ticker collapse (first episode per ticker)
    tc = sub.sort_values(["ticker", "brief_date"]).drop_duplicates("ticker")
    _, y_t, X_t, cl_t = member_fit(tc, col, extra_controls)
    res_t = cluster_ols(y_t, X_t, cl_t)
    beta_t = res_t.beta[1]
    notes.append(f"ticker-collapse beta={beta_t:+.4f} (full {beta_full:+.4f})")
    ok &= (np.sign(beta_t) == np.sign(beta_full)) and (abs(beta_t) >= 0.5 * abs(beta_full))
    # car_5 / car_20 sign consistency (descriptive)
    signs = []
    for h in ("car_5", "car_20"):
        s2 = sub[sub[h].notna()]
        if len(s2) < MIN_EPISODES:
            continue
        y2 = s2[h].astype(float).to_numpy()
        X2 = np.column_stack(
            [
                np.ones(len(s2)),
                s2[col].astype(float).to_numpy(),
                s2["technical_atr_pct"].astype(float).to_numpy(),
            ]
        )
        r2 = cluster_ols(y2, X2, s2["arrival"].astype(str).to_numpy())
        signs.append(np.sign(r2.beta[1]) == np.sign(beta_full))
        notes.append(f"{h} beta={r2.beta[1]:+.4f}")
    ok &= (not signs) or any(signs)
    return ok, notes


# ---------------------------------------------------------------------- model
def purged_folds(dd, blocks):
    """(train_idx, val_idx) per fold with outcome-window purge applied."""
    arr = dd["arrival"].to_numpy()
    hor = dd["horizon"].to_numpy()
    folds = []
    block_ids = sorted(set(blocks))
    n_splits = min(MAX_SPLITS, len(block_ids))
    # contiguous grouping of blocks into folds
    per = math.ceil(len(block_ids) / n_splits)
    for f in range(n_splits):
        val_blocks = set(block_ids[f * per : (f + 1) * per])
        if not val_blocks:
            continue
        va = np.where(np.isin(blocks, list(val_blocks)))[0]
        span_lo, span_hi = arr[va].min(), hor[va].max()
        tr_mask = ~np.isin(blocks, list(val_blocks))
        overlap = (arr <= span_hi) & (hor >= span_lo)
        purged = tr_mask & overlap
        tr = np.where(tr_mask & ~overlap)[0]
        folds.append((tr, va, int(purged.sum())))
    return folds


def enet(alpha):
    return Pipeline(
        [
            ("imp", SimpleImputer(strategy="median")),
            ("sc", StandardScaler()),
            ("en", ElasticNet(alpha=alpha, l1_ratio=0.5, random_state=SEED, max_iter=10_000)),
        ]
    )


def rank_within_fold(values, folds):
    ranked = np.full(len(values), np.nan)
    for _, va, _ in folds:
        ranked[va] = scipy_stats.rankdata(values[va]) / (len(va) + 1)
    return ranked


def pooled_spearman(a_rank, b_rank):
    mask = ~(np.isnan(a_rank) | np.isnan(b_rank))
    if mask.sum() < 3 or np.ptp(a_rank[mask]) == 0 or np.ptp(b_rank[mask]) == 0:
        return float("nan")
    return float(scipy_stats.spearmanr(a_rank[mask], b_rank[mask])[0])


def run_model(dd, blocks):
    y = dd["car_10"].astype(float).to_numpy()
    X = dd[list(MODEL_FEATURES)].astype(float).to_numpy()
    folds = purged_folds(dd, blocks)
    for i, (tr, va, purged) in enumerate(folds):
        print(f"  fold {i}: train {len(tr)} (purged {purged}) / val {len(va)}")
    sd_y = float(np.std(y))
    alphas = [m * sd_y for m in ALPHA_GRID_SD_MULTIPLES]
    primary_alpha = ALPHA_PRIMARY_SD_MULTIPLE * sd_y

    def oof(alpha):
        pred = np.full(len(y), np.nan)
        for tr, va, _ in folds:
            m = enet(alpha)
            m.fit(X[tr], y[tr])
            pred[va] = m.predict(X[va])
        return pred

    y_rank = rank_within_fold(y, folds)
    atr = dd[["technical_atr_pct"]].astype(float).to_numpy()
    atr_score = np.full(len(y), np.nan)
    for tr, va, _ in folds:
        med = np.nanmedian(atr[tr])
        atr_score[va] = -np.where(np.isnan(atr[va][:, 0]), med, atr[va][:, 0])
    atr_rank = rank_within_fold(atr_score, folds)
    rho_atr = pooled_spearman(atr_rank, y_rank)

    oof_primary = None
    degenerate_folds = 0
    for alpha in alphas:
        pred = oof(alpha)
        deg = sum(1 for _, va, _ in folds if np.ptp(pred[va]) == 0)
        per_fold = [
            float("nan")
            if np.ptp(pred[va]) == 0
            else float(scipy_stats.spearmanr(pred[va], y[va])[0])
            for _, va, _ in folds
        ]
        pooled = pooled_spearman(rank_within_fold(pred, folds), y_rank)
        tag = ""
        if math.isclose(alpha, primary_alpha):
            oof_primary, degenerate_folds = pred, deg
            tag = " <- PRIMARY (pre-committed 0.15 x sd(y))"
        fold_txt = " ".join("degen" if math.isnan(r) else f"{r:+.2f}" for r in per_fold)
        print(f"  alpha={alpha:.4f} pooled={pooled:+.3f} folds: {fold_txt}{tag}")
    print(f"  baseline -ATR pooled rank-within-fold Spearman = {rho_atr:+.3f}")

    if degenerate_folds >= 2:
        print("  DEGENERATE RULE: primary predictions constant in >= 2 folds -> model test NULL")
        return {"clears": False, "reason": "degenerate", "delta": float("nan"), "p": float("nan")}

    pred_rank = rank_within_fold(oof_primary, folds)
    rho_model = pooled_spearman(pred_rank, y_rank)
    delta = rho_model - rho_atr
    cl = dd["arrival"].astype(str).to_numpy()
    by_cluster = {c: np.where(cl == c)[0] for c in sorted(set(cl))}
    keys = list(by_cluster)
    rng = np.random.default_rng(SEED)
    diffs = []
    skipped = 0
    for _ in range(N_BOOT_PRIMARY):
        take = rng.choice(len(keys), size=len(keys), replace=True)
        ix = np.concatenate([by_cluster[keys[t]] for t in take])
        pr, ar, yr = pred_rank[ix], atr_rank[ix], y_rank[ix]
        mask = ~(np.isnan(pr) | np.isnan(ar) | np.isnan(yr))
        if (
            mask.sum() < 3
            or np.ptp(pr[mask]) == 0
            or np.ptp(ar[mask]) == 0
            or np.ptp(yr[mask]) == 0
        ):
            skipped += 1
            continue
        rm = float(scipy_stats.spearmanr(pr[mask], yr[mask])[0])
        ra = float(scipy_stats.spearmanr(ar[mask], yr[mask])[0])
        diffs.append(rm - ra)
    diffs_arr = np.array(diffs)
    frac_le = float((diffs_arr <= 0).mean())
    p_boot = 2 * min(frac_le, 1 - frac_le)
    lo, hi = np.percentile(diffs_arr, [100 * (1 - CI_LEVEL) / 2, 100 * (1 + CI_LEVEL) / 2])
    print(
        f"  model vs -ATR: delta={delta:+.3f} bootstrap p={p_boot:.4f} "
        f"CI{CI_LEVEL:.5f}=[{lo:+.3f},{hi:+.3f}] skipped {skipped}/{N_BOOT_PRIMARY}"
    )
    clears = p_boot < FAMILY_BAR and delta > 0
    result = {"clears": clears, "delta": delta, "p": p_boot, "ci": (lo, hi)}
    if clears:
        # model verification: single-block-drop worst delta; ticker collapse
        worst = math.inf
        for b in sorted(set(blocks)):
            keep = blocks != b
            d2 = pooled_spearman(pred_rank[keep], y_rank[keep]) - pooled_spearman(
                atr_rank[keep], y_rank[keep]
            )
            worst = min(worst, d2)
        tc_mask = (
            ~dd.sort_values(["ticker", "brief_date"]).duplicated("ticker").sort_index().to_numpy()
        )
        d_tc = pooled_spearman(pred_rank[tc_mask], y_rank[tc_mask]) - pooled_spearman(
            atr_rank[tc_mask], y_rank[tc_mask]
        )
        print(
            f"  model verification: worst block-drop delta={worst:+.3f}, ticker-collapse delta={d_tc:+.3f}"
        )
        result["verified"] = worst > 0 and d_tc > 0
    else:
        result["verified"] = False
    return result


# ----------------------------------------------------------------------- run
def full_run():
    dd, diag = build_panel()
    print_sample_diagnostics(dd, diag)
    versions = dd["panel_config_version"].unique().tolist()
    assert versions == [PANEL_CONFIG], versions
    blocks, _ = make_blocks(dd)

    print(f"\nPART A — per-member tests (family bar {FAMILY_BAR:.5f}):")
    survivors = []
    for name, col, needs_mfr in MEMBERS:
        sub, y, X, cl = member_fit(dd, col)
        n, g = len(sub), sub["arrival"].nunique()
        if n < MIN_EPISODES or g < MIN_CLUSTERS:
            print(f"  {name:44s} INFEASIBLE (n={n}, clusters={g}) -> null")
            continue
        res = cluster_ols(y, X, cl)
        p = wcb(y, X, cl, 1, N_BOOT_PRIMARY)
        rho, ci, _ = partial_spearman_ci(sub, col)
        inconclusive = ci[0] <= -EQUIV_RHO or ci[1] >= EQUIV_RHO
        print(
            f"  {name:44s} n={n:3d} G={g:2d} beta={res.beta[1]:+.4f} t_cr2={res.t_cr2[1]:+.2f} "
            f"p_wcb={p:.4f} | partial-Spearman rho={rho:+.3f} CI=[{ci[0]:+.3f},{ci[1]:+.3f}]"
        )
        cleared = p < FAMILY_BAR
        if cleared and needs_mfr:
            mfr_sub, y_m, X_m, cl_m = member_fit(dd, col, extra_controls=("magic_formula_rank",))
            if len(mfr_sub) < MIN_EPISODES or mfr_sub["arrival"].nunique() < MIN_CLUSTERS:
                print(
                    "    mfr veto: control subset infeasible -> member CANNOT clear (conservative null)"
                )
                cleared = False
            else:
                res_m = cluster_ols(y_m, X_m, cl_m)
                keep = np.sign(res_m.beta[1]) == np.sign(res.beta[1]) and abs(
                    res_m.beta[1]
                ) >= 0.5 * abs(res.beta[1])
                print(
                    f"    mfr veto: beta under mfr partial {res_m.beta[1]:+.4f} -> {'retained' if keep else 'VETOED'}"
                )
                cleared &= keep
        if cleared:
            ok, notes = verify_member(dd, name, col, res.beta[1])
            for note in notes:
                print(f"    verify: {note}")
            print(f"    verification {'PASS' if ok else 'FAIL'}")
            if ok:
                survivors.append((name, "member"))
        else:
            tag = "inconclusive-range CI" if inconclusive else "CI inside equivalence bound"
            print(f"    not cleared ({tag})")

    print("\nPART B — elastic net vs -ATR (purged block folds):")
    model_res = run_model(dd, blocks)
    if model_res["clears"] and model_res["verified"]:
        survivors.append(("elastic-net panel model", "model"))

    print("\nDESCRIPTIVE (whole panel, not cut by any signal):")
    print(f"  ladder_classification: {dd['ladder_classification'].value_counts().to_dict()}")
    rr = dd["realized_r"].dropna().astype(float)
    print(f"  realized_r (all): n={len(rr)} mean={rr.mean():+.3f} median={rr.median():+.3f}")
    me = dd["market_excess_return"].dropna().astype(float)
    sign_mismatch = (
        (np.sign(dd["car_10"].astype(float)) != np.sign(dd["market_excess_return"].astype(float)))
        & dd["market_excess_return"].notna()
    ).sum()
    print(
        f"  market_excess_return present {len(me)}; sign differs from car_10 on {sign_mismatch} rows"
    )

    print("\n" + "=" * 72)
    if survivors:
        print("VERDICT: SURVIVES —", "; ".join(f"{n} ({k})" for n, k in survivors))
        print("Promotion object frozen at registration; promotion requires a separate")
        print("future pre-registration (owner decision). Experts remain display-only.")
    else:
        print("VERDICT: RETIRE — cluster 15 closed (operational stop rule).")
        print("See the pre-committed three-way conclusion language in the docstring;")
        print("per-member CIs above decide 'inconclusive' vs 'evidence against'.")
    print("=" * 72)


def main():
    ap = argparse.ArgumentParser(
        description="Experts x EDGE calibration — the pre-registered LAST look on cluster 15"
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true", help="outcome-blind checks + power sim")
    mode.add_argument(
        "--run", action="store_true", help="THE look — burns cluster 15's last re-look"
    )
    ap.add_argument(
        "--override-run-date",
        action="store_true",
        help="run before RUN_NOT_BEFORE (logged protocol deviation)",
    )
    args = ap.parse_args()
    if args.preflight:
        preflight()
        return
    today = dt.date.today()
    if today < RUN_NOT_BEFORE and not args.override_run_date:
        sys.exit(
            f"refusing to run before {RUN_NOT_BEFORE} (registration schedules the look "
            f"in the sunset window; --override-run-date is a logged protocol deviation)"
        )
    full_run()


if __name__ == "__main__":
    main()
