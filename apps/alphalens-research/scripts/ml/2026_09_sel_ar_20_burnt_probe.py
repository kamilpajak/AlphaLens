"""News-axis probe against `sel_ar_20` on the BURNT discovery panel.

EXPLORATORY, ZERO CHARGE. This is not a registration and nothing here may be
cited as evidence for any decision that changes production. Under ADR 0013 R4
(amended 2026-09-25) an exploratory look costs nothing only when all three
conditions hold at once, and they are stated here so a later reader can check
them:

  1. CUTOFF — it reads outcomes matured to `brief_date <= 2026-07-05` only.
     That is the frozen discovery window; ledger rule 3 already spent it in the
     June and July sweeps, and `a20_power_preflight_2026_09.md` §2.3 records
     that burnt outcome values are fair game.
  2. LEDGER — a row is appended to §4.1 of `edge_hypothesis_budget_2026_07.md`
     with `charges: 0 (exploration)` BEFORE the run, per README house rule 11.
  3. NOT CITED — any later CLAIM on these features pays, and a candidate
     without a cluster slot opens a new ledger row, raising every other
     hypothesis's denominator.

It does NOT touch the held-out panel, so it leaves #1227 untouched.

THE QUESTION
Every model this project has fitted lost to plain ATR, and every one of them
measured the PRICE. The candidates below measure the NEWS instead. Rank
correlation with ATR on this panel, measured outcome-blind before the list was
frozen: catalyst_age_h 0.00, catalyst_strength 0.06, ma200_slope 0.09.

So the question is NOT "does a model beat ATR" — see WHY NOT A PREDICTION TEST.
It is: with ATR and the MA50 extension already in the model, does any news-axis
term carry an independent coefficient?

PANEL (frozen before the run)
- `selection_labels` rows with `sel_label_status_20 == "ok"` and
  `brief_date <= 2026-07-05`, inner-joined to `thematic_briefs` on
  (brief_date, ticker), then collapsed by `ticker_episode_dedup`.
- Measured 2026-09-29: 391 pre-dedup rows -> **198 ticker-episodes in 26
  arrival-session clusters** (the 27 an earlier draft carried came from the
  recomputed anchor the note below retracts). The episode count is the unit (README rule 1,
  ledger rule 5); quoting the 391 would repeat the error that voided the #1227
  power gate on 2026-09-24.
- `briefed_any_theme` is true on every row here, so the briefed and all-rows
  populations are identical on the burnt side and the population choice is not
  a live decision.
- The insider-cluster event lane cannot appear: it went live 2026-09-06, two
  months after this window closes (verified: 0 rows at or before 2026-07-05).

OUTCOME AND ANCHOR
`sel_ar_20` AND the `anchor_session` it is measured from are both READ from
the label store, never recomputed — recomputing either is a new definition
(`ml_label_registry_design_2026_09_16.md` §8). The anchor is the first session
AFTER the brief date (owner decision D4), which `session_on_or_after` does NOT
return when the brief date is itself a session: the two disagree on 296 of 466
burnt rows. The first run of this probe recomputed it and measured 65 articles
as published after their own anchor open, which was an artefact of that error
and not a property of the data. A source gate in the test file now forbids the
call outright.

Known limitation, recorded not fixed: `ticker_episode_dedup` derives its own
chaining arrival internally via `session_on_or_after`. That only sets the
5-session window for collapsing repeats of one ticker, a relative comparison a
uniform one-session shift barely moves, and the shared helper is out of scope
for an exploratory probe.

REGRESSORS (frozen; standardised, so coefficients read against the published
burnt A20 table in `a20_power_preflight_2026_09.md` §4.1: ATR -0.378,
MA50 -0.303, press gate +0.026)

  controls   technical_atr_pct
             technical_ma50_distance_pct
  candidates catalyst_age_h
             catalyst_strength
             technical_ma200_slope_pct_per_day

MA50 is a CONTROL, not a candidate, and that is what makes the test strict: a
candidate's coefficient is its increment over the structure already known to be
real, so clearing it is the "beat baseline B" question in a single fit.

DROPPED BEFORE THE RUN, each for a measured reason:
  cohort_size_in_day  - it IS `len(deduped)` (orchestrator.py:536), identical
                        for every row of a brief date (ICC 1.000), effective N
                        34 not 391; and it is already a RECORDED CLEAN NULL in
                        `edge_signal_attribution_2026_07_06.md` §4, which says
                        not to re-test it.
  sigma_resid_pre     - |rho| 0.91 with ATR. ATR in a new dress; the exact
                        failure mode of the July EWMA test.
  beta_ols            - |rho| 0.60 with ATR, and it constructs `sel_ar_20`.
  rank_in_day         - carries an ATR penalty by construction
                        (`scorer-v1-atrtilt`).
  technical_rsi,
  market_cap          - L1 zeroed both in July on car_10.

catalyst_age_h — DEFINITION (the brief column cannot supply it)
`thematic_briefs.source_event_published_at` is a bare DATE: the resolver throws
the timestamp away (`catalyst_resolver.py:581`, `.date().isoformat()`), so an
"hours" figure built from it is 13.5 + 24k and takes 14 distinct values on this
panel. The feature is therefore defined against the NEWS store:

    hours from `thematic_news.timestamp` (tz-aware UTC) of the article at
    `source_event_url`, to `session_open_utc(arrival_session, "XNYS")`.

Measured recovery: 387 of 391 pre-dedup rows (99.0%), 161 distinct values
against 14. Two urls carry two recorded timestamps ~46 h apart (6 rows), so the
tie-break is frozen in `pick_news_timestamp`: prefer a candidate whose UTC date
equals the date the brief stamped, latest among those, else the latest overall.
Rows that resolve to nothing are complete-case dropped and COUNTED, never
imputed.

PIT
The check the spec first asked for has no power and is not used: 171 of 391 rows
(43.7%) sit on dates whose brief parquet was rewritten 3.7-7.7 h AFTER the
arrival open, so `brief_generated_at` is a rewrite stamp, not a publication
time. The check with power is `news timestamp < session_open_utc(arrival)`,
reported below; measured 0 violations on 387 rows before the run. Disclosed: 31
of 387 articles were published on the UTC day after their brief date, all
between 00:00 and 06:30 UTC and all before the arrival open — overnight ingest,
not look-ahead.

PRIMARY INSTRUMENT — jointly fitted coefficients, cluster-robust
`cluster_ols` with arrival sessions as clusters, plus a restricted wild cluster
bootstrap p per candidate (B=10,000). This is the same instrument that produced
the published burnt A20 table, so the new rows are directly comparable.

WHY NOT A PREDICTION TEST
`sel_ar_20` spans 20 trading sessions; this panel's arrivals span 30. 90.3% of
episode pairs have overlapping outcome windows and at most 2 arrival sessions
are 20 or more sessions apart, so purged folds leave no training set. README
rule 9 legislates exactly this case and prescribes unpurged contiguous blocks
with the leak disclosed rather than no folds at all, so the fold comparison IS
run — as a SECONDARY, and with its bias stated: a fitted model can memorise the
one shared market path and the fit-free ATR baseline cannot, so the leak favours
the model. A win there is weak evidence; a loss is strong.

MULTIPLICITY
Exploratory, so no registered bar. A Bonferroni reference line of 0.05/3 =
0.0167 is printed so three coefficients are not read as three independent
chances at 0.05. Nothing here promotes anything.

SEEDS: base 0; coefficient bootstrap 1. The secondary carries NO inference
on purpose — a bootstrap interval around a knowingly leaking comparison
would put false precision on a biased number. It prints point values only.

RESULT (run 2026-09-29; the run of record is the SECOND — see the note below)
Panel 198 episodes / 26 arrival clusters, 194 complete cases. PIT violations 0,
3 rows unresolved against the news store.

  control    atr               beta -0.0594  t -7.66  p_wcb 0.0006
  control    ma50_dist         beta -0.0500  t -3.74  p_wcb 0.0050
  candidate  catalyst_age_h    beta +0.0056  t +0.57  p_wcb 0.5557
  candidate  catalyst_strength beta +0.0123  t +0.94  p_wcb 0.4242
  candidate  ma200_slope       beta -0.0176  t -1.76  p_wcb 0.0847

Re-run after the review hardening (PIT rows excluded rather than counted,
duplicate-brief branch made loud): identical to the digit, because zero rows
hit either guard.

NULL on the news axis. Nothing approaches the 0.0167 reference line, and only
`ma200_slope` clears even a plain 0.05 on neither reading (0.0847 jointly,
0.0601 with the controls alone).

Independent reproduction of the known structure: coefficients here are in raw
label units per 1 sd of feature. sd(`sel_ar_20`) on this panel is 0.1713, so in
the published table's units ATR is -0.347 against its -0.378 and MA50 -0.292
against -0.303. Two panels built by different code agree to within a few
hundredths. `ma200_slope` converts to -0.103, which lands on the 0.10 smallest
actionable effect #1227 froze — and is still indistinguishable from zero at this
N, is one of three exploratory coefficients, and is not a finding.

Secondary, LEAKING as declared: baseline A (fit-free -ATR) +0.253, baseline B
(ATR + MA50, fitted) +0.315, model (all five) +0.314. Model minus B is -0.002.
The leak was the model's advantage and it still did not beat the structure
already known to be real; it beats A by +0.061 only because it contains MA50.

FIRST RUN DISCARDED, recorded rather than hidden: it recomputed the arrival
anchor with `session_on_or_after` instead of reading `anchor_session`, which
disagrees on 296 of 466 burnt rows and made 65 articles look published after
their own anchor open. The candidates read null in both runs, but the feature
was mis-specified, so only the second run stands. The test file now forbids the
call with an AST gate.

Last run: 2026-09-29 (second run). Verdict: null on the news axis.
"""

