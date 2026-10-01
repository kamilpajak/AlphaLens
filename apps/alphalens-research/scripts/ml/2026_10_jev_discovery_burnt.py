"""Discovery pass: do the System One features carry a coefficient on the BURNT panel?

PURPOSE, which is NOT "is this a finding"
-----------------------------------------
This measures an EFFECT SIZE so a power simulation can be run. Registering a
confirmation without one is the mistake #1227 already made and had to correct: its
power read 46.2% under Holm and 66.7% at a family of one, against an 80% bar, and
that was for ATR, a feature whose discovery effect was known to be -0.347. The
System One columns have no estimate at all, so there is nothing to compute power
from and therefore nothing that can honestly be registered yet.

The order this script exists to serve is: burnt discovery -> power simulation ->
registration. Not registration first.

EXPLORATORY, ZERO CHARGE, and the three R4 conditions are stated so a later reader
can check them rather than take them on trust:

  1. CUTOFF — reads outcomes matured to `brief_date <= 2026-07-05` only, the frozen
     discovery window. Ledger rule 3 already spent it in the June and July sweeps,
     and `a20_power_preflight_2026_09.md` §2.3 records that burnt outcome values are
     fair game.
  2. LEDGER — a row is appended to §4.1 of `edge_hypothesis_budget_2026_07.md` with
     `charges: 0 (exploration)` **BEFORE** the run, per README house rule 11. The
     2026-10-01 self-grade row records a deviation from exactly this, written after
     its run; this script is not to be executed until its own row is committed.
  3. NOT CITED — any later CLAIM on these columns pays, and a candidate without a
     cluster slot opens a new ledger row, raising every other hypothesis's
     denominator.

The held-out panel is NOT touched, so #1227 is unaffected.

THE SIX CANDIDATES, FROZEN BEFORE ANY OUTPUT WAS SEEN
----------------------------------------------------
Each names the hand-chosen mechanism it would replace, because a column with no
counterpart is a new hypothesis rather than a repair:

| candidate                   | replaces                                             |
| --------------------------- | ---------------------------------------------------- |
| `jev_materiality`           | `EVENT_TYPE_TIER`'s role as a MAGNITUDE prior        |
| `jev_catalyst_mass`         | `NOISE_EVENT_TYPES`' role as a market-moving GATE    |
| `jev_concrete_fact`         | `catalyst_confidence`, the extractor's self-grade    |
| `jev_company_gain`          | `llm_confidence`, the mapper's self-grade            |
| `jev_event_type_confidence` | nothing — new                                        |
| `jev_touches_industry`      | nothing — new                                        |

`jev_catalyst_mass` is `1 - sum(P over NOISE_EVENT_TYPES)` from the stored type
distribution: how much probability mass says this is a market-moving event at all.
Deliberately NOT the expectation of our own tier map under Jev's distribution —
that would mix the hand-written numbers back in and defeat the comparison.

Six candidates, so the descriptive reference line is 0.05 / 6 = 0.0083. It is a
reference and not a bar: this look registers nothing.

CONTROLS are ATR and the MA50 extension, the same two the published burnt A20 table
carries (ATR -0.378, MA50 -0.303 in standardised units). Keeping them as controls is
what makes the test strict — a candidate's coefficient is its increment over the
structure already known to be real.

WHAT IS REPORTED, AND WHY THE LAST ONE IS THE POINT
---------------------------------------------------
1. Rank correlation of every candidate with ATR, measured and printed BEFORE the
   coefficients, because the July EWMA test's lesson is that a feature strongly
   correlated with ATR is ATR in a new dress.
2. Jointly fitted cluster-robust coefficients plus a restricted wild cluster
   bootstrap p, the same instrument that produced the published burnt table, so the
   numbers are directly comparable.
3. Each candidate alone with the two controls, because a composite and a component
   in one fit play against each other — measured on 2026-10-01, putting
   `catalyst_confidence` beside `catalyst_strength`, of which it is 40% by
   construction, read p 0.0093 and the decomposition removed it.
4. **The minimum detectable effect per candidate**, in standardised units, against
   the 0.10 smallest actionable effect #1227 froze. This is the deliverable. A
   coefficient without it cannot say whether a null means "no effect" or "no power",
   and on 2026-10-01 I reported a null that was the latter.

Last run: NOT YET RUN.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys

import numpy as np
import pandas as pd
from alphalens_pipeline.thematic.screening.catalyst_signals import NOISE_EVENT_TYPES
from alphalens_research.diagnostics import edge_stores
from alphalens_research.diagnostics.options_retro import (
    cluster_ols,
    ticker_episode_dedup,
    wild_cluster_bootstrap_p,
)
from scipy import stats as scipy_stats

BURNT_CUTOFF = "2026-07-05"
LABEL = "sel_ar_20"
LABEL_STATUS = "sel_label_status_20"
#: The instrument. Rows written under any other token came from a different one and
#: must not pool; the builder's docstring explains why the token moves at all.
FEATURE_VERSION = "jev-v1-96785ab6cd0f"
SEED = 0
SEED_COEF_BOOT = SEED + 1
N_BOOT = 10_000
#: Descriptive only. Six candidates, so six chances at a plain 0.05.
REFERENCE_BAR = 0.05 / 6
#: Frozen by owner decision 2026-09-23 as the smallest effect worth acting on.
ACTIONABLE_DELTA = 0.10
#: Two-sided 0.05 at 80% power: z_.975 + z_.80.
_MDE_Z = 1.959964 + 0.841621

CONTROLS = (
    ("technical_atr_pct", "atr"),
    ("technical_ma50_distance_pct", "ma50_dist"),
)
CANDIDATES = (
    ("jev_materiality", "jev_materiality"),
    ("jev_catalyst_mass", "jev_catalyst_mass"),
    ("jev_concrete_fact", "jev_concrete_fact"),
    ("jev_company_gain", "jev_company_gain"),
    ("jev_event_type_confidence", "jev_type_confidence"),
    ("jev_touches_industry", "jev_touches_industry"),
)
_BRIEF_COLS = [
    "technical_atr_pct",
    "technical_ma50_distance_pct",
    "source_event_url",
]


def iso_date(value) -> str:
    """One text form for a date that arrives as a `date`, a Timestamp or a string.

    Join keys that look identical when printed and differ by type are the quiet kind
    of bug: nothing raises, the join simply matches nothing.
    """
    if value is None:
        return ""
    text = str(value)
    return text[:10] if len(text) >= 10 else text


def catalyst_mass(probs_json) -> float | None:
    """`1 - P(the event type is one of ours marked non-market-moving)`.

    Returns None rather than 0.0 on a missing or unparseable distribution: a row we
    could not read is not a row whose catalyst mass is zero, and imputing one would
    put a confident value where there is no measurement.
    """
    if not isinstance(probs_json, str) or not probs_json:
        return None
    try:
        probs = json.loads(probs_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(probs, dict):
        return None
    noise = sum(float(v) for k, v in probs.items() if k in set(NOISE_EVENT_TYPES))
    return float(min(1.0, max(0.0, 1.0 - noise)))


def load_jev_article() -> pd.DataFrame:
    """Article-pass features at the current version, one row per (url, news_id)."""
    files = sorted(glob.glob(str(edge_stores.HOME / "jev_features" / "article" / "*.parquet")))
    if not files:
        raise RuntimeError(
            f"no article features under {edge_stores.HOME / 'jev_features' / 'article'}; "
            "run 2026_10_jev_feature_layer.py --run --pass article first"
        )
    d = pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)
    d = d[d["jev_feature_version"].astype(str) == FEATURE_VERSION].copy()
    d["jev_catalyst_mass"] = [catalyst_mass(v) for v in d["jev_event_type_probs_json"]]
    keep = [
        "url",
        "jev_materiality",
        "jev_concrete_fact",
        "jev_event_type_confidence",
        "jev_catalyst_mass",
    ]
    return d[keep].drop_duplicates("url", keep="last").set_index("url")


def load_jev_candidate() -> pd.DataFrame:
    """Candidate-pass features at the current version, keyed (brief_date, ticker)."""
    files = sorted(glob.glob(str(edge_stores.HOME / "jev_features" / "candidate" / "*.parquet")))
    if not files:
        raise RuntimeError(
            f"no candidate features under {edge_stores.HOME / 'jev_features' / 'candidate'}; "
            "run 2026_10_jev_feature_layer.py --run --pass candidate first"
        )
    d = pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)
    d = d[d["jev_feature_version"].astype(str) == FEATURE_VERSION].copy()
    d["ticker"] = d["ticker"].astype(str).str.upper()
    # The builder writes `brief_date` as the parquet filename stem, so a STRING, while
    # the label store carries `datetime.date`. The two never compare equal, so a tuple
    # key from one side silently misses every row of the other. Measured on the first
    # run of this script: 391 of 391 candidate joins missed and two of the six
    # candidates arrived all-null, which would have read as "they carry nothing".
    d["brief_date"] = d["brief_date"].map(iso_date)
    keep = ["brief_date", "ticker", "jev_touches_industry", "jev_company_gain"]
    return (
        d[keep]
        .drop_duplicates(["brief_date", "ticker"], keep="last")
        .set_index(["brief_date", "ticker"])
    )


def build_panel() -> tuple[pd.DataFrame, dict]:
    """The burnt episode panel with the six frozen candidates joined on."""
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
    art = load_jev_article()
    cand = load_jev_candidate()

    diag = {
        "pre_join": len(burnt),
        "no_brief": 0,
        "no_article_feature": 0,
        "no_candidate_feature": 0,
    }
    rows = []
    for _, r in burnt.drop_duplicates(subset=["brief_date", "ticker"]).iterrows():
        key = (r["brief_date"], str(r["ticker"]).upper())
        try:
            brief = bix.loc[key]
        except KeyError:
            diag["no_brief"] += 1
            continue
        if isinstance(brief, pd.DataFrame):
            raise AssertionError(f"the brief store holds more than one row for {key}")
        row = {
            "brief_date": r["brief_date"],
            "ticker": key[1],
            # READ from the label store, never recomputed: owner decision D4 fixes the
            # anchor at the first session AFTER the brief date, which
            # `session_on_or_after` does not return when the brief date is itself a
            # session. The two disagreed on 296 of 466 burnt rows.
            "arrival": r["anchor_session"],
            LABEL: r[LABEL],
            **brief.to_dict(),
        }
        url = row.get("source_event_url")
        if isinstance(url, str) and url in art.index:
            row.update(art.loc[url].to_dict())
        else:
            diag["no_article_feature"] += 1
        cand_key = (iso_date(r["brief_date"]), key[1])
        if cand_key in cand.index:
            row.update(cand.loc[cand_key].to_dict())
        else:
            diag["no_candidate_feature"] += 1
        rows.append(row)

    panel = pd.DataFrame(rows)
    diag["joined"] = len(panel)
    # A join that misses every single row is a key bug, never a property of the data.
    # Reporting it as coverage would let the pass print "carries nothing" about columns
    # it never read.
    for what, missed in (
        ("article", diag["no_article_feature"]),
        ("candidate", diag["no_candidate_feature"]),
    ):
        if rows and missed == len(rows):
            raise RuntimeError(
                f"the {what} feature join matched 0 of {len(rows)} rows. That is a "
                "join-key bug, not an absence of features; check the key types on both "
                "sides before reading anything into a coefficient."
            )
    panel = ticker_episode_dedup(panel).reset_index(drop=True)
    diag["episodes"] = len(panel)
    diag["clusters"] = panel["arrival"].astype(str).nunique()
    return panel, diag


def standardise(frame: pd.DataFrame, columns) -> pd.DataFrame:
    out = frame.copy()
    for col in columns:
        values = pd.to_numeric(out[col], errors="coerce")
        sd = values.std(ddof=0)
        out[col] = (values - values.mean()) / sd if sd and sd > 0 else np.nan
    return out


def joint_fit(panel: pd.DataFrame, regressors, seed: int = SEED_COEF_BOOT):
    """cluster_ols of the label on [const, *regressors]; clusters = arrival session."""
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
        se = abs(fit.beta[i] / fit.t_cr2[i]) if fit.t_cr2[i] else float("nan")
        out.append(
            {
                "name": name,
                "beta": float(fit.beta[i]),
                "t_cr2": float(fit.t_cr2[i]),
                "p_wcb": float(p_wcb),
                "se": float(se),
                "n": len(sub),
                "clusters": fit.n_clusters,
            }
        )
    return out


def run() -> int:
    panel, diag = build_panel()
    names = [n for _, n in CONTROLS + CANDIDATES]
    for src, name in CONTROLS + CANDIDATES:
        panel[name] = pd.to_numeric(panel.get(src), errors="coerce")

    print("=" * 76)
    print("JEV DISCOVERY ON THE BURNT PANEL — exploratory, charges 0, cutoff", BURNT_CUTOFF)
    print("instrument:", FEATURE_VERSION)
    print("=" * 76)
    print(
        f"build: {diag['pre_join']} label rows -> {diag['joined']} joined -> "
        f"{diag['episodes']} episodes in {diag['clusters']} arrival clusters"
    )
    print(
        f"  no brief {diag['no_brief']} | no article feature {diag['no_article_feature']} "
        f"| no candidate feature {diag['no_candidate_feature']}"
    )
    sd_label = float(pd.to_numeric(panel[LABEL], errors="coerce").std(ddof=0))
    print(f"sd({LABEL}) on this panel: {sd_label:.4f}")
    print()
    print("coverage (episodes with the value present):")
    for name in names:
        print(f"  {name:24s}{int(panel[name].notna().sum()):>5} / {len(panel)}")

    print()
    print("--- rank correlation of each candidate with ATR (a feature close to ATR is")
    print("    ATR in a new dress; the July EWMA test is the precedent) ---")
    for _, name in CANDIDATES:
        pair = panel[[name, "atr"]].dropna()
        rho = (
            scipy_stats.spearmanr(pair[name], pair["atr"]).statistic
            if len(pair) > 10
            else float("nan")
        )
        print(f"  {name:24s} rho_atr {rho:+.3f}  (n={len(pair)})")

    std = standardise(panel, names)
    print()
    print("--- PRIMARY: jointly fitted, standardised, clusters = arrival session ---")
    print(f"    reference line (descriptive, NOT a bar): {REFERENCE_BAR:.4f}")
    joint = joint_fit(std, names)
    if joint is None:
        print("    too few complete cases to fit")
        return 1
    print(f"    complete-case n={joint[0]['n']} episodes / {joint[0]['clusters']} clusters")
    control_names = {n for _, n in CONTROLS}
    for row in joint:
        role = "control  " if row["name"] in control_names else "candidate"
        print(
            f"    {role} {row['name']:24s} beta={row['beta']:+.4f} t_cr2={row['t_cr2']:+.2f} "
            f"p_wcb={row['p_wcb']:.4f}  std={row['beta'] / sd_label:+.3f}"
        )

    print()
    print("--- PER CANDIDATE (candidate + both controls, on its own complete cases) ---")
    print("    a composite and a component in one fit play against each other; see the docstring")
    per = {}
    for _, name in CANDIDATES:
        one = joint_fit(std, [n for _, n in CONTROLS] + [name])
        if one is None:
            print(f"    {name:24s} too few complete cases")
            continue
        row = next(r for r in one if r["name"] == name)
        per[name] = row
        print(
            f"    {name:24s} beta={row['beta']:+.4f} t_cr2={row['t_cr2']:+.2f} "
            f"p_wcb={row['p_wcb']:.4f}  std={row['beta'] / sd_label:+.3f}  "
            f"(n={row['n']} / {row['clusters']} clusters)"
        )

    print()
    print("=" * 76)
    print("THE DELIVERABLE: minimum detectable effect, standardised, vs the frozen")
    print(f"smallest actionable effect {ACTIONABLE_DELTA:.2f}")
    print("=" * 76)
    print(f"    {'candidate':24s}{'|std beta|':>11}{'MDE':>9}{'powered?':>10}")
    for _, name in CANDIDATES:
        row = per.get(name)
        if row is None or not np.isfinite(row["se"]):
            print(f"    {name:24s}      (not estimated)")
            continue
        mde_std = _MDE_Z * row["se"] / sd_label
        powered = "yes" if mde_std <= ACTIONABLE_DELTA else "NO"
        print(f"    {name:24s}{abs(row['beta'] / sd_label):>11.3f}{mde_std:>9.3f}{powered:>10}")
    print()
    print("    MDE above the actionable floor means a null here is 'no power', not")
    print("    'no effect'. A registration needs the yes column, not a small p.")
    print()
    print("=" * 76)
    print("Exploratory. Nothing here promotes anything, and nothing here may be cited")
    print("as evidence for what ships without paying the budget (ADR 0013 R4).")
    print("=" * 76)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--run",
        action="store_true",
        required=True,
        help="required; reads burnt outcome values, so its ledger row must exist FIRST",
    )
    parser.parse_args(argv)
    return run()


if __name__ == "__main__":
    sys.exit(main())
