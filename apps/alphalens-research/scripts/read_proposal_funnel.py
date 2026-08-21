#!/usr/bin/env python
"""Descriptive read of the mapper proposal funnel — issue #1002.

Executes exactly the plan fixed in
``docs/research/proposal_funnel_first_read_contract_2026_08_21.md``: one primary
estimand (the pooled ``too_big`` share on the resolved-mcap denominator) and the
secondary list, nothing promoted after the fact.

Read-only. No LLM call, no network, no re-fetch of market caps — the caps are
whatever the classifier stamped at run time.

Usage::

    .venv/bin/python apps/alphalens-research/scripts/read_proposal_funnel.py
    .venv/bin/python apps/alphalens-research/scripts/read_proposal_funnel.py --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

FUNNEL_DIR = Path.home() / ".alphalens" / "thematic_candidates" / "proposal_funnel"

IN_BRACKET = "in_bracket"
TOO_SMALL = "too_small"
TOO_BIG = "too_big"
NO_MCAP = "no_mcap"
VERDICTS = (TOO_BIG, IN_BRACKET, TOO_SMALL, NO_MCAP)
# "Resolved" is a WHITELIST, never "not no_mcap". A null or unknown verdict must
# not be able to enter a denominator by failing an inequality.
RESOLVED_VERDICTS = (TOO_BIG, IN_BRACKET, TOO_SMALL)

# Contract §8. Both are pre-committed and must not be tuned to the data.
STABILITY_BAND_PP = 10.0
MIN_RESOLVED_FOR_TEST = 5
STRUCTURAL_MIN_PROPOSALS = 10
STRUCTURAL_MIN_DAYS = 3
THEME_MIN_PROPOSALS = 5

# Contract §"What was known before writing this": the day the hypothesis came
# from. Reported, never used as evidence that the split is stable.
HYPOTHESIS_DAY = "2026-08-06"


def load(funnel_dir: Path) -> pd.DataFrame:
    """Every funnel row on disk, with the file name as the authority on ``asof``."""
    frames = []
    for path in sorted(funnel_dir.glob("*.parquet")):
        frame = pd.read_parquet(path)
        # The file name is the day; the column is belt-checked against it below.
        frame["_file_asof"] = path.stem
        frames.append(frame)
    if not frames:
        raise SystemExit(f"no funnel parquets under {funnel_dir}")
    return pd.concat(frames, ignore_index=True)


def exact_binomial(k: int, n: int) -> tuple[float, float]:
    """95% Clopper-Pearson interval.

    Contract §6 fixed an EXACT binomial interval. Wilson would be the better
    default in most settings and was what a first draft used, but substituting
    it silently is precisely the kind of after-the-fact swap the contract exists
    to make visible — so the contract wins.
    """
    if n == 0:
        return (float("nan"), float("nan"))
    lo = float(stats.beta.ppf(0.025, k, n - k + 1)) if k > 0 else 0.0
    hi = float(stats.beta.ppf(0.975, k + 1, n - k)) if k < n else 1.0
    return (lo, hi)


def pct(x: float) -> str:
    return "n/a" if math.isnan(x) else f"{100 * x:.1f}%"


def _permutation_homogeneity_p(
    table: list[list[int]], *, observed: float, draws: int = 20000
) -> float:
    """P(chi2 >= observed) under "every day draws from one pooled rate".

    Deterministic seed: the same parquets must give the same p twice.
    """
    sizes = np.array([row[0] + row[1] for row in table])
    n_all = int(sizes.sum())
    n_big = sum(row[0] for row in table)
    if n_big in (0, n_all):
        # A degenerate column margin: every proposal on every day is the same
        # verdict, so there is nothing to be heterogeneous about — and the
        # expected counts below would be zero and divide.
        return 1.0
    labels = np.concatenate([np.ones(n_big), np.zeros(n_all - n_big)])
    rng = np.random.default_rng(1002)
    edges = np.cumsum(sizes)[:-1]
    # Loop-invariant: both margins are fixed by the permutation, so the expected
    # counts are the same for every draw.
    exp_big = sizes * (n_big / n_all)
    exp_rest = sizes - exp_big
    hits = 0
    for _ in range(draws):
        shuffled = rng.permutation(labels)
        per_day = np.array([chunk.sum() for chunk in np.split(shuffled, edges)])
        # Pearson chi-square on the 2 x k table, computed directly.
        stat = (((per_day - exp_big) ** 2) / exp_big).sum() + (
            (((sizes - per_day) - exp_rest) ** 2) / exp_rest
        ).sum()
        if stat >= observed:
            hits += 1
    return (hits + 1) / (draws + 1)


def _prompt_sha(config_version: str) -> str:
    """The mapper's own prompt digest out of the config token, or ``unparsed``."""
    try:
        payload = json.loads(config_version)
    except (TypeError, ValueError):
        return "unparsed"
    # Valid JSON is not necessarily an object: `null`, `[]` and `"v1"` all parse
    # and none of them has ``.get``.
    if not isinstance(payload, dict):
        return "unparsed"
    return str(payload.get("prompt_sha", "absent"))


