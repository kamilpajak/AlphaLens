#!/usr/bin/env python3
"""SimFin coverage + schema sanity check (diagnostic-only, no Bonferroni cost).

Validates whether SimFin free tier delivers PIT fundamentals suitable for
R2000 DCF / value-factor experiments before committing to a hypothesis.

Gate criteria:
- Active R2000 coverage >= 90% → coverage OK
- Delisted sample (10 tickers) >= 5/10 → survivorship bias tolerable
- PUBLISH_DATE / RESTATED_DATE / REPORT_DATE all present → PIT schema OK
- Cashflow has capex + OCF, Balance has current_assets + current_liabilities → DCF inputs OK

Prints a markdown report to stdout. Pipe to docs/research/simfin_coverage_check_*.md.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from alphalens.data.alt_data.russell_universe import load_iwm_current  # noqa: E402

# Known R2000 / small-cap delistings spanning 2008-2020. Survivorship probe.
KNOWN_DELISTED_SAMPLE = [
    ("LEHMQ", "Lehman Brothers", 2008),
    ("BSC", "Bear Stearns", 2008),
    ("SHLDQ", "Sears Holdings", 2018),
    ("JCPNQ", "JCPenney", 2020),
    ("FTRCQ", "Frontier Communications", 2020),
    ("HTZGQ", "Hertz (pre-2020 bk)", 2020),
    ("RSHCQ", "RadioShack", 2015),
    ("BGPIQ", "Borders Group", 2011),
    ("PIRRQ", "Pier 1 Imports", 2020),
    ("CHK", "Chesapeake Energy (2020 bk)", 2020),
]


def section(title: str) -> None:
    print(f"\n## {title}\n")


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    api_key = os.environ.get("SIMFIN_API_KEY")
    if not api_key:
        print("ERROR: SIMFIN_API_KEY missing from .env", file=sys.stderr)
        return 1

    import simfin as sf
    from simfin.names import PUBLISH_DATE, REPORT_DATE, RESTATED_DATE, TICKER

    cache_dir = Path.home() / ".alphalens" / "simfin_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    sf.set_api_key(api_key)
    sf.set_data_dir(str(cache_dir))

    print(f"# SimFin coverage check — {date.today().isoformat()}")
    print(f"\nCache dir: `{cache_dir}`  ")
    print(f"simfin version: `{sf.__version__}`")

    # --- Step 1: companies metadata ---
    section("Step 1 — companies metadata (US market)")
    df_companies = sf.load_companies(market="us")
    print(f"- Total US companies: **{len(df_companies):,}**")
    print(f"- Index: `{df_companies.index.name}` / columns: `{list(df_companies.columns)}`")
    if df_companies.index.name == TICKER:
        all_tickers = set(df_companies.index.astype(str))
    else:
        all_tickers = set(df_companies[TICKER].astype(str))

    # --- Step 2: R2000 active coverage ---
    section("Step 2 — R2000 active coverage")
    iwm_path = REPO_ROOT / "alphalens" / "data" / "alt_data" / "data" / "iwm_current.yaml"
    r2000_tickers = set(load_iwm_current(iwm_path))
    covered = r2000_tickers & all_tickers
    missing = r2000_tickers - all_tickers
    coverage_pct = 100 * len(covered) / len(r2000_tickers)
    print(f"- R2000 active tickers in IWM snapshot: {len(r2000_tickers)}")
    print(f"- Covered by SimFin: **{len(covered)} ({coverage_pct:.1f}%)**")
    print(f"- Missing sample (first 15): `{sorted(missing)[:15]}`")

    # --- Step 3: known-delisted coverage ---
    section("Step 3 — known-delisted survivorship probe")
    found_count = 0
    for tkr, name, year in KNOWN_DELISTED_SAMPLE:
        present = tkr in all_tickers
        marker = "found" if present else "MISSING"
        print(f"- `{tkr}` ({name}, ~{year}): **{marker}**")
        if present:
            found_count += 1
    delisted_pct = 100 * found_count / len(KNOWN_DELISTED_SAMPLE)
    print(
        f"\n**Delisted coverage: {found_count}/{len(KNOWN_DELISTED_SAMPLE)} ({delisted_pct:.0f}%)**"
    )

    # --- Step 4: schema + DCF column inventory ---
    section("Step 4 — schema & DCF line item inventory")
    for dataset_name in ("income", "balance", "cashflow"):
        print(f"\n### `{dataset_name}` (annual variant)\n")
        try:
            df = sf.load(dataset=dataset_name, variant="annual", market="us")
        except Exception as exc:
            print(f"- ERROR loading `{dataset_name}`: `{exc}`")
            continue
        print(f"- Rows: {len(df):,}")
        date_cols_in_df = list(df.columns) + list(df.index.names or [])
        for col in (REPORT_DATE, PUBLISH_DATE, RESTATED_DATE):
            present = col in date_cols_in_df
            print(f"- date col `{col}`: **{'present' if present else 'MISSING'}**")
        print(f"- columns ({len(df.columns)}):")
        for c in df.columns:
            print(f"    - `{c}`")

    # --- Step 5: PIT lag spot-check on a single well-known ticker ---
    section("Step 5 — PIT lag spot-check (AAPL annual reports)")
    try:
        df_inc = sf.load(
            dataset="income",
            variant="annual",
            market="us",
            index=[TICKER, REPORT_DATE],
            parse_dates=[REPORT_DATE, PUBLISH_DATE, RESTATED_DATE],
        )
        aapl = df_inc.loc["AAPL"].sort_index(ascending=False).head(5)
        cols = [c for c in (PUBLISH_DATE, RESTATED_DATE) if c in aapl.columns]
        print("AAPL last 5 annual reports — date integrity:\n")
        print("| REPORT_DATE | PUBLISH_DATE | RESTATED_DATE | publish_lag_days |")
        print("|---|---|---|---|")
        for idx, row in aapl[cols].iterrows():
            pub = row.get(PUBLISH_DATE)
            rest = row.get(RESTATED_DATE)
            lag = (pub - idx).days if pub is not None else None
            print(
                f"| {idx.date()} | {pub.date() if pub is not None else 'NA'} | {rest.date() if rest is not None else 'NA'} | {lag} |"
            )
    except Exception as exc:
        print(f"- ERROR: `{exc}`")

    # --- Step 6: historical depth probe ---
    section("Step 6 — historical depth probe")
    DEPTH_PROBE = ["AAPL", "MSFT", "GE", "IBM", "JPM", "XOM", "WFC", "F", "T", "KO"]
    try:
        df_inc2 = sf.load(
            dataset="income",
            variant="annual",
            market="us",
            index=[TICKER, REPORT_DATE],
            parse_dates=[REPORT_DATE, PUBLISH_DATE, RESTATED_DATE],
        )
        print("Per-ticker REPORT_DATE range (long-lived large-caps):\n")
        print("| Ticker | min REPORT_DATE | max REPORT_DATE | annual rows |")
        print("|---|---|---|---|")
        for tkr in DEPTH_PROBE:
            if tkr not in df_inc2.index.get_level_values(0):
                print(f"| {tkr} | MISSING | MISSING | 0 |")
                continue
            sub = df_inc2.loc[tkr]
            dmin = sub.index.min().date()
            dmax = sub.index.max().date()
            n = len(sub)
            print(f"| {tkr} | {dmin} | {dmax} | {n} |")

        # Distribution summary across full dataset
        per_ticker_n = df_inc2.groupby(level=0).size()
        print("\nDataset-wide annual-row-per-ticker distribution:\n")
        print(f"- median: **{int(per_ticker_n.median())}** years")
        print(
            f"- p25 / p75: {int(per_ticker_n.quantile(0.25))} / {int(per_ticker_n.quantile(0.75))} years"
        )
        print(f"- max: {int(per_ticker_n.max())} years")
        date_min = df_inc2.index.get_level_values(REPORT_DATE).min().date()
        date_max = df_inc2.index.get_level_values(REPORT_DATE).max().date()
        print(f"- earliest REPORT_DATE in entire dataset: **{date_min}**")
        print(f"- latest REPORT_DATE: {date_max}")
    except Exception as exc:
        print(f"- ERROR: `{exc}`")

    # --- Verdict ---
    section("Verdict gate")
    print(f"- Active R2000 coverage: **{coverage_pct:.1f}%** (gate: >= 90%)")
    print(
        f"- Delisted sample coverage: **{found_count}/{len(KNOWN_DELISTED_SAMPLE)}** (gate: >= 5/10)"
    )
    print("\n(Schema + DCF columns: inspect Step 4 output above.)")
    print(
        "(Historical depth: inspect Step 6 above — earliest date in dataset is the binding constraint for 2007-2026 backtest.)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
