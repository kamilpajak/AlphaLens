/Users/jacoren/Developer/Personal/AlphaLens/.venv/lib/python3.13/site-packages/simfin/load.py:154: FutureWarning: The argument 'date_parser' is deprecated and will be removed in a future version. Please use 'date_format' instead, or read your data in as 'object' dtype and then call 'to_datetime'.
  df = pd.read_csv(path, sep=';', header=0,
/Users/jacoren/Developer/Personal/AlphaLens/.venv/lib/python3.13/site-packages/simfin/load.py:154: FutureWarning: The argument 'date_parser' is deprecated and will be removed in a future version. Please use 'date_format' instead, or read your data in as 'object' dtype and then call 'to_datetime'.
  df = pd.read_csv(path, sep=';', header=0,
# SimFin financials sector probe — 2026-05-12

Dataset "us-companies" on disk (0 days old).
- Loading from disk ... Done!
Dataset "us-income-annual" on disk (0 days old).
- Loading from disk ... Done!
Dataset "industries" not on disk.
- Downloading ... 100.0%/Users/jacoren/Developer/Personal/AlphaLens/.venv/lib/python3.13/site-packages/simfin/load.py:154: FutureWarning: The argument 'date_parser' is deprecated and will be removed in a future version. Please use 'date_format' instead, or read your data in as 'object' dtype and then call 'to_datetime'.
  df = pd.read_csv(path, sep=';', header=0,

- Extracting zip-file ... Done!
- Loading from disk ... Done!
## Probe results

| Ticker | Name | Type | In `companies`? | In `income`? | Industry |
|---|---|---|---|---|---|
| `JPM` | JPMorgan Chase | money-center bank | ✅ | ❌ | Financial Services / Banks |
| `WFC` | Wells Fargo | money-center bank | ✅ | ❌ | Financial Services / Banks |
| `BAC` | Bank of America | money-center bank | ✅ | ❌ | Financial Services / Banks |
| `C` | Citigroup | money-center bank | ✅ | ❌ | Financial Services / Banks |
| `GS` | Goldman Sachs | investment bank | ✅ | ❌ | Financial Services / Banks |
| `MS` | Morgan Stanley | investment bank | ✅ | ❌ | Financial Services / Banks |
| `USB` | US Bancorp | regional bank | ✅ | ❌ | Financial Services / Banks |
| `PNC` | PNC Financial | regional bank | ✅ | ❌ | Financial Services / Banks |
| `BLK` | BlackRock | asset manager | ✅ | ✅ | Financial Services / Asset Management |
| `AXP` | American Express | payments | ✅ | ❌ | Financial Services / Credit Services |
| `V` | Visa | payments — non-bank | ✅ | ✅ | Financial Services / Credit Services |
| `MA` | Mastercard | payments — non-bank | ✅ | ✅ | Financial Services / Credit Services |
| `BRK-B` | Berkshire Hathaway B | insurance/holding | ❌ | ❌ |  |
| `AIG` | AIG | insurance | ✅ | ❌ | Financial Services / Insurance |
| `MET` | MetLife | insurance | ✅ | ❌ | Financial Services / Insurance - Life |

**Summary: 14/15 in `companies`, 3/15 in `income` annual**

## Sector aggregate (full us-companies)

Detected financials-related sectors: `['Financial Services']`

- **Financial Services**: 1188 companies in metadata, 453 (38.1%) have income data