def _homogeneity(days: list[dict], *, pooled_share: float) -> dict:
    """Contract §7 + §8 on one set of days: the band verdict and the chi-square.

    The band is the PRE-COMMITTED verdict and is reported as such. The
    permutation p is a POST-HOC sensitivity — the contract fixed a Pearson
    chi-square and a range rule, and never mentioned a permutation test. It is
    computed because the chi-square's own reliability condition fails here, but
    it does not override the band.
    """
    eligible = [d for d in days if d["resolved"] >= MIN_RESOLVED_FOR_TEST]
    table = [[d["counts"][TOO_BIG], d["resolved"] - d["counts"][TOO_BIG]] for d in eligible]
    chi2 = pval = min_expected = perm_p = float("nan")
    if len(table) >= 2:
        stat, p_asym, _dof, expected_tab = stats.chi2_contingency(table, correction=False)
        chi2 = float(stat)  # pyright: ignore[reportArgumentType]
        pval = float(p_asym)  # pyright: ignore[reportArgumentType]
        min_expected = float(np.asarray(expected_tab).min())
        perm_p = _permutation_homogeneity_p(table, observed=chi2)
    shares = [float(d["too_big_share"]) for d in eligible]
    worst = max((abs(s - pooled_share) for s in shares), default=float("nan"))
    return {
        "days_tested": len(table),
        "days_used": [d["asof"] for d in eligible],
        "chi2": chi2,
        "p": pval,
        "min_expected_cell": min_expected,
        "chi2_reliable": bool(min_expected >= 5) if not math.isnan(min_expected) else False,
        "permutation_p_post_hoc": perm_p,
        "observed_range": [min(shares), max(shares)] if shares else [float("nan")] * 2,
        "max_abs_deviation_pp": 100 * worst,
        "band_pp": STABILITY_BAND_PP,
        # Contract §13 vocabulary. This is THE verdict; nothing below overrides it.
        "precommitted_verdict": "stable" if 100 * worst <= STABILITY_BAND_PP else "varies",
    }


def verdict_counts(frame: pd.DataFrame) -> dict[str, int]:
    counts = frame["bracket_verdict"].value_counts().to_dict()
    return {v: int(counts.get(v, 0)) for v in VERDICTS}


def _cap_distribution(caps: pd.Series) -> dict:
    """Min / deciles / max / log10 histogram for one set of caps.

    Buckets are HALF-OPEN, ``[10^b, 10^(b+1))``. Worth stating because the
    bracket itself is closed at both ends: a cap of exactly $10B is
    ``in_bracket`` yet lands in the ``1e10`` bucket. No cap sits on a boundary
    in the current data, but the two conventions genuinely differ.
    """
    histogram = {
        f"1e{b}": int(((caps >= 10.0**b) & (caps < 10.0 ** (b + 1))).sum()) for b in range(6, 13)
    }
    return {
        "n": len(caps),
        "min": float(caps.min()),
        "max": float(caps.max()),
        "median": float(caps.median()),
        "deciles": {f"p{d}": float(caps.quantile(d / 100)) for d in range(10, 100, 10)},
        "log10_histogram_half_open": histogram,
        # The buckets span $1M .. $10T. Anything outside falls through every
        # comparison silently, and the renderer hides empty buckets, so the
        # shortfall would never appear on screen. Print it instead.
        "uncounted_by_histogram": len(caps) - sum(histogram.values()),
    }


