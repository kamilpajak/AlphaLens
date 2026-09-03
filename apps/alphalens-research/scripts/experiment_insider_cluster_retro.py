"""Insider-cluster retrospective — pre-registered ESTIMATION stage (no verdict).

Spec (LOCKED 2026-09-03): docs/research/preregistration/params_insider_cluster_retro_2026_09.json
Design memo: docs/research/insider_cluster_retro_design_2026_09_03.md
Helpers (unit-tested): alphalens_research.diagnostics.insider_cluster_retro

Modes
  preflight  outcome-blind: build events, fetch/cache EDGAR acceptance times, count
             universe/pool coverage. No price is joined to an outcome.
  run        the estimation: matched controls, car_20/car_40, cluster-bootstrap CIs,
             planning rule, descriptives. Refuses to run unless --i-have-merged-the-lock
             is passed (the lock commit must be on main before the first run).

Data live where they live: Form-4 store synced from the VPS, prices/factors on the
analysis host (~/.alphalens). Outputs: ~/.alphalens/insider_cluster_retro_2026_09/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from alphalens_pipeline.data.alt_data.sec_edgar_client import get_default_sec_client
from alphalens_pipeline.data.alt_data.yfinance_cache import load_cached_histories
from alphalens_pipeline.paper.calendar import advance_trading_sessions, session_on_or_after
from alphalens_pipeline.scorers.cohen_malloy_classifier import classify_from_transaction_dates
from alphalens_research.diagnostics import insider_cluster_retro as icr
from alphalens_research.diagnostics.options_retro import cluster_ols

EX = "XNYS"
HOME = Path.home() / ".alphalens"
FORM4_ROOT = HOME / "form4_parquet"
PRICES_DIR = HOME / "prices"
PIT_ROOT = HOME / "pit_universe"
ACCEPT_CACHE = HOME / "edgar_acceptance"
OUT_DIR = HOME / "insider_cluster_retro_2026_09"
INFER_START, INFER_END = dt.date(2013, 1, 1), dt.date(2023, 12, 31)
BURNT_START, BURNT_END = dt.date(2024, 1, 1), dt.date(2026, 3, 31)
LATE_FILING_BDAYS = 10
PRE_EVENT_LOOKBACK = 20
ACCEPT_RE = re.compile(r"<ACCEPTANCE-DATETIME>\s*(\d{14})")
log = logging.getLogger("insider_cluster_retro")


# ---------------------------------------------------------------- data loading
def load_form4(years: range) -> pd.DataFrame:
    frames = []
    for y in years:
        p = FORM4_ROOT / f"transaction_year={y}" / "compacted.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p))
    df = pd.concat(frames, ignore_index=True)
    df["ticker"] = df["ticker"].astype(str).str.upper()
    return df


def load_pit_yamls() -> dict[str, set[str]]:
    import yaml

    out = {}
    for p in sorted(PIT_ROOT.glob("*.yaml")):
        out[p.stem] = set(yaml.safe_load(p.read_text()).get("tickers") or [])
    return out


def business_days_between(a: dt.date, b: dt.date) -> int:
    return int(np.busday_count(a, b))


def sessions_back(calendar_px: pd.DataFrame, day: dt.date, n: int) -> dt.date:
    """The session ``n`` trading days before ``day`` on the benchmark's index."""
    idx = calendar_px.index
    pos = idx.searchsorted(pd.Timestamp(day))
    return idx[max(pos - n, 0)].date()


# ------------------------------------------------------------ acceptance times
def fetch_acceptance(accession: str, cik: str, client) -> dt.datetime | None:
    """EDGAR <ACCEPTANCE-DATETIME> of a filing, cached per accession (raw header text persisted first)."""
    ACCEPT_CACHE.mkdir(parents=True, exist_ok=True)
    cache = ACCEPT_CACHE / f"{accession}.json"
    if cache.exists():
        v = json.loads(cache.read_text()).get("acceptance")
        return dt.datetime.strptime(v, "%Y%m%d%H%M%S") if v else None
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}.txt"
    try:
        text = client.get_text(url)[:4000]
    except Exception as exc:  # logged, treated as unknown (conservative arrival)
        log.warning("acceptance fetch failed %s: %s", accession, exc)
        cache.write_text(json.dumps({"acceptance": None, "error": str(exc)[:200]}))
        return None
    m = ACCEPT_RE.search(text)
    cache.write_text(json.dumps({"acceptance": m.group(1) if m else None, "header": text[:600]}))
    return dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S") if m else None


