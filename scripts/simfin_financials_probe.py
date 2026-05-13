#!/usr/bin/env python3
"""SimFin financials sector probe.

JPM and WFC were missing from the income dataset in the baseline coverage
check. This probes whether SimFin systemically lacks banks / financials, or
whether it's per-ticker edge cases. Tests:
  1. Are the tickers in df_companies metadata?
  2. Are they in df_income annual?
  3. What sector/industry is assigned where available?

Prints markdown to stdout.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# Mix of big US banks, IB, asset managers, insurers, payments
FINANCIALS_PROBE = [
    ("JPM", "JPMorgan Chase", "money-center bank"),
    ("WFC", "Wells Fargo", "money-center bank"),
    ("BAC", "Bank of America", "money-center bank"),
    ("C", "Citigroup", "money-center bank"),
    ("GS", "Goldman Sachs", "investment bank"),
    ("MS", "Morgan Stanley", "investment bank"),
    ("USB", "US Bancorp", "regional bank"),
    ("PNC", "PNC Financial", "regional bank"),
    ("BLK", "BlackRock", "asset manager"),
    ("AXP", "American Express", "payments"),
    ("V", "Visa", "payments — non-bank"),
    ("MA", "Mastercard", "payments — non-bank"),
    ("BRK-B", "Berkshire Hathaway B", "insurance/holding"),
    ("AIG", "AIG", "insurance"),
    ("MET", "MetLife", "insurance"),
]


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    api_key = os.environ.get("SIMFIN_API_KEY")
    if not api_key:
        print("ERROR: SIMFIN_API_KEY missing", file=sys.stderr)
        return 1

    import simfin as sf
    from simfin.names import REPORT_DATE, TICKER

    cache_dir = Path.home() / ".alphalens" / "simfin_cache"
    sf.set_api_key(api_key)
    sf.set_data_dir(str(cache_dir))

    print(f"# SimFin financials sector probe — {date.today().isoformat()}\n")

    df_companies = sf.load_companies(market="us")
    df_income = sf.load(
        dataset="income", variant="annual", market="us", index=[TICKER, REPORT_DATE]
    )

    companies_idx = set(df_companies.index.astype(str))
    income_tickers = set(df_income.index.get_level_values(0).astype(str))

    # Try to surface industry/sector from companies metadata
    industries_path = cache_dir / "us-industries.csv"
    df_industries = None
    try:
        df_industries = sf.load_industries()
    except Exception as exc:
        print(f"_industries lookup failed: `{exc}`_\n")

    print("## Probe results\n")
    print("| Ticker | Name | Type | In `companies`? | In `income`? | Industry |")
    print("|---|---|---|---|---|---|")
    in_companies_count = 0
    in_income_count = 0
    for tkr, name, kind in FINANCIALS_PROBE:
        in_comp = tkr in companies_idx
        in_inc = tkr in income_tickers
        if in_comp:
            in_companies_count += 1
        if in_inc:
            in_income_count += 1
        industry_str = ""
        if in_comp and df_industries is not None:
            try:
                row = df_companies.loc[tkr]
                indus_id = row.get("IndustryId") if hasattr(row, "get") else None
                if indus_id is not None and indus_id in df_industries.index:
                    irow = df_industries.loc[indus_id]
                    industry_str = f"{irow.get('Sector', '')} / {irow.get('Industry', '')}"
            except Exception:
                pass
        print(
            f"| `{tkr}` | {name} | {kind} | {'✅' if in_comp else '❌'} | {'✅' if in_inc else '❌'} | {industry_str} |"
        )

    print(
        f"\n**Summary: {in_companies_count}/{len(FINANCIALS_PROBE)} in `companies`, "
        f"{in_income_count}/{len(FINANCIALS_PROBE)} in `income` annual**\n"
    )

    # Sector-wide aggregate — count all tickers in companies belonging to financials sectors
    if df_industries is not None:
        print("## Sector aggregate (full us-companies)\n")
        # Try to find financials-related sectors
        try:
            sectors = (
                df_industries["Sector"].dropna().unique()
                if "Sector" in df_industries.columns
                else []
            )
            fin_sectors = [
                s for s in sectors if "Financ" in str(s) or "Bank" in str(s) or "Insur" in str(s)
            ]
            print(f"Detected financials-related sectors: `{fin_sectors}`\n")
            for fs in fin_sectors:
                indus_ids = df_industries[df_industries["Sector"] == fs].index
                # Count companies with that IndustryId
                if "IndustryId" in df_companies.columns:
                    n = df_companies["IndustryId"].isin(indus_ids).sum()
                    # And count of those present in income
                    matching_tickers = set(
                        df_companies[df_companies["IndustryId"].isin(indus_ids)].index.astype(str)
                    )
                    n_in_income = len(matching_tickers & income_tickers)
                    pct = 100 * n_in_income / n if n else 0
                    print(
                        f"- **{fs}**: {n} companies in metadata, {n_in_income} ({pct:.1f}%) have income data"
                    )
        except Exception as exc:
            print(f"_sector aggregate failed: `{exc}`_")

    return 0


if __name__ == "__main__":
    sys.exit(main())