def build_report(df: pd.DataFrame) -> dict:
    report: dict = {}

    # ---- integrity, before any share is computed -------------------------
    mismatched = int((df["asof"].astype(str) != df["_file_asof"]).sum())
    days = sorted(df["_file_asof"].unique())
    expected = pd.date_range(days[0], days[-1], freq="D").strftime("%Y-%m-%d")
    report["integrity"] = {
        "rows": len(df),
        "days_present": len(days),
        "first_day": days[0],
        "last_day": days[-1],
        "missing_days": [d for d in expected if d not in set(days)],
        "asof_column_disagrees_with_filename": mismatched,
        "unknown_verdicts": sorted(set(df["bracket_verdict"].dropna()) - set(VERDICTS)),
        "null_verdicts": int(df["bracket_verdict"].isna().sum()),
        "mcap_null_but_resolved": int(
            (df["bracket_verdict"].isin(RESOLVED_VERDICTS) & df["market_cap"].isna()).sum()
        ),
        "mcap_present_but_no_mcap_verdict": int(
            ((df["bracket_verdict"] == NO_MCAP) & df["market_cap"].notna()).sum()
        ),
        "mapper_config_versions": sorted(df["mapper_config_version"].dropna().unique().tolist()),
        # Every per-theme / per-version aggregate below is a groupby, and pandas
        # DROPS a null group key without a word. Today all three are 0, which is
        # exactly why the count has to be printed: a clean corpus is only half
        # the evidence, and a silent drop would otherwise shrink a denominator
        # with nothing on screen to say so.
        "null_group_keys": {
            col: int(df[col].isna().sum()) for col in ("theme", "ticker", "mapper_config_version")
        },
    }

    # Fail closed. The contract's FAILURE MODES (§10) list refusal, no_mcap,
    # duplicates, repeated themes and a missing day — a null or unrecognised
    # verdict is NOT among them, so there is no pre-committed rule for what
    # denominator such a row belongs to. Choosing one here, after the fact,
    # would be exactly the move the contract exists to prevent. Stop instead
    # and amend the contract.
    ig = report["integrity"]
    if ig["null_verdicts"] or ig["unknown_verdicts"]:
        raise SystemExit(
            f"unplanned verdict values: {ig['null_verdicts']} null, "
            f"{ig['unknown_verdicts']} unknown. The contract has no rule for these; "
            "amend it before reading further."
        )

    resolved = df[df["bracket_verdict"].isin(RESOLVED_VERDICTS)]

    # ---- PRIMARY ---------------------------------------------------------
    n_res = len(resolved)
    k_big = int((resolved["bracket_verdict"] == TOO_BIG).sum())
    lo, hi = exact_binomial(k_big, n_res)
    report["primary"] = {
        "estimand": "share of proposals with bracket_verdict == too_big, denominator = resolved mcap",
        "too_big": k_big,
        "resolved": n_res,
        "share": k_big / n_res if n_res else float("nan"),
        "ci95_exact": [lo, hi],
    }

    # ---- SECONDARY: pooled four-way split on ALL proposals ---------------
    pooled = verdict_counts(df)
    report["pooled_all_proposals"] = {
        "counts": pooled,
        "shares": {v: pooled[v] / len(df) for v in VERDICTS},
        "denominator": len(df),
    }

    # ---- SECONDARY: per day ----------------------------------------------
    per_day = []
    for day, g in df.groupby("_file_asof", sort=True):
        g_res = g[g["bracket_verdict"].isin(RESOLVED_VERDICTS)]
        k = int((g_res["bracket_verdict"] == TOO_BIG).sum())
        n = len(g_res)
        d_lo, d_hi = exact_binomial(k, n)
        per_day.append(
            {
                "asof": day,
                "proposals": len(g),
                "resolved": n,
                "themes": int(g["theme"].nunique()),
                "counts": verdict_counts(g),
                "too_big_share": k / n if n else float("nan"),
                "ci95_exact": [d_lo, d_hi],
                "is_hypothesis_day": day == HYPOTHESIS_DAY,
            }
        )
    report["per_day"] = per_day

    # ---- SECONDARY: homogeneity test (contract §7) -----------------------
    # The contract excludes the hypothesis day from EVERY "was it stable"
    # comparison — it is the day the question came from, so it cannot also be
    # evidence about the answer. The pooled share in PRIMARY keeps it (that is a
    # census, not a comparison); only this block drops it. The with-08-06
    # variant is computed too, as a sensitivity, never as the headline.
    stability_days = [d for d in per_day if not d["is_hypothesis_day"]]
    report["homogeneity"] = _homogeneity(stability_days, pooled_share=report["primary"]["share"])
    report["homogeneity_sensitivity_with_hypothesis_day"] = _homogeneity(
        per_day, pooled_share=report["primary"]["share"]
    )

    # ---- SECONDARY: full cap distribution --------------------------------
    report["cap_distribution"] = _cap_distribution(resolved["market_cap"].dropna().astype(float))

    # ---- SECONDARY: per theme --------------------------------------------
    themes = []
    for theme, g in df.groupby("theme", sort=True):
        g_res = g[g["bracket_verdict"].isin(RESOLVED_VERDICTS)]
        n_in = int((g["bracket_verdict"] == IN_BRACKET).sum())
        n_days = int(g["_file_asof"].nunique())
        caps_t = g_res["market_cap"].dropna().astype(float)
        themes.append(
            {
                "theme": theme,
                "proposals": len(g),
                "days": n_days,
                "theme_days": int(g.groupby(["_file_asof"]).ngroups),
                "counts": verdict_counts(g),
                "in_bracket": n_in,
                "in_bracket_share": n_in / len(g),
                "median_cap": float(caps_t.median()) if len(caps_t) else float("nan"),
                # Contract §6 asks for "the same, per theme" as the pooled
                # distribution — deciles and the histogram, not just a median.
                # The table prints median + p10/p90; the full set rides in
                # --json so the promised item exists rather than being implied.
                "cap_distribution": _cap_distribution(caps_t)
                if len(caps_t) >= THEME_MIN_PROPOSALS
                else None,
                "structurally_outside": bool(
                    n_in == 0
                    and len(g) >= STRUCTURAL_MIN_PROPOSALS
                    and n_days >= STRUCTURAL_MIN_DAYS
                ),
            }
        )
    themes.sort(key=lambda t: (-t["proposals"], t["theme"]))
    report["per_theme"] = themes
    report["per_theme_reported_min_proposals"] = THEME_MIN_PROPOSALS

    # ---- POST-HOC: the same homogeneity statistic, across themes ---------
    # NOT pre-committed. The contract fixed a test across DAYS and a structural
    # flag for themes; #1002 also asks "is it theme-dependent", and a flag that
    # only fires on 0-in-bracket themes cannot answer that. Reported as post-hoc
    # and never quoted as a confirmation.
    theme_table = [
        [t["counts"][TOO_BIG], t["proposals"] - t["counts"][TOO_BIG] - t["counts"][NO_MCAP]]
        for t in themes
        if t["proposals"] - t["counts"][NO_MCAP] >= MIN_RESOLVED_FOR_TEST
    ]
    t_chi2 = t_perm = float("nan")
    if len(theme_table) >= 2:
        t_stat, _p, _dof2, _exp = stats.chi2_contingency(theme_table, correction=False)
        t_chi2 = float(t_stat)  # pyright: ignore[reportArgumentType]
        t_perm = _permutation_homogeneity_p(theme_table, observed=t_chi2)
    report["theme_homogeneity_post_hoc"] = {
        "themes_tested": len(theme_table),
        "chi2": t_chi2,
        "permutation_p": t_perm,
    }

    # ---- SECONDARY: theme-day yield --------------------------------------
    td = df.groupby(["_file_asof", "theme"], sort=True).agg(
        proposals=("ticker", "size"),
        in_bracket=("bracket_verdict", lambda s: int((s == IN_BRACKET).sum())),
    )
    report["theme_day_yield"] = {
        "theme_days": len(td),
        "theme_days_zero_in_bracket": int((td["in_bracket"] == 0).sum()),
        "median_proposals_per_theme_day": float(td["proposals"].median()),
        "median_in_bracket_per_theme_day": float(td["in_bracket"].median()),
    }

    # ---- SECONDARY: duplication ------------------------------------------
    top = df["ticker"].value_counts().head(15)
    ticker_days = df.groupby("ticker")["_file_asof"].nunique()
    recurring = set(ticker_days[ticker_days >= 3].index)
    report["duplication"] = {
        "proposals": len(df),
        "distinct_tickers": int(df["ticker"].nunique()),
        "distinct_ticker_days": int(df.groupby(["_file_asof", "ticker"]).ngroups),
        "top_tickers": [
            {
                "ticker": str(t),
                "proposals": int(c),
                "days": int(ticker_days.loc[str(t)]),
                "verdict": str(df.loc[df["ticker"] == t, "bracket_verdict"].mode().iat[0]),
            }
            for t, c in top.items()
        ],
        "share_of_proposals_from_tickers_on_3plus_days": float(df["ticker"].isin(recurring).mean()),
    }

    # ---- SECONDARY: no_mcap detail (which names fail to resolve) ----------
    no_mcap = df[df["bracket_verdict"] == NO_MCAP]
    report["no_mcap"] = {
        "proposals": len(no_mcap),
        "share_of_all": float(len(no_mcap) / len(df)),
        "distinct_tickers": int(no_mcap["ticker"].nunique()),
        "tickers": no_mcap["ticker"].value_counts().to_dict(),
        "days_with_any": int(no_mcap["_file_asof"].nunique()),
    }

    # ---- SECONDARY: stratified by mapper_config_version -------------------
    # The token is a long JSON blob; a short digest is what makes the strata
    # readable, and the full string stays in the JSON output. The token also
    # moves when a NESTED token moves (the channel assessor's), so the mapper's
    # own ``prompt_sha`` is pulled out beside it — that is the field that says
    # whether the proposing prompt itself changed.
    df = df.assign(_prompt_sha=df["mapper_config_version"].map(_prompt_sha))
    prompt_strata = []
    for sha, g in df.groupby("_prompt_sha", sort=True):
        g_res = g[g["bracket_verdict"].isin(RESOLVED_VERDICTS)]
        k = int((g_res["bracket_verdict"] == TOO_BIG).sum())
        days_g = sorted(g["_file_asof"].unique().tolist())
        prompt_strata.append(
            {
                "prompt_sha": str(sha),
                "days": days_g,
                "proposals": len(g),
                "proposals_per_day": len(g) / len(days_g),
                "resolved": len(g_res),
                "too_big_share": k / len(g_res) if len(g_res) else float("nan"),
                "in_bracket_share": float((g["bracket_verdict"] == IN_BRACKET).mean()),
            }
        )
    report["by_mapper_prompt_sha"] = prompt_strata

    strata = []
    for version, g in df.groupby("mapper_config_version", sort=True):
        g_res = g[g["bracket_verdict"].isin(RESOLVED_VERDICTS)]
        k = int((g_res["bracket_verdict"] == TOO_BIG).sum())
        strata.append(
            {
                "mapper_config_version": str(version),
                "digest": hashlib.sha256(str(version).encode()).hexdigest()[:12],
                "days": sorted(g["_file_asof"].unique().tolist()),
                "proposals": len(g),
                "resolved": len(g_res),
                "too_big_share": k / len(g_res) if len(g_res) else float("nan"),
                "in_bracket_share": float((g["bracket_verdict"] == IN_BRACKET).mean()),
            }
        )
    report["by_mapper_config_version"] = strata

    return report