# -------------------------------------------------------------------- features
def rolling_features(px: pd.DataFrame) -> pd.DataFrame:
    c = px["close"].astype(float)
    dv = (px["close"] * px["volume"]).astype(float)
    lr = np.log(c / c.shift(1))
    return pd.DataFrame(
        {
            "ret_20d": c / c.shift(20) - 1.0,
            "ret_6m": c / c.shift(126) - 1.0,
            "vol_20d": lr.rolling(20).std() * np.sqrt(252),
            "log_dv_20d": np.log(dv.rolling(20).mean().replace(0, np.nan)),
        },
        index=px.index,
    )


def feature_at(feat: pd.DataFrame, day: dt.date) -> pd.Series | None:
    """Features as of the close BEFORE ``day`` (pre-event information only)."""
    ts = pd.Timestamp(day)
    pos = feat.index.searchsorted(ts)  # first index >= day
    if pos == 0:
        return None
    row = feat.iloc[pos - 1]
    return None if row.isna().any() else row


def build_events(form4: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    legs = icr.qualifying_legs(form4)
    ev = icr.detect_clusters(legs)
    ev3 = icr.detect_clusters(legs, window_sessions=5, min_insiders=3)
    ev["flag_3_in_5"] = ev.set_index(["ticker", "event_date"]).index.isin(
        ev3.set_index(["ticker", "event_date"]).index
    )
    ev["filing_lag_bdays"] = [
        business_days_between(t, f)
        for t, f in zip(ev.completing_transaction_date, ev.event_date, strict=True)
    ]
    ev["late_filing"] = ev.filing_lag_bdays > LATE_FILING_BDAYS
    ev["year"] = pd.to_datetime(ev.event_date).dt.year
    return legs, ev


def add_labels(ev: pd.DataFrame, legs: pd.DataFrame, form4: pd.DataFrame) -> pd.DataFrame:
    """Cohen-Malloy label of the completing cluster's buyers (all-opportunistic / mixed / all-routine / unclassified)."""
    hist = form4.groupby("reporting_owner_cik")["transaction_date"].apply(
        lambda s: pd.to_datetime(s).dt.date.tolist()
    )
    labels = []
    for r in ev.itertuples(index=False):
        hi = advance_trading_sessions(
            session_on_or_after(r.first_leg_date, EX), icr.CLUSTER_WINDOW_SESSIONS, EX
        )
        buyers = legs[
            (legs.ticker == r.ticker)
            & (legs.filed_date >= r.first_leg_date)
            & (legs.filed_date <= hi)
        ].reporting_owner_cik.unique()
        labs = {
            str(
                classify_from_transaction_dates(
                    hist.get(b, []), classification_year=r.event_date.year
                )
            )
            for b in buyers
        }
        labels.append(
            "all_opportunistic"
            if labs == {"opportunistic"}
            else "all_routine"
            if labs == {"routine"}
            else "unclassified"
            if labs == {"unclassified"}
            else "mixed"
        )
    return ev.assign(cm_label=labels)


# ------------------------------------------------------------------ main flow
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["preflight", "run"], required=True)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument(
        "--skip-acceptance", action="store_true", help="preflight only: do not fetch EDGAR headers"
    )
    ap.add_argument("--i-have-merged-the-lock", action="store_true")
    ap.add_argument("--n-boot", type=int, default=9999)
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr
    )
    args.out.mkdir(parents=True, exist_ok=True)

    form4 = load_form4(range(2006, 2027))
    legs, ev = build_events(form4)
    cik_of = form4.drop_duplicates("ticker").set_index("ticker")["issuer_cik"].to_dict()
    pit = load_pit_yamls()
    ev["in_pit_yaml"] = [
        t in pit.get(d.strftime("%Y-%m"), set())
        for t, d in zip(ev.ticker, ev.event_date, strict=True)
    ]
    priced = {p.stem for p in PRICES_DIR.glob("*.parquet")}
    ev["has_price"] = ev.ticker.isin(priced)
    infer = ev[(ev.event_date >= INFER_START) & (ev.event_date <= INFER_END)]
    print(
        f"events all {len(ev)}; inference window {len(infer)}; priced {int(infer.has_price.sum())}; in PIT yaml {int(infer.in_pit_yaml.sum())}; late filings {int(infer.late_filing.sum())}; 3-in-5 {int(infer.flag_3_in_5.sum())}"
    )
    print(
        "inference events per year (priced):",
        infer[infer.has_price].groupby("year").size().to_dict(),
    )

    # acceptance times (feature-only; cached)
    if not args.skip_acceptance:
        client = get_default_sec_client()
        acc = {}
        todo = infer[infer.has_price & ~infer.late_filing]
        for i, r in enumerate(todo.itertuples(index=False)):
            acc[r.completing_accession] = fetch_acceptance(
                r.completing_accession, cik_of.get(r.ticker, "0"), client
            )
            if i % 500 == 0:
                log.info("acceptance fetched %d/%d", i, len(todo))
        ev["acceptance_et"] = ev.completing_accession.map(acc)
        known = ev.acceptance_et.notna()
        pre_open = ev.acceptance_et.dropna().apply(lambda x: x.time() < icr.PRE_OPEN_CUTOFF_ET)
        print(
            f"acceptance known {int(known.sum())}/{len(todo)}; pre-open share {pre_open.mean():.2f}"
        )
    else:
        ev["acceptance_et"] = None
    ev["arrival"] = [
        icr.arrival_session(d, a if isinstance(a, dt.datetime) else None)
        for d, a in zip(ev.event_date, ev.acceptance_et, strict=True)
    ]
    ev.to_parquet(args.out / "events.parquet", index=False)

    if args.mode == "preflight":
        pool_tickers = sorted(set(form4.ticker) & priced)
        print(
            f"universe (store ∩ priced) tickers: {len(pool_tickers)}; PIT-yaml union priced: {len(set().union(*pit.values()) & priced)}"
        )
        return 0
    if not args.i_have_merged_the_lock:
        print(
            "refusing --mode run: pass --i-have-merged-the-lock after the lock commit is on main",
            file=sys.stderr,
        )
        return 2
    return run_estimation(ev, legs, form4, priced, pit, args)