from __future__ import annotations

import argparse
import glob
from collections import defaultdict

import numpy as np
import pandas as pd
from alphalens_pipeline.paper.calendar import session_open_utc
from alphalens_research.diagnostics import edge_stores
from alphalens_research.diagnostics.options_retro import (
    cluster_ols,
    ticker_episode_dedup,
    wild_cluster_bootstrap_p,
)
from scipy import stats as scipy_stats

EX = "XNYS"
BURNT_CUTOFF = "2026-07-05"
LABEL = "sel_ar_20"
LABEL_STATUS = "sel_label_status_20"

SEED = 0
SEED_COEF_BOOT = SEED + 1
N_BOOT = 10_000
BLOCK_SESSIONS = 5
REFERENCE_BAR = 0.05 / 3  # descriptive only — this look registers nothing

CONTROLS = (
    ("technical_atr_pct", "atr"),
    ("technical_ma50_distance_pct", "ma50_dist"),
)
CANDIDATES = (
    ("catalyst_age_h", "catalyst_age_h"),
    ("catalyst_strength", "catalyst_strength"),
    ("technical_ma200_slope_pct_per_day", "ma200_slope"),
)
_BRIEF_COLS = [
    "technical_atr_pct",
    "technical_ma50_distance_pct",
    "technical_ma200_slope_pct_per_day",
    "catalyst_strength",
    "source_event_url",
    "source_event_published_at",
]