def render(r: dict) -> str:
    out: list[str] = []
    ig = r["integrity"]
    out.append("=== INTEGRITY ===")
    out.append(
        f"{ig['rows']} proposals over {ig['days_present']} days "
        f"({ig['first_day']} .. {ig['last_day']}); missing days: {ig['missing_days'] or 'none'}"
    )
    out.append(
        f"asof/filename disagreements: {ig['asof_column_disagrees_with_filename']}; "
        f"null verdicts: {ig['null_verdicts']}; unknown verdicts: {ig['unknown_verdicts'] or 'none'}"
    )
    out.append(
        f"mcap null on a resolved verdict: {ig['mcap_null_but_resolved']}; "
        f"mcap present on no_mcap: {ig['mcap_present_but_no_mcap_verdict']}"
    )
    out.append(
        f"mapper_config_versions: {len(ig['mapper_config_versions'])}; "
        f"null group keys: {ig['null_group_keys']}"
    )

    p = r["primary"]
    out.append("")
    out.append("=== PRIMARY ===")
    out.append(
        f"too_big = {p['too_big']}/{p['resolved']} = {pct(p['share'])} "
        f"(exact 95% {pct(p['ci95_exact'][0])} .. {pct(p['ci95_exact'][1])})"
    )

    pooled = r["pooled_all_proposals"]
    out.append("")
    out.append(f"=== POOLED SPLIT (all {pooled['denominator']} proposals) ===")
    for v in VERDICTS:
        out.append(f"  {v:<12} {pooled['counts'][v]:>5}  {pct(pooled['shares'][v])}")

    out.append("")
    out.append("=== PER DAY ===")
    out.append(
        f"{'asof':<12}{'prop':>5}{'res':>5}{'thm':>5}{'big':>5}{'in':>5}{'small':>6}{'nomc':>6}   too_big share (95%)"
    )
    for d in r["per_day"]:
        c = d["counts"]
        mark = "  <- hypothesis day" if d["is_hypothesis_day"] else ""
        out.append(
            f"{d['asof']:<12}{d['proposals']:>5}{d['resolved']:>5}{d['themes']:>5}"
            f"{c[TOO_BIG]:>5}{c[IN_BRACKET]:>5}{c[TOO_SMALL]:>6}{c[NO_MCAP]:>6}   "
            f"{pct(d['too_big_share'])} ({pct(d['ci95_exact'][0])}..{pct(d['ci95_exact'][1])}){mark}"
        )

    for label, key in (
        ("HOMOGENEITY (contract §7/§8; hypothesis day excluded)", "homogeneity"),
        (
            "  sensitivity: same, WITH the hypothesis day",
            "homogeneity_sensitivity_with_hypothesis_day",
        ),
    ):
        h = r[key]
        out.append("")
        out.append(f"=== {label} ===")
        out.append(
            f"PRE-COMMITTED VERDICT: {h['precommitted_verdict'].upper()} — observed range "
            f"{pct(h['observed_range'][0])} .. {pct(h['observed_range'][1])}, max deviation "
            f"{h['max_abs_deviation_pp']:.1f} pp against a {h['band_pp']:.0f} pp band, "
            f"over {h['days_tested']} days"
        )
        out.append(
            f"  chi2={h['chi2']:.2f} p={h['p']:.4f}; min expected cell "
            f"{h['min_expected_cell']:.1f} -> {'reliable' if h['chi2_reliable'] else 'NOT reliable'}"
        )
        out.append(
            f"  post-hoc permutation p (20k reshuffles, NOT pre-committed) = "
            f"{h['permutation_p_post_hoc']:.4f} — a sensitivity, it does not override the verdict"
        )

    c = r["cap_distribution"]
    out.append("")
    out.append(f"=== CAP DISTRIBUTION (n={c['n']} resolved) ===")
    out.append(
        f"min ${c['min'] / 1e9:.3f}B  median ${c['median'] / 1e9:.2f}B  max ${c['max'] / 1e9:.0f}B"
    )
    out.append("  " + "  ".join(f"{k}=${v / 1e9:.2f}B" for k, v in c["deciles"].items()))
    out.append(
        "  histogram [1e_b, 1e_b+1): "
        + "  ".join(f"{k}:{v}" for k, v in c["log10_histogram_half_open"].items() if v)
        + f"  (uncounted: {c['uncounted_by_histogram']})"
    )

    out.append("")
    out.append(f"=== PER THEME (>= {r['per_theme_reported_min_proposals']} proposals) ===")
    out.append(
        f"{'theme':<34}{'prop':>5}{'days':>6}{'big':>5}{'in':>4}{'small':>6}{'nomc':>6}{'median cap':>13}  flag"
    )
    for t in r["per_theme"]:
        if t["proposals"] < r["per_theme_reported_min_proposals"]:
            continue
        cc = t["counts"]
        med = "n/a" if math.isnan(t["median_cap"]) else f"${t['median_cap'] / 1e9:.2f}B"
        flag = "STRUCTURALLY OUTSIDE" if t["structurally_outside"] else ""
        out.append(
            f"{t['theme'][:33]:<34}{t['proposals']:>5}{t['days']:>6}{cc[TOO_BIG]:>5}"
            f"{cc[IN_BRACKET]:>4}{cc[TOO_SMALL]:>6}{cc[NO_MCAP]:>6}{med:>13}  {flag}"
        )

    th = r["theme_homogeneity_post_hoc"]
    out.append("")
    out.append("=== THEME HOMOGENEITY (post-hoc, not pre-committed) ===")
    out.append(
        f"chi2={th['chi2']:.1f} over {th['themes_tested']} themes; "
        f"permutation p = {th['permutation_p']:.4f}"
    )

    y = r["theme_day_yield"]
    out.append("")
    out.append("=== THEME-DAY YIELD ===")
    out.append(
        f"{y['theme_days']} theme-days; {y['theme_days_zero_in_bracket']} returned zero in-bracket "
        f"({pct(y['theme_days_zero_in_bracket'] / y['theme_days'])}); "
        f"median {y['median_proposals_per_theme_day']:.0f} proposals -> "
        f"{y['median_in_bracket_per_theme_day']:.0f} in bracket"
    )

    d = r["duplication"]
    out.append("")
    out.append("=== DUPLICATION ===")
    out.append(
        f"{d['proposals']} proposals = {d['distinct_ticker_days']} ticker-days = "
        f"{d['distinct_tickers']} distinct tickers; "
        f"{pct(d['share_of_proposals_from_tickers_on_3plus_days'])} of proposals come from "
        f"tickers seen on 3+ days"
    )
    out.append(
        "  top: "
        + ", ".join(
            f"{t['ticker']}({t['proposals']}x/{t['days']}d,{t['verdict']})"
            for t in d["top_tickers"]
        )
    )

    nm = r["no_mcap"]
    out.append("")
    out.append("=== NO_MCAP (cap never resolved) ===")
    out.append(
        f"{nm['proposals']} proposals ({pct(nm['share_of_all'])}) over "
        f"{nm['distinct_tickers']} tickers on {nm['days_with_any']} days"
    )
    out.append("  " + ", ".join(f"{t}x{n}" for t, n in nm["tickers"].items()))

    out.append("")
    out.append("=== BY mapper prompt_sha (the proposing prompt itself) ===")
    for s in r["by_mapper_prompt_sha"]:
        out.append(
            f"  {s['prompt_sha']}  days={len(s['days']):>2} ({s['days'][0]}..{s['days'][-1]})  "
            f"prop={s['proposals']:>4} ({s['proposals_per_day']:.1f}/day)  "
            f"too_big={pct(s['too_big_share'])}  in_bracket={pct(s['in_bracket_share'])}"
        )

    out.append("")
    out.append("=== BY mapper_config_version ===")
    for s in r["by_mapper_config_version"]:
        out.append(
            f"  {s['digest']}  days={len(s['days']):>2} ({s['days'][0]}..{s['days'][-1]})  "
            f"prop={s['proposals']:>4}  too_big={pct(s['too_big_share'])}  "
            f"in_bracket={pct(s['in_bracket_share'])}"
        )

    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--funnel-dir", type=Path, default=FUNNEL_DIR)
    ap.add_argument(
        "--json", action="store_true", help="emit the report object instead of the table"
    )
    args = ap.parse_args()

    report = build_report(load(args.funnel_dir))
    print(json.dumps(report, indent=2, default=str) if args.json else render(report))


if __name__ == "__main__":
    main()