def run_estimation(ev, legs, form4, priced, pit, args) -> int:
    ev = add_labels(ev, legs, form4)
    universe = sorted(set(form4.ticker) & priced)
    hist = load_cached_histories([*universe, "SPY", "IWM"], PRICES_DIR)
    feats = {t: rolling_features(h) for t, h in hist.items() if t not in ("SPY", "IWM")}
    spy, iwm = hist["SPY"], hist["IWM"]
    leg_dates = legs.groupby("ticker").filed_date.apply(lambda s: np.array(sorted(s)))

    def had_leg(ticker: str, start: dt.date, end: dt.date) -> bool:
        d = leg_dates.get(ticker)
        return d is not None and bool(((d >= start) & (d <= end)).any())

    rng = np.random.default_rng(1)
    infer = ev[
        (ev.event_date >= INFER_START)
        & (ev.event_date <= INFER_END)
        & ev.has_price
        & ~ev.late_filing
    ].copy()
    rows, ctrl_rows = [], []
    by_month: dict[str, pd.DataFrame] = {}
    for r in infer.itertuples(index=False):
        px = hist.get(r.ticker)
        if px is None:
            continue
        tf = feature_at(feats[r.ticker], r.arrival)
        car20 = icr.event_car(
            px, spy, arrival=r.arrival, horizon_sessions=icr.HORIZON_SESSIONS_PRIMARY
        )
        if tf is None or car20 is None:
            continue
        # pool for this arrival session (cached per month for speed: features as of the day before arrival)
        key = r.arrival.isoformat()
        if key not in by_month:
            recs = []
            for t in universe:
                f = feature_at(feats[t], r.arrival)
                if f is None:
                    continue
                recs.append({"ticker": t, **f.to_dict()})
            by_month[key] = pd.DataFrame(recs)
        pool = by_month[key]
        start20 = sessions_back(spy, r.event_date, PRE_EVENT_LOOKBACK)
        pool = pool[[not had_leg(t, start20, r.event_date) and t != r.ticker for t in pool.ticker]]
        if len(pool) < icr.CONTROLS_PER_EVENT:
            continue
        pool = pool.sample(
            frac=1.0, random_state=int(rng.integers(0, 2**31 - 1))
        )  # tie-break order
        picked = icr.match_controls(tf, pool)
        if picked.empty:
            continue
        cars = []
        for c in picked.itertuples(index=False):
            cc = icr.event_car(
                hist[c.ticker],
                spy,
                arrival=r.arrival,
                horizon_sessions=icr.HORIZON_SESSIONS_PRIMARY,
            )
            if cc is not None:
                cars.append(cc)
                ctrl_rows.append(
                    {
                        "event_ticker": r.ticker,
                        "arrival": r.arrival,
                        "control": c.ticker,
                        "car_20": cc,
                    }
                )
        if not cars:
            continue
        car40 = icr.event_car(
            px, spy, arrival=r.arrival, horizon_sessions=icr.HORIZON_SESSIONS_SECONDARY
        )
        car20_iwm = icr.event_car(
            px, iwm, arrival=r.arrival, horizon_sessions=icr.HORIZON_SESSIONS_PRIMARY
        )
        first_arr = icr.arrival_session(r.first_leg_date, None)
        car_first = icr.event_car(
            px, spy, arrival=first_arr, horizon_sessions=icr.HORIZON_SESSIONS_PRIMARY
        )
        # same-day close anchor sensitivity: entry at close of arrival session
        a_row = px.loc[pd.Timestamp(r.arrival)] if pd.Timestamp(r.arrival) in px.index else None
        rows.append(
            {
                **{
                    k: getattr(r, k)
                    for k in (
                        "ticker",
                        "event_date",
                        "arrival",
                        "first_leg_date",
                        "n_insiders",
                        "cluster_usd",
                        "flag_3_in_5",
                        "filing_lag_bdays",
                        "in_pit_yaml",
                        "cm_label",
                        "year",
                    )
                },
                "car_20": car20,
                "car_40": car40,
                "car_20_iwm": car20_iwm,
                "car_20_first_leg": car_first,
                "ctrl_mean_car_20": float(np.mean(cars)),
                "n_controls": len(cars),
                "d20": car20 - float(np.mean(cars)),
                "ret_20d": tf["ret_20d"],
                "ret_6m": tf["ret_6m"],
                "vol_20d": tf["vol_20d"],
                "log_dv_20d": tf["log_dv_20d"],
                "ctrl_ret_20d": float(picked.ret_20d.mean()),
                "ctrl_ret_6m": float(picked.ret_6m.mean()),
                "ctrl_vol_20d": float(picked.vol_20d.mean()),
                "ctrl_log_dv_20d": float(picked.log_dv_20d.mean()),
                "usd_over_dv": float(r.cluster_usd / np.exp(tf["log_dv_20d"])),
                "anchor_open": None if a_row is None else float(a_row["open"]),
                "anchor_close": None if a_row is None else float(a_row["close"]),
            }
        )
    T = pd.DataFrame(rows)
    C = pd.DataFrame(ctrl_rows)
    T.to_parquet(args.out / "treated.parquet", index=False)
    C.to_parquet(args.out / "controls.parquet", index=False)
    if T.empty:
        print("no treated rows — abort (outcome-blind defect)")
        return 3

    out: dict = {
        "n_events": len(T),
        "n_arrival_sessions": int(T.arrival.nunique()),
        "n_tickers": int(T.ticker.nunique()),
    }
    prim = icr.paired_difference_ci(T.d20.values, T.arrival.values, n_boot=args.n_boot, seed=0)
    out["primary_d20"] = prim
    out["planning_rule_build"] = icr.planning_rule(mean=prim["mean"], ci90_low=prim["ci90"][0])
    out["treated_car_20_mean"] = float(T.car_20.mean())
    out["control_car_20_mean"] = float(T.ctrl_mean_car_20.mean())
    # balance (SMD) treated vs matched controls
    out["smd_after"] = {
        v: float((T[v].mean() - T[f"ctrl_{v}"].mean()) / T[v].std(ddof=0)) for v in icr.MATCH_VARS
    }
    # regression adjustment on pooled rows (treated=1 / control=0), arrival clusters
    pooled_y = np.concatenate([T.car_20.values, C.car_20.values])
    pooled_tr = np.concatenate([np.ones(len(T)), np.zeros(len(C))])
    pooled_cl = np.concatenate([T.arrival.astype(str).values, C.arrival.astype(str).values])
    X = np.column_stack([np.ones(len(pooled_y)), pooled_tr])
    ols = cluster_ols(pooled_y, X, pooled_cl)
    out["regression_treated_coef"] = {
        "beta": float(ols.beta[1]),
        "se_cr2": float(ols.se_cr2[1]),
        "t_cr2": float(ols.t_cr2[1]),
        "n_clusters": int(ols.n_clusters),
    }
    # two-way (ticker) clustering CI as sensitivity
    out["d20_ci_ticker_clusters"] = icr.paired_difference_ci(
        T.d20.values, T.ticker.values, n_boot=args.n_boot, seed=0
    )

    # descriptives
    def desc(mask, name):
        s = T[mask]
        if len(s) >= 20:
            out[name] = {
                **icr.paired_difference_ci(s.d20.values, s.arrival.values, n_boot=2000, seed=0),
                "car_20_mean": float(s.car_20.mean()),
            }

    desc(T.year <= 2018, "sub_2013_2018")
    desc(T.year >= 2019, "sub_2019_2023")
    desc(T.cluster_usd >= icr.CLUSTER_MIN_USD_CHECK, "usd_floor_250k")
    desc(T.n_insiders == 2, "n_insiders_2")
    desc(T.n_insiders >= 3, "n_insiders_3plus")
    desc(T.flag_3_in_5, "flag_3_in_5")
    desc(T.in_pit_yaml, "pit_yaml_cut")
    for lab in T.cm_label.unique():
        desc(T.cm_label == lab, f"cm_{lab}")
    tc = T.sort_values("event_date").drop_duplicates("ticker")
    out["ticker_collapsed"] = icr.paired_difference_ci(
        tc.d20.values, tc.arrival.values, n_boot=2000, seed=0
    )
    out["car_40_mean"] = float(T.car_40.dropna().mean())
    out["car_20_iwm_mean"] = float(T.car_20_iwm.dropna().mean())
    out["car_20_first_leg_mean"] = float(T.car_20_first_leg.dropna().mean())
    # retail simulation: k-name cap, equal-dollar, first-come, fee model
    out["retail_sim"] = retail_sim(T, caps=(3, 5), seed=2)
    (args.out / "summary.json").write_text(json.dumps(out, indent=2, default=str))
    print(
        json.dumps(
            {
                k: out[k]
                for k in (
                    "n_events",
                    "n_arrival_sessions",
                    "n_tickers",
                    "primary_d20",
                    "planning_rule_build",
                    "treated_car_20_mean",
                    "control_car_20_mean",
                    "smd_after",
                    "regression_treated_coef",
                )
            },
            indent=2,
            default=str,
        )
    )
    return 0


def retail_sim(T: pd.DataFrame, *, caps=(3, 5), seed=2) -> dict:
    """Concentrated-book simulation: hold at most ``cap`` names, first-come, equal-dollar, hold 20 sessions."""
    res = {}
    T = T.sort_values(["arrival", "ticker"])
    for cap in caps:
        open_until: dict[str, dt.date] = {}
        taken = []
        for r in T.itertuples(index=False):
            open_until = {k: v for k, v in open_until.items() if v >= r.arrival}
            if len(open_until) >= cap:
                continue
            open_until[r.ticker] = advance_trading_sessions(
                r.arrival, icr.HORIZON_SESSIONS_PRIMARY, EX
            )
            taken.append(r.car_20 - icr.FEE_ROUND_TRIP)
        a = np.array(taken)
        res[f"cap_{cap}"] = {
            "n_trades": len(a),
            "mean_net": float(a.mean()),
            "median_net": float(np.median(a)),
            "p10": float(np.percentile(a, 10)),
            "p90": float(np.percentile(a, 90)),
            "share_negative": float((a < 0).mean()),
        }
    return res


if __name__ == "__main__":
    raise SystemExit(main())