# ------------------------------------------------------------------- helpers
def pick_news_timestamp(candidates, stamped_date):
    """Choose one publication timestamp for an article with several recorded.

    Frozen rule: prefer a candidate whose UTC date equals the date the brief
    stamped, taking the latest among those; otherwise the latest overall. That
    keeps the recovered value consistent with what the pipeline itself wrote,
    which matters because the two ambiguous urls on this panel disagree by
    about 46 hours.
    """
    if not candidates:
        return None
    same_day = [c for c in candidates if c.date() == stamped_date]
    return max(same_day) if same_day else max(candidates)


def contiguous_block_folds(sessions, block_sessions=BLOCK_SESSIONS):
    """Split ordered arrival sessions into contiguous blocks, runt merged.

    Purged folds are infeasible at a 20-session label over a 30-session arrival
    span, so these are the unpurged contiguous blocks README rule 9 prescribes
    for exactly this situation. The runt is merged into its predecessor rather
    than dropped, so every session is scored exactly once.
    """
    ordered = sorted(sessions)
    if not ordered:
        return []
    blocks = [ordered[i : i + block_sessions] for i in range(0, len(ordered), block_sessions)]
    if len(blocks) > 1 and len(blocks[-1]) < block_sessions:
        blocks[-2] = blocks[-2] + blocks[-1]
        blocks.pop()
    return blocks


