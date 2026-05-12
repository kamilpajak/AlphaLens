/Users/jacoren/Developer/Personal/AlphaLens/.venv/lib/python3.13/site-packages/simfin/load.py:154: FutureWarning: The argument 'date_parser' is deprecated and will be removed in a future version. Please use 'date_format' instead, or read your data in as 'object' dtype and then call 'to_datetime'.
  df = pd.read_csv(path, sep=';', header=0,
/Users/jacoren/Developer/Personal/AlphaLens/.venv/lib/python3.13/site-packages/simfin/load.py:154: FutureWarning: The argument 'date_parser' is deprecated and will be removed in a future version. Please use 'date_format' instead, or read your data in as 'object' dtype and then call 'to_datetime'.
  df = pd.read_csv(path, sep=';', header=0,
/Users/jacoren/Developer/Personal/AlphaLens/.venv/lib/python3.13/site-packages/simfin/load.py:154: FutureWarning: The argument 'date_parser' is deprecated and will be removed in a future version. Please use 'date_format' instead, or read your data in as 'object' dtype and then call 'to_datetime'.
  df = pd.read_csv(path, sep=';', header=0,
/Users/jacoren/Developer/Personal/AlphaLens/.venv/lib/python3.13/site-packages/simfin/load.py:154: FutureWarning: The argument 'date_parser' is deprecated and will be removed in a future version. Please use 'date_format' instead, or read your data in as 'object' dtype and then call 'to_datetime'.
  df = pd.read_csv(path, sep=';', header=0,
/Users/jacoren/Developer/Personal/AlphaLens/.venv/lib/python3.13/site-packages/simfin/load.py:154: FutureWarning: The argument 'date_parser' is deprecated and will be removed in a future version. Please use 'date_format' instead, or read your data in as 'object' dtype and then call 'to_datetime'.
  df = pd.read_csv(path, sep=';', header=0,
/Users/jacoren/Developer/Personal/AlphaLens/.venv/lib/python3.13/site-packages/simfin/load.py:154: FutureWarning: The argument 'date_parser' is deprecated and will be removed in a future version. Please use 'date_format' instead, or read your data in as 'object' dtype and then call 'to_datetime'.
  df = pd.read_csv(path, sep=';', header=0,
# SimFin coverage check — 2026-05-12

Cache dir: `/Users/jacoren/.alphalens/simfin_cache`
simfin version: `1.0.1`

## Step 1 — companies metadata (US market)

Dataset "us-companies" on disk (0 days old).
- Loading from disk ... Done!
- Total US companies: **6,580**
- Index: `Ticker` / columns: `['SimFinId', 'Company Name', 'IndustryId', 'ISIN', 'End of financial year (month)', 'Number Employees', 'Business Summary', 'Market', 'CIK', 'Main Currency']`

## Step 2 — R2000 active coverage

- R2000 active tickers in IWM snapshot: 1930
- Covered by SimFin: **1730 (89.6%)**
- Missing sample (first 15): `['AAMI', 'AARD', 'ABAT', 'ACH', 'ACNT', 'ADAM', 'ADRO', 'AEBI', 'AHRT', 'AIOT', 'AIRJ', 'AIRO', 'AISP', 'AKE', 'ALH']`

## Step 3 — known-delisted survivorship probe

- `LEHMQ` (Lehman Brothers, ~2008): **MISSING**
- `BSC` (Bear Stearns, ~2008): **MISSING**
- `SHLDQ` (Sears Holdings, ~2018): **found**
- `JCPNQ` (JCPenney, ~2020): **MISSING**
- `FTRCQ` (Frontier Communications, ~2020): **MISSING**
- `HTZGQ` (Hertz (pre-2020 bk), ~2020): **MISSING**
- `RSHCQ` (RadioShack, ~2015): **MISSING**
- `BGPIQ` (Borders Group, ~2011): **MISSING**
- `PIRRQ` (Pier 1 Imports, ~2020): **MISSING**
- `CHK` (Chesapeake Energy (2020 bk), ~2020): **MISSING**

**Delisted coverage: 1/10 (10%)**

## Step 4 — schema & DCF line item inventory


### `income` (annual variant)

Dataset "us-income-annual" on disk (0 days old).
- Loading from disk ... Done!
- Rows: 17,054
- date col `Report Date`: **present**
- date col `Publish Date`: **present**
- date col `Restated Date`: **present**
- columns (28):
    - `Ticker`
    - `SimFinId`
    - `Currency`
    - `Fiscal Year`
    - `Fiscal Period`
    - `Report Date`
    - `Publish Date`
    - `Restated Date`
    - `Shares (Basic)`
    - `Shares (Diluted)`
    - `Revenue`
    - `Cost of Revenue`
    - `Gross Profit`
    - `Operating Expenses`
    - `Selling, General & Administrative`
    - `Research & Development`
    - `Depreciation & Amortization`
    - `Operating Income (Loss)`
    - `Non-Operating Income (Loss)`
    - `Interest Expense, Net`
    - `Pretax Income (Loss), Adj.`
    - `Abnormal Gains (Losses)`
    - `Pretax Income (Loss)`
    - `Income Tax (Expense) Benefit, Net`
    - `Income (Loss) from Continuing Operations`
    - `Net Extraordinary Gains (Losses)`
    - `Net Income`
    - `Net Income (Common)`

### `balance` (annual variant)

Dataset "us-balance-annual" on disk (0 days old).
- Loading from disk ... Done!
- Rows: 17,054
- date col `Report Date`: **present**
- date col `Publish Date`: **present**
- date col `Restated Date`: **present**
- columns (30):
    - `Ticker`
    - `SimFinId`
    - `Currency`
    - `Fiscal Year`
    - `Fiscal Period`
    - `Report Date`
    - `Publish Date`
    - `Restated Date`
    - `Shares (Basic)`
    - `Shares (Diluted)`
    - `Cash, Cash Equivalents & Short Term Investments`
    - `Accounts & Notes Receivable`
    - `Inventories`
    - `Total Current Assets`
    - `Property, Plant & Equipment, Net`
    - `Long Term Investments & Receivables`
    - `Other Long Term Assets`
    - `Total Noncurrent Assets`
    - `Total Assets`
    - `Payables & Accruals`
    - `Short Term Debt`
    - `Total Current Liabilities`
    - `Long Term Debt`
    - `Total Noncurrent Liabilities`
    - `Total Liabilities`
    - `Share Capital & Additional Paid-In Capital`
    - `Treasury Stock`
    - `Retained Earnings`
    - `Total Equity`
    - `Total Liabilities & Equity`

### `cashflow` (annual variant)

Dataset "us-cashflow-annual" on disk (0 days old).
- Loading from disk ... Done!
- Rows: 17,053
- date col `Report Date`: **present**
- date col `Publish Date`: **present**
- date col `Restated Date`: **present**
- columns (28):
    - `Ticker`
    - `SimFinId`
    - `Currency`
    - `Fiscal Year`
    - `Fiscal Period`
    - `Report Date`
    - `Publish Date`
    - `Restated Date`
    - `Shares (Basic)`
    - `Shares (Diluted)`
    - `Net Income/Starting Line`
    - `Depreciation & Amortization`
    - `Non-Cash Items`
    - `Change in Working Capital`
    - `Change in Accounts Receivable`
    - `Change in Inventories`
    - `Change in Accounts Payable`
    - `Change in Other`
    - `Net Cash from Operating Activities`
    - `Change in Fixed Assets & Intangibles`
    - `Net Change in Long Term Investment`
    - `Net Cash from Acquisitions & Divestitures`
    - `Net Cash from Investing Activities`
    - `Dividends Paid`
    - `Cash from (Repayment of) Debt`
    - `Cash from (Repurchase of) Equity`
    - `Net Cash from Financing Activities`
    - `Net Change in Cash`

## Step 5 — PIT lag spot-check (AAPL annual reports)

Dataset "us-income-annual" on disk (0 days old).
- Loading from disk ... Done!
AAPL last 5 annual reports — date integrity:

| REPORT_DATE | PUBLISH_DATE | RESTATED_DATE | publish_lag_days |
|---|---|---|---|
| 2024-09-30 | 2024-11-01 | 2025-10-31 | 32 |
| 2023-09-30 | 2023-11-03 | 2025-10-31 | 34 |
| 2022-09-30 | 2022-10-28 | 2024-11-01 | 28 |
| 2021-09-30 | 2021-10-29 | 2023-11-03 | 29 |
| 2020-09-30 | 2020-10-30 | 2022-10-28 | 30 |

## Step 6 — historical depth probe

Dataset "us-income-annual" on disk (0 days old).
- Loading from disk ... Done!
Per-ticker REPORT_DATE range (long-lived large-caps):

| Ticker | min REPORT_DATE | max REPORT_DATE | annual rows |
|---|---|---|---|
| AAPL | 2020-09-30 | 2024-09-30 | 5 |
| MSFT | 2020-06-30 | 2024-06-30 | 5 |
| GE | 2020-12-31 | 2024-12-31 | 5 |
| IBM | 2020-12-31 | 2024-12-31 | 5 |
| JPM | MISSING | MISSING | 0 |
| XOM | 2020-12-31 | 2024-12-31 | 5 |
| WFC | MISSING | MISSING | 0 |
| F | 2020-12-31 | 2024-12-31 | 5 |
| T | 2020-12-31 | 2024-12-31 | 5 |
| KO | 2020-12-31 | 2024-12-31 | 5 |

Dataset-wide annual-row-per-ticker distribution:

- median: **5** years
- p25 / p75: 3 / 5 years
- max: 5 years
- earliest REPORT_DATE in entire dataset: **2020-06-30**
- latest REPORT_DATE: 2025-04-30

## Verdict gate

- Active R2000 coverage: **89.6%** (gate: >= 90%)
- Delisted sample coverage: **1/10** (gate: >= 5/10)

(Schema + DCF columns: inspect Step 4 output above.)
(Historical depth: inspect Step 6 above — earliest date in dataset is the binding constraint for 2007-2026 backtest.)

---

## Verdict & decision (added post-run)

**SimFin free tier UNUSABLE for AlphaLens primary fundamentals source.**

Findings:

| Criterion | Gate | Observed | Pass? |
|---|---|---|---|
| Active R2000 coverage | ≥ 90% | 89.6% | borderline (acceptable in isolation) |
| Delisted survivorship | ≥ 5/10 known names | **1/10** | **FAIL** |
| Schema (PUBLISH_DATE etc.) | all three date cols | all present | PASS |
| DCF line items | capex + OCF + WC | all present | PASS |
| Historical depth (binding constraint) | back to 2007–2010 | **earliest = 2020-06-30, median 5y/ticker** | **FAIL** |

The historical depth finding is the absolute deal-breaker: free tier provides only 5 years of annual data. JPM and WFC entirely absent from the income dataset. Even a forward-only design starting 2020 would have ~5 years history + survivorship bias, which is below phase-robust audit thresholds (typically need IS/OOS/FL = 3y × 3 = 9y minimum).

## Options forward

### Option A — Upgrade SimFin to paid tier
- Verify actual cost of full-history tier (Perplexity said ~$15-20/mo but that may apply to only bulk rate-limit removal, not historical depth)
- Realistically expect $60-100/mo for full history per SimFin pricing page conventions
- Schema + delisted issues remain even if history extends
- **Not recommended**: spending money on a vendor whose delisted coverage is structurally limited (community-review-driven, focuses on active companies)

### Option B — SEC EDGAR XBRL (RECOMMENDED)
- **Already partially built**: `alphalens/data/fundamentals/edgar_companyfacts.py` (PIT-clean TTM ROE pipeline, peer-reviewed 2026-04-29)
- **Already cached**: 2,784 companyfacts JSON files in `~/.alphalens/companyfacts/`
- Coverage: ALL US public companies that filed XBRL post-2009, including delisted (`companyfacts/{CIK}.json` survives delisting; SEC keeps the record)
- Depth: 2009 → present (covers 2010-2026 window cleanly; pre-2009 is HTML-only)
- PIT semantics: native — every fact has a `filed` date; existing module already filters `filed <= asof`
- Cost: FREE
- Effort: extend ROE helper to expose: `revenue_ttm`, `net_income_ttm`, `op_cashflow_ttm`, `capex_ttm`, `working_capital`, `total_debt`, `book_equity`, `shares_outstanding`. Reuse the matched-pair + fiscal-drift logic. Roughly 1-3 days of work given the foundation exists.
- Known cost: XBRL tagging variance per Perplexity ~15-25% on tail metrics. For headline line items (Revenue, NI, OCF, Capex) accuracy is much higher — these are core US-GAAP tags with strong adoption.

### Option C — Pivot away from PIT fundamentals experiments
- Drop DCF / value-factor direction entirely
- Stay with options/insider data classes already in production
- **Not recommended**: closing yet another door without trying contradicts project doctrine (CLAUDE.md: "keep searching screeners — never close the door")

## Recommendation

**Pursue Option B (SEC EDGAR XBRL extension).** Concrete next steps:

1. Inventory what tags `edgar_companyfacts.py` already extracts vs what DCF requires
2. Extend module to expose 7-9 PIT line-items (revenue, NI, OCF, capex, WC, debt, book equity, shares)
3. Sanity-check on same 10 delisted tickers from this report — expect ≥ 8/10 coverage (SEC EDGAR retains delisted filings post-2009)
4. Sanity-check historical depth on 10 long-lived large caps — expect 2009-2025 full coverage
5. Once data layer ready, return to design-memo phase for reverse-DCF or DCF×insider experiment

Free tier SimFin retained as **lightweight cross-check secondary** (high-quality active-company data, useful for spot validating EDGAR extractions on 2020-2024 overlap). Cache `~/.alphalens/simfin_cache/` is ~few hundred MB but useful.
