# SimFin coverage check — 2026-05-12

Cache dir: `/Users/jacoren/.alphalens/simfin_cache`
simfin version: `1.0.1`

## Step 1 — companies metadata (US market)

Dataset "us-companies" not on disk.
- Downloading ... 1.1%- Downloading ... 2.2%- Downloading ... 3.2%- Downloading ... 4.3%- Downloading ... 5.4%- Downloading ... 6.5%- Downloading ... 7.6%- Downloading ... 8.6%- Downloading ... 9.7%- Downloading ... 10.8%- Downloading ... 11.9%- Downloading ... 13.0%- Downloading ... 14.1%- Downloading ... 15.1%- Downloading ... 16.2%- Downloading ... 17.3%- Downloading ... 18.4%- Downloading ... 19.5%- Downloading ... 20.5%- Downloading ... 21.6%- Downloading ... 22.7%- Downloading ... 23.8%- Downloading ... 24.9%- Downloading ... 25.9%- Downloading ... 27.0%- Downloading ... 28.1%- Downloading ... 29.2%- Downloading ... 30.3%- Downloading ... 31.3%- Downloading ... 32.4%- Downloading ... 33.5%- Downloading ... 34.6%- Downloading ... 35.7%- Downloading ... 36.8%- Downloading ... 37.8%- Downloading ... 38.9%- Downloading ... 40.0%- Downloading ... 41.1%- Downloading ... 42.2%- Downloading ... 43.2%- Downloading ... 44.3%- Downloading ... 45.4%- Downloading ... 46.5%- Downloading ... 47.6%- Downloading ... 48.6%- Downloading ... 49.7%- Downloading ... 50.8%- Downloading ... 51.9%- Downloading ... 53.0%- Downloading ... 54.0%- Downloading ... 55.1%- Downloading ... 56.2%- Downloading ... 57.3%- Downloading ... 58.4%- Downloading ... 59.5%- Downloading ... 60.5%- Downloading ... 61.6%- Downloading ... 62.7%- Downloading ... 63.8%- Downloading ... 64.9%- Downloading ... 65.9%- Downloading ... 67.0%- Downloading ... 68.1%- Downloading ... 69.2%- Downloading ... 70.3%- Downloading ... 71.3%- Downloading ... 72.4%- Downloading ... 73.5%- Downloading ... 74.6%- Downloading ... 75.7%- Downloading ... 76.7%- Downloading ... 77.8%- Downloading ... 78.9%- Downloading ... 80.0%- Downloading ... 81.1%- Downloading ... 82.2%- Downloading ... 83.2%- Downloading ... 84.3%- Downloading ... 85.4%- Downloading ... 86.5%- Downloading ... 87.6%- Downloading ... 88.6%- Downloading ... 89.7%- Downloading ... 90.8%- Downloading ... 91.9%- Downloading ... 93.0%- Downloading ... 94.0%- Downloading ... 95.1%- Downloading ... 96.2%- Downloading ... 97.3%- Downloading ... 98.4%- Downloading ... 99.5%- Downloading ... 100.0%/Users/jacoren/Developer/Personal/AlphaLens/.venv/lib/python3.13/site-packages/simfin/load.py:154: FutureWarning: The argument 'date_parser' is deprecated and will be removed in a future version. Please use 'date_format' instead, or read your data in as 'object' dtype and then call 'to_datetime'.
  df = pd.read_csv(path, sep=';', header=0,

- Extracting zip-file ... Done!
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

Dataset "us-income-annual" not on disk.
- Downloading ... 0.8%- Downloading ... 1.6%- Downloading ... 2.3%- Downloading ... 3.1%- Downloading ... 3.9%- Downloading ... 4.7%- Downloading ... 5.5%- Downloading ... 6.2%- Downloading ... 7.0%- Downloading ... 7.8%- Downloading ... 8.6%- Downloading ... 9.4%- Downloading ... 10.1%- Downloading ... 10.9%- Downloading ... 11.7%- Downloading ... 12.5%- Downloading ... 13.3%- Downloading ... 14.1%- Downloading ... 14.8%- Downloading ... 15.6%- Downloading ... 16.4%- Downloading ... 17.2%- Downloading ... 18.0%- Downloading ... 18.7%- Downloading ... 19.5%- Downloading ... 20.3%- Downloading ... 21.1%- Downloading ... 21.9%- Downloading ... 22.6%- Downloading ... 23.4%- Downloading ... 24.2%- Downloading ... 25.0%- Downloading ... 25.8%- Downloading ... 26.5%- Downloading ... 27.3%- Downloading ... 28.1%- Downloading ... 28.9%- Downloading ... 29.7%- Downloading ... 30.4%- Downloading ... 31.2%- Downloading ... 32.0%- Downloading ... 32.8%- Downloading ... 33.6%- Downloading ... 34.4%- Downloading ... 35.1%- Downloading ... 35.9%- Downloading ... 36.7%- Downloading ... 37.5%- Downloading ... 38.3%- Downloading ... 39.0%- Downloading ... 39.8%- Downloading ... 40.6%- Downloading ... 41.4%- Downloading ... 42.2%- Downloading ... 42.9%- Downloading ... 43.7%- Downloading ... 44.5%- Downloading ... 45.3%- Downloading ... 46.1%- Downloading ... 46.8%- Downloading ... 47.6%- Downloading ... 48.4%- Downloading ... 49.2%- Downloading ... 50.0%- Downloading ... 50.7%- Downloading ... 51.5%- Downloading ... 52.3%- Downloading ... 53.1%- Downloading ... 53.9%- Downloading ... 54.6%- Downloading ... 55.4%- Downloading ... 56.2%- Downloading ... 57.0%- Downloading ... 57.8%- Downloading ... 58.6%- Downloading ... 59.3%- Downloading ... 60.1%- Downloading ... 60.9%- Downloading ... 61.7%- Downloading ... 62.5%- Downloading ... 63.2%- Downloading ... 64.0%- Downloading ... 64.8%- Downloading ... 65.6%- Downloading ... 66.4%- Downloading ... 67.1%- Downloading ... 67.9%- Downloading ... 68.7%- Downloading ... 69.5%- Downloading ... 70.3%- Downloading ... 71.0%- Downloading ... 71.8%- Downloading ... 72.6%- Downloading ... 73.4%- Downloading ... 74.2%- Downloading ... 74.9%- Downloading ... 75.7%- Downloading ... 76.5%- Downloading ... 77.3%- Downloading ... 78.1%- Downloading ... 78.9%- Downloading ... 79.6%- Downloading ... 80.4%- Downloading ... 81.2%- Downloading ... 82.0%- Downloading ... 82.8%- Downloading ... 83.5%- Downloading ... 84.3%- Downloading ... 85.1%- Downloading ... 85.9%- Downloading ... 86.7%- Downloading ... 87.4%- Downloading ... 88.2%- Downloading ... 89.0%- Downloading ... 89.8%- Downloading ... 90.6%- Downloading ... 91.3%- Downloading ... 92.1%- Downloading ... 92.9%- Downloading ... 93.7%- Downloading ... 94.5%- Downloading ... 95.2%- Downloading ... 96.0%- Downloading ... 96.8%- Downloading ... 97.6%- Downloading ... 98.4%- Downloading ... 99.1%- Downloading ... 99.9%- Downloading ... 100.0%/Users/jacoren/Developer/Personal/AlphaLens/.venv/lib/python3.13/site-packages/simfin/load.py:154: FutureWarning: The argument 'date_parser' is deprecated and will be removed in a future version. Please use 'date_format' instead, or read your data in as 'object' dtype and then call 'to_datetime'.
  df = pd.read_csv(path, sep=';', header=0,

- Extracting zip-file ... Done!
- Loading from disk ... Done!
- Rows: 31,636
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

Dataset "us-balance-annual" not on disk.
- Downloading ... 0.6%- Downloading ... 1.3%- Downloading ... 1.9%- Downloading ... 2.6%- Downloading ... 3.2%- Downloading ... 3.8%- Downloading ... 4.5%- Downloading ... 5.1%- Downloading ... 5.8%- Downloading ... 6.4%- Downloading ... 7.1%- Downloading ... 7.7%- Downloading ... 8.3%- Downloading ... 9.0%- Downloading ... 9.6%- Downloading ... 10.3%- Downloading ... 10.9%- Downloading ... 11.5%- Downloading ... 12.2%- Downloading ... 12.8%- Downloading ... 13.5%- Downloading ... 14.1%- Downloading ... 14.7%- Downloading ... 15.4%- Downloading ... 16.0%- Downloading ... 16.7%- Downloading ... 17.3%- Downloading ... 18.0%- Downloading ... 18.6%- Downloading ... 19.2%- Downloading ... 19.9%- Downloading ... 20.5%- Downloading ... 21.2%- Downloading ... 21.8%- Downloading ... 22.4%- Downloading ... 23.1%- Downloading ... 23.7%- Downloading ... 24.4%- Downloading ... 25.0%- Downloading ... 25.6%- Downloading ... 26.3%- Downloading ... 26.9%- Downloading ... 27.6%- Downloading ... 28.2%- Downloading ... 28.9%- Downloading ... 29.5%- Downloading ... 30.1%- Downloading ... 30.8%- Downloading ... 31.4%- Downloading ... 32.1%- Downloading ... 32.7%- Downloading ... 33.3%- Downloading ... 34.0%- Downloading ... 34.6%- Downloading ... 35.3%- Downloading ... 35.9%- Downloading ... 36.5%- Downloading ... 37.2%- Downloading ... 37.8%- Downloading ... 38.5%- Downloading ... 39.1%- Downloading ... 39.8%- Downloading ... 40.4%- Downloading ... 41.0%- Downloading ... 41.7%- Downloading ... 42.3%- Downloading ... 43.0%- Downloading ... 43.6%- Downloading ... 44.2%- Downloading ... 44.9%- Downloading ... 45.5%- Downloading ... 46.2%- Downloading ... 46.8%- Downloading ... 47.4%- Downloading ... 48.1%- Downloading ... 48.7%- Downloading ... 49.4%- Downloading ... 50.0%- Downloading ... 50.7%- Downloading ... 51.3%- Downloading ... 51.9%- Downloading ... 52.6%- Downloading ... 53.2%- Downloading ... 53.9%- Downloading ... 54.5%- Downloading ... 55.1%- Downloading ... 55.8%- Downloading ... 56.4%- Downloading ... 57.1%- Downloading ... 57.7%- Downloading ... 58.3%- Downloading ... 59.0%- Downloading ... 59.6%- Downloading ... 60.3%- Downloading ... 60.9%- Downloading ... 61.6%- Downloading ... 62.2%- Downloading ... 62.8%- Downloading ... 63.5%- Downloading ... 64.1%- Downloading ... 64.8%- Downloading ... 65.4%- Downloading ... 66.0%- Downloading ... 66.7%- Downloading ... 67.3%- Downloading ... 68.0%- Downloading ... 68.6%- Downloading ... 69.2%- Downloading ... 69.9%- Downloading ... 70.5%- Downloading ... 71.2%- Downloading ... 71.8%- Downloading ... 72.5%- Downloading ... 73.1%- Downloading ... 73.7%- Downloading ... 74.4%- Downloading ... 75.0%- Downloading ... 75.7%- Downloading ... 76.3%- Downloading ... 76.9%- Downloading ... 77.6%- Downloading ... 78.2%- Downloading ... 78.9%- Downloading ... 79.5%- Downloading ... 80.1%- Downloading ... 80.8%- Downloading ... 81.4%- Downloading ... 82.1%- Downloading ... 82.7%- Downloading ... 83.3%- Downloading ... 84.0%- Downloading ... 84.6%- Downloading ... 85.3%- Downloading ... 85.9%- Downloading ... 86.6%- Downloading ... 87.2%- Downloading ... 87.8%- Downloading ... 88.5%- Downloading ... 89.1%- Downloading ... 89.8%- Downloading ... 90.4%- Downloading ... 91.0%- Downloading ... 91.7%- Downloading ... 92.3%- Downloading ... 93.0%- Downloading ... 93.6%- Downloading ... 94.2%- Downloading ... 94.9%- Downloading ... 95.5%- Downloading ... 96.2%- Downloading ... 96.8%- Downloading ... 97.5%- Downloading ... 98.1%- Downloading ... 98.7%- Downloading ... 99.4%- Downloading ... 100.0%/Users/jacoren/Developer/Personal/AlphaLens/.venv/lib/python3.13/site-packages/simfin/load.py:154: FutureWarning: The argument 'date_parser' is deprecated and will be removed in a future version. Please use 'date_format' instead, or read your data in as 'object' dtype and then call 'to_datetime'.
  df = pd.read_csv(path, sep=';', header=0,

- Extracting zip-file ... Done!
- Loading from disk ... Done!
- Rows: 31,633
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

Dataset "us-cashflow-annual" not on disk.
- Downloading ... 0.9%- Downloading ... 1.8%- Downloading ... 2.6%- Downloading ... 3.5%- Downloading ... 4.4%- Downloading ... 5.3%- Downloading ... 6.1%- Downloading ... 7.0%- Downloading ... 7.9%- Downloading ... 8.8%- Downloading ... 9.6%- Downloading ... 10.5%- Downloading ... 11.4%- Downloading ... 12.3%- Downloading ... 13.1%- Downloading ... 14.0%- Downloading ... 14.9%- Downloading ... 15.8%- Downloading ... 16.6%- Downloading ... 17.5%- Downloading ... 18.4%- Downloading ... 19.3%- Downloading ... 20.1%- Downloading ... 21.0%- Downloading ... 21.9%- Downloading ... 22.8%- Downloading ... 23.6%- Downloading ... 24.5%- Downloading ... 25.4%- Downloading ... 26.3%- Downloading ... 27.1%- Downloading ... 28.0%- Downloading ... 28.9%- Downloading ... 29.8%- Downloading ... 30.6%- Downloading ... 31.5%- Downloading ... 32.4%- Downloading ... 33.3%- Downloading ... 34.1%- Downloading ... 35.0%- Downloading ... 35.9%- Downloading ... 36.8%- Downloading ... 37.6%- Downloading ... 38.5%- Downloading ... 39.4%- Downloading ... 40.3%- Downloading ... 41.1%- Downloading ... 42.0%- Downloading ... 42.9%- Downloading ... 43.8%- Downloading ... 44.6%- Downloading ... 45.5%- Downloading ... 46.4%- Downloading ... 47.3%- Downloading ... 48.1%- Downloading ... 49.0%- Downloading ... 49.9%- Downloading ... 50.8%- Downloading ... 51.6%- Downloading ... 52.5%- Downloading ... 53.4%- Downloading ... 54.3%- Downloading ... 55.1%- Downloading ... 56.0%- Downloading ... 56.9%- Downloading ... 57.8%- Downloading ... 58.6%- Downloading ... 59.5%- Downloading ... 60.4%- Downloading ... 61.3%- Downloading ... 62.1%- Downloading ... 63.0%- Downloading ... 63.9%- Downloading ... 64.8%- Downloading ... 65.6%- Downloading ... 66.5%- Downloading ... 67.4%- Downloading ... 68.3%- Downloading ... 69.1%- Downloading ... 70.0%- Downloading ... 70.9%- Downloading ... 71.8%- Downloading ... 72.6%- Downloading ... 73.5%- Downloading ... 74.4%- Downloading ... 75.3%- Downloading ... 76.1%- Downloading ... 77.0%- Downloading ... 77.9%- Downloading ... 78.8%- Downloading ... 79.6%- Downloading ... 80.5%- Downloading ... 81.4%- Downloading ... 82.3%- Downloading ... 83.1%- Downloading ... 84.0%- Downloading ... 84.9%- Downloading ... 85.8%- Downloading ... 86.6%- Downloading ... 87.5%- Downloading ... 88.4%- Downloading ... 89.3%- Downloading ... 90.1%- Downloading ... 91.0%- Downloading ... 91.9%- Downloading ... 92.8%- Downloading ... 93.6%- Downloading ... 94.5%- Downloading ... 95.4%- Downloading ... 96.3%- Downloading ... 97.1%- Downloading ... 98.0%- Downloading ... 98.9%- Downloading ... 99.8%- Downloading ... 100.0%/Users/jacoren/Developer/Personal/AlphaLens/.venv/lib/python3.13/site-packages/simfin/load.py:154: FutureWarning: The argument 'date_parser' is deprecated and will be removed in a future version. Please use 'date_format' instead, or read your data in as 'object' dtype and then call 'to_datetime'.
  df = pd.read_csv(path, sep=';', header=0,
/Users/jacoren/Developer/Personal/AlphaLens/.venv/lib/python3.13/site-packages/simfin/load.py:154: FutureWarning: The argument 'date_parser' is deprecated and will be removed in a future version. Please use 'date_format' instead, or read your data in as 'object' dtype and then call 'to_datetime'.
  df = pd.read_csv(path, sep=';', header=0,
/Users/jacoren/Developer/Personal/AlphaLens/.venv/lib/python3.13/site-packages/simfin/load.py:154: FutureWarning: The argument 'date_parser' is deprecated and will be removed in a future version. Please use 'date_format' instead, or read your data in as 'object' dtype and then call 'to_datetime'.
  df = pd.read_csv(path, sep=';', header=0,

- Extracting zip-file ... Done!
- Loading from disk ... Done!
- Rows: 31,637
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
| 2025-09-30 | 2025-10-31 | 2025-10-31 | 31 |
| 2024-09-30 | 2024-11-01 | 2025-10-31 | 32 |
| 2023-09-30 | 2023-11-03 | 2025-10-31 | 34 |
| 2022-09-30 | 2022-10-28 | 2024-11-01 | 28 |
| 2021-09-30 | 2021-10-29 | 2023-11-03 | 29 |

## Step 6 — historical depth probe

Dataset "us-income-annual" on disk (0 days old).
- Loading from disk ... Done!
Per-ticker REPORT_DATE range (long-lived large-caps):

| Ticker | min REPORT_DATE | max REPORT_DATE | annual rows |
|---|---|---|---|
| AAPL | 2016-09-30 | 2025-09-30 | 10 |
| MSFT | 2017-06-30 | 2025-06-30 | 9 |
| GE | 2016-12-31 | 2025-12-31 | 10 |
| IBM | 2016-12-31 | 2024-12-31 | 9 |
| JPM | MISSING | MISSING | 0 |
| XOM | 2016-12-31 | 2025-12-31 | 10 |
| WFC | MISSING | MISSING | 0 |
| F | 2016-12-31 | 2025-12-31 | 10 |
| T | 2016-12-31 | 2025-12-31 | 10 |
| KO | 2016-12-31 | 2025-12-31 | 10 |

Dataset-wide annual-row-per-ticker distribution:

- median: **7** years
- p25 / p75: 4 / 10 years
- max: 10 years
- earliest REPORT_DATE in entire dataset: **2016-07-31**
- latest REPORT_DATE: 2026-03-31

## Verdict gate

- Active R2000 coverage: **89.6%** (gate: >= 90%)
- Delisted sample coverage: **1/10** (gate: >= 5/10)

(Schema + DCF columns: inspect Step 4 output above.)
(Historical depth: inspect Step 6 above — earliest date in dataset is the binding constraint for 2007-2026 backtest.)