def catalyst_age_hours(chosen, open_utc):
    """Hours from publication to the anchor open, or NaN if that is not a lag.

    An article at or after the open is not a point-in-time feature, so it is
    excluded rather than entered with a negative value. Counting the violation
    and keeping the row would put a future-stamped value on the money side of
    the fit. Zero rows hit this after the anchor fix; the guard is here so a
    future store change cannot reintroduce it silently.
    """
    if chosen is None or chosen >= open_utc:
        return float("nan")
    return (open_utc - chosen).total_seconds() / 3600.0


def load_news_index():
    """url -> list of tz-aware UTC publication timestamps, from the news store."""
    index = defaultdict(list)
    for path in sorted(glob.glob(str(edge_stores.HOME / "thematic_news" / "*.parquet"))):
        frame = pd.read_parquet(path, columns=["url", "timestamp"])
        for url, stamp in zip(frame["url"], frame["timestamp"], strict=True):
            if url is None or pd.isna(stamp):
                continue
            index[str(url)].append(pd.Timestamp(stamp).to_pydatetime())
    return index


# ---------------------------------------------------------------- panel build
def build_panel():
    """The burnt episode panel with the frozen regressors. Returns (frame, diag)."""
    files = sorted(glob.glob(str(edge_stores.HOME / "selection_labels" / "*.parquet")))
    labels = pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)
    labels["ticker"] = labels["ticker"].astype(str).str.upper()
    burnt = labels[
        (labels[LABEL_STATUS].astype(str) == "ok")
        & (labels["brief_date"].astype(str) <= BURNT_CUTOFF)
    ].copy()

    briefs = edge_stores.load_store(edge_stores.HOME / "thematic_briefs")
    briefs["ticker"] = briefs["ticker"].astype(str).str.upper()
    bix = briefs.set_index(["brief_date", "ticker"])[
        [c for c in _BRIEF_COLS if c in briefs.columns]
    ]

    diag = {"pre_join": len(burnt), "no_brief": 0, "news_unresolved": 0, "pit_violations": 0}
    rows = []
    for _, r in burnt.drop_duplicates(subset=["brief_date", "ticker"]).iterrows():
        key = (r["brief_date"], str(r["ticker"]).upper())
        try:
            brief = bix.loc[key]
        except KeyError:
            diag["no_brief"] += 1
            continue
        if isinstance(brief, pd.DataFrame):
            # Measured 2026-09-29: zero duplicated (brief_date, ticker) in the
            # brief store and zero on the label side after the status filter,
            # so this cannot happen today. If the store shape ever changes,
            # picking row zero would silently choose one brief over another.
            raise AssertionError(f"the brief store holds more than one row for {key}")
        rows.append(
            {
                "brief_date": r["brief_date"],
                "ticker": key[1],
                # READ, never recomputed — see OUTCOME AND ANCHOR.
                "arrival": r["anchor_session"],
                LABEL: r[LABEL],
                **brief.to_dict(),
            }
        )
    panel = pd.DataFrame(rows)
    diag["joined"] = len(panel)

    panel = ticker_episode_dedup(panel).reset_index(drop=True)
    diag["episodes"] = len(panel)

    news = load_news_index()
    ages = []
    for _, r in panel.iterrows():
        open_utc = session_open_utc(r["arrival"], EX)
        stamped = r.get("source_event_published_at")
        parsed = pd.to_datetime(stamped, errors="coerce") if stamped is not None else pd.NaT
        stamped_date = parsed.date() if not pd.isna(parsed) else None
        chosen = pick_news_timestamp(news.get(str(r.get("source_event_url"))) or [], stamped_date)
        if chosen is None:
            diag["news_unresolved"] += 1
        elif chosen >= open_utc:
            diag["pit_violations"] += 1
        ages.append(catalyst_age_hours(chosen, open_utc))
    panel["catalyst_age_h"] = ages
    diag["clusters"] = panel["arrival"].astype(str).nunique()
    return panel, diag


def standardise(frame, columns):
    out = frame.copy()
    for col in columns:
        values = pd.to_numeric(out[col], errors="coerce")
        sd = values.std(ddof=0)
        out[col] = (values - values.mean()) / sd if sd and sd > 0 else np.nan
    return out


# ------------------------------------------------------------------ inference
def joint_fit(panel, regressors, seed=SEED_COEF_BOOT):
    """cluster_ols of the label on [const, *regressors], clusters = arrival."""
    cols = list(regressors)
    sub = panel[panel[[*cols, LABEL]].notna().all(axis=1)]
    if len(sub) < 20:
        return None
    y = sub[LABEL].astype(float).to_numpy()
    X = np.column_stack([np.ones(len(sub))] + [sub[c].astype(float).to_numpy() for c in cols])
    clusters = np.array([str(a) for a in sub["arrival"]])
    fit = cluster_ols(y, X, clusters)
    out = []
    for i, name in enumerate(cols, start=1):
        p_wcb = wild_cluster_bootstrap_p(y, X, clusters, i, n_boot=N_BOOT, seed=seed + i)
        out.append(
            {
                "name": name,
                "beta": float(fit.beta[i]),
                "t_cr2": float(fit.t_cr2[i]),
                "p_wcb": float(p_wcb),
                "n": len(sub),
                "clusters": fit.n_clusters,
            }
        )
    return out


def _ols_predict(train, val, y_train, columns):
    """Fit OLS on the training block and score the validation block."""
    xtr = np.column_stack(
        [np.ones(len(train))] + [train[c].astype(float).to_numpy() for c in columns]
    )
    xv = np.column_stack([np.ones(len(val))] + [val[c].astype(float).to_numpy() for c in columns])
    beta = np.linalg.pinv(xtr.T @ xtr) @ (xtr.T @ y_train)
    return xv @ beta


def _rank_within_fold(values):
    return scipy_stats.rankdata(values) / max(len(values), 1)


def fold_comparison(panel, model_cols):
    """SECONDARY, and it leaks. Unpurged contiguous arrival-session blocks.

    Every training label shares outcome sessions with every validation label at
    this horizon, and the leak is asymmetric: the fitted model can memorise the
    shared market path, the fit-free ATR baseline cannot. Read a win here as
    weak and a loss as strong.
    """
    cols = list(model_cols)
    sub = panel[panel[[*cols, LABEL]].notna().all(axis=1)].copy()
    sessions = sorted(set(sub["arrival"]))
    blocks = contiguous_block_folds(sessions)
    if len(blocks) < 2:
        return None
    pooled = {"model": [], "atr_only": [], "atr_ma50": [], "truth": []}
    per_fold = []
    for block in blocks:
        in_val = sub["arrival"].isin(block)
        train, val = sub[~in_val], sub[in_val]
        if len(train) < 20 or len(val) < 5:
            continue
        ytr = train[LABEL].astype(float).to_numpy()
        yv = val[LABEL].astype(float).to_numpy()

        pooled["model"].extend(_rank_within_fold(_ols_predict(train, val, ytr, cols)))
        pooled["atr_ma50"].extend(
            _rank_within_fold(_ols_predict(train, val, ytr, [c for _, c in CONTROLS]))
        )
        # Fit-free baseline: ATR with its a-priori direction, higher ATR worse.
        pooled["atr_only"].extend(_rank_within_fold(-val["atr"].astype(float).to_numpy()))
        pooled["truth"].extend(_rank_within_fold(yv))
        per_fold.append((str(block[0]), str(block[-1]), len(train), len(val)))
    if not per_fold:
        return None
    truth = np.array(pooled["truth"])
    scores = {
        k: float(scipy_stats.spearmanr(np.array(v), truth)[0])
        for k, v in pooled.items()
        if k != "truth"
    }
    return {"per_fold": per_fold, "scores": scores}


# ----------------------------------------------------------------------- main
def run():
    panel, diag = build_panel()
    print("=" * 72)
    print("BURNT-PANEL NEWS-AXIS PROBE — exploratory, charges 0, cutoff", BURNT_CUTOFF)
    print("=" * 72)
    print(
        f"panel: {diag['episodes']} episodes | {diag['clusters']} arrival-session clusters "
        f"| {panel['ticker'].nunique()} tickers | "
        f"{min(panel['brief_date'])} -> {max(panel['brief_date'])}"
    )
    print(
        f"build: {diag['pre_join']} label rows -> {diag['joined']} joined -> "
        f"{diag['episodes']} episodes (no-brief dropped {diag['no_brief']})"
    )
    print(
        f"catalyst_age_h: unresolved {diag['news_unresolved']} | "
        f"PIT violations (article at/after the arrival open) {diag['pit_violations']}"
    )

    names = [n for _, n in CONTROLS] + [n for _, n in CANDIDATES]
    for src, name in CONTROLS + CANDIDATES:
        source = panel[src] if src in panel.columns else panel.get(name)
        panel[name] = pd.to_numeric(source, errors="coerce")
    panel = standardise(panel, names)

    print("\ncoverage after standardisation (episodes with the value present):")
    for name in names:
        present = int(panel[name].notna().sum())
        print(f"  {name:22s} {present:4d} / {len(panel)}")

    print("\nPRIMARY — jointly fitted, standardised, clusters = arrival session")
    print(f"  reference line (descriptive, not a registered bar): {REFERENCE_BAR:.4f}")
    full = joint_fit(panel, names)
    if full is None:
        print("  not estimable: too few complete cases")
    else:
        print(f"  complete-case n={full[0]['n']} episodes / {full[0]['clusters']} clusters")
        for row in full:
            role = "control  " if row["name"] in {n for _, n in CONTROLS} else "candidate"
            print(
                f"  {role} {row['name']:22s} beta={row['beta']:+.4f} "
                f"t_cr2={row['t_cr2']:+.2f} p_wcb={row['p_wcb']:.4f}"
            )

    print("\nPER-CANDIDATE (candidate + both controls, on its own complete cases)")
    for _, name in CANDIDATES:
        one = joint_fit(panel, [n for _, n in CONTROLS] + [name])
        if one is None:
            print(f"  {name:22s} not estimable")
            continue
        row = next(r for r in one if r["name"] == name)
        print(
            f"  {name:22s} beta={row['beta']:+.4f} t_cr2={row['t_cr2']:+.2f} "
            f"p_wcb={row['p_wcb']:.4f}  (n={row['n']} / {row['clusters']} clusters)"
        )

    print("\nSECONDARY — unpurged contiguous block folds. THIS LEAKS, by construction.")
    print("  Every training label shares outcome sessions with every validation label")
    print("  at a 20-session horizon on a 30-session span; the fitted model can")
    print("  memorise the shared path and the fit-free ATR baseline cannot.")
    comp = fold_comparison(panel, names)
    if comp is None:
        print("  not estimable")
    else:
        for start, end, ntr, nv in comp["per_fold"]:
            print(f"  fold {start}..{end}: train {ntr} / val {nv}")
        for key, label in (
            ("atr_only", "baseline A  -ATR, fit-free"),
            ("atr_ma50", "baseline B  ATR + MA50, fitted"),
            ("model", "model       all five, fitted"),
        ):
            print(f"  {label:34s} pooled rank-within-fold Spearman {comp['scores'][key]:+.3f}")
        print(
            f"  delta model - A = {comp['scores']['model'] - comp['scores']['atr_only']:+.3f} | "
            f"model - B = {comp['scores']['model'] - comp['scores']['atr_ma50']:+.3f}"
        )

    print("\n" + "=" * 72)
    print("Exploratory. Nothing here promotes anything, and nothing here may be")
    print("cited as evidence without paying the budget (ADR 0013 R4).")
    print("=" * 72)


def main():
    parser = argparse.ArgumentParser(description=(__doc__ or "burnt-panel probe").splitlines()[0])
    parser.add_argument("--run", action="store_true", required=True, help="run the probe")
    parser.parse_args()
    run()


if __name__ == "__main__":
    main()
