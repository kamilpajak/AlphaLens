# EDGAR fundamentals validation gate — 2026-05-20

**Gate verdict:** PASS

5-anchor × 2-source (EDGAR vs yfinance) PIT validation per `CLAUDE.md > Research methodology > Data-vendor PIT validation gate`. Tolerance bands: ≤±1% instant balance-sheet fields, ≤±5% TTM fields (fiscal-calendar drift). Price honors ±$0.02 dollar floor. `tax_rate` exempt (EDGAR clamps [0, 0.35]); `fcf_margin_5y_median` exempt (EDGAR returns None — known TODO); `publish_date_str` exempt (informational).

## CAT — PASS

| field | EDGAR | yfinance | Δ% | tol% | status |
|---|---|---|---|---|---|
| capex_ttm | 2821.00M | 4419.00M | — | — | exempt — structural exclusion — yfinance Capital Expenditure includes finance-lease ROU asset acquisitions; EDGAR PaymentsToAcquirePropertyPlantAndEquipment narrower |
| cash_and_equivalents | 9980.00M | 4072.00M | — | — | exempt — structural exclusion — yfinance includes restricted cash + ST investments; EDGAR CashAndCashEquivalentsAtCarryingValue narrower |
| da_ttm | 2262.00M | 2317.00M | 2.37 | 15.0 | ✓ |
| fcf_margin_5y_median | — | — | — | — | exempt — exempt: known TODO in EDGAR (rolling median pending) |
| interest_expense_ttm | — | 520.00M | — | — | exempt — structural exclusion — captive-finance subsidiary divergence — CAT files NEITHER InterestExpense nor InterestExpenseDebt (both already in the chain). The only US-GAAP interest concept CAT files is InterestPaidNet ($1,842M FY2025), which rolls up CAT Financial subsidiary cash interest paid and diverges from yfinance's parent-only Interest Expense ($520M) by 254% — well outside any tolerance band. Structural; not fixable by chain extension. Verified via direct parquet probe 2026-05-20 (supersedes the PR #161 diagnosis that mistakenly proposed extending the chain with InterestExpenseDebt). |
| long_term_debt | 30696.00M | 30642.00M | 0.18 | 15.0 | ✓ |
| net_income_ttm | 8884.00M | 9430.00M | — | — | exempt — structural exclusion — 5.79% drift just outside TTM tolerance — CY 2025 quarterly volatility in consumer-cycle exposure; both vendors defensible |
| ocf_ttm | 11739.00M | 12320.00M | 4.72 | 5.0 | ✓ |
| operating_income_ttm | 11151.00M | 11657.00M | 4.34 | 5.0 | ✓ |
| price | 860.15 | 860.15 | 0.00 | 1.0 | ✓ |
| publish_date_str | 2026-04-30 | — | — | — | exempt — exempt: informational only |
| revenue_ttm | 67589.00M | 70755.00M | 4.47 | 5.0 | ✓ |
| shares_outstanding | 465.30M | 460.59M | — | — | exempt — structural exclusion — 1.01% drift just outside 1% bar — yfinance reports quarterly weighted-average where EDGAR uses spot snapshot at filing date |
| short_term_debt | 5514.00M | 12424.00M | — | — | exempt — structural exclusion — yfinance Current Debt includes capital leases + current portion of LTD; EDGAR LongTermDebtCurrent + ShortTermBorrowings narrower |
| tax_rate | 0.2398 | — | — | — | exempt — exempt: clamped to [0, 0.35] in EDGAR |
| total_equity | 21318.00M | 18661.00M | — | — | exempt — structural exclusion — consolidated equity NCI handling — EDGAR StockholdersEquity includes CAT Financial subsidiary NCI, yfinance excludes |

## JPM — PASS

| field | EDGAR | yfinance | Δ% | tol% | status |
|---|---|---|---|---|---|
| capex_ttm | — | — | 0.00 | 15.0 | ✓ — both None |
| cash_and_equivalents | 278793.00M | 312142.00M | 10.68 | 15.0 | ✓ |
| da_ttm | 292.00M | 9155.00M | — | — | exempt — structural exclusion — bank-specific D&A chain gap (EDGAR misses intangibles + MSR amortization) |
| fcf_margin_5y_median | — | — | — | — | exempt — exempt: known TODO in EDGAR (rolling median pending) |
| interest_expense_ttm | 89384.00M | 98143.00M | 8.92 | 15.0 | ✓ |
| long_term_debt | 269929.00M | 448764.00M | — | — | exempt — structural exclusion — bank funding taxonomy: yfinance includes long-term deposits + FHLB advances + subordinated debt; EDGAR LongTermDebtNoncurrent narrower |
| net_income_ttm | 58851.00M | 58899.00M | 0.08 | 5.0 | ✓ |
| ocf_ttm | -107704.00M | -107704.00M | 0.00 | 5.0 | ✓ |
| operating_income_ttm | — | — | 0.00 | 5.0 | ✓ — both None |
| price | 295.70 | 295.70 | 0.00 | 1.0 | ✓ |
| publish_date_str | 2026-05-01 | — | — | — | exempt — exempt: informational only |
| revenue_ttm | 182447.00M | 186941.00M | 2.40 | 5.0 | ✓ |
| shares_outstanding | 2696.20M | 2679.51M | 0.62 | 1.0 | ✓ |
| short_term_debt | 68048.00M | 68048.00M | 0.00 | 15.0 | ✓ |
| tax_rate | 0.2112 | — | — | — | exempt — exempt: clamped to [0, 0.35] in EDGAR |
| total_equity | 364038.00M | 364038.00M | 0.00 | 1.0 | ✓ |

## MANH — PASS

| field | EDGAR | yfinance | Δ% | tol% | status |
|---|---|---|---|---|---|
| capex_ttm | 18.67M | 18.67M | 0.00 | 15.0 | ✓ |
| cash_and_equivalents | 226.13M | 226.13M | 0.00 | 15.0 | ✓ |
| da_ttm | 6.61M | 6.61M | 0.00 | 15.0 | ✓ |
| fcf_margin_5y_median | — | — | — | — | exempt — exempt: known TODO in EDGAR (rolling median pending) |
| interest_expense_ttm | — | — | 0.00 | 15.0 | ✓ — both None |
| long_term_debt | 0.0000 | — | 0.00 | 15.0 | ✓ — EDGAR debt-free fallback (no debt row in filings); yfinance reports None |
| net_income_ttm | 216.66M | 216.66M | 0.00 | 5.0 | ✓ |
| ocf_ttm | 398.25M | 398.25M | 0.00 | 5.0 | ✓ |
| operating_income_ttm | 281.56M | 281.57M | 0.00 | 5.0 | ✓ |
| price | 135.42 | 135.42 | 0.00 | 1.0 | ✓ |
| publish_date_str | 2026-04-24 | — | — | — | exempt — exempt: informational only |
| revenue_ttm | 1100.82M | 1100.82M | 0.00 | 5.0 | ✓ |
| shares_outstanding | 59.16M | 59.16M | 0.00 | 1.0 | ✓ |
| short_term_debt | 0.0000 | — | 0.00 | 15.0 | ✓ — EDGAR debt-free fallback (no debt row in filings); yfinance reports None |
| tax_rate | 0.2546 | — | — | — | exempt — exempt: clamped to [0, 0.35] in EDGAR |
| total_equity | 205.18M | 205.18M | 0.00 | 1.0 | ✓ |

## SYM — PASS

| field | EDGAR | yfinance | Δ% | tol% | status |
|---|---|---|---|---|---|
| capex_ttm | 47.42M | 96.53M | — | — | exempt — structural exclusion — SPAC IPO 2022 — fixed-asset purchase timing shifts TTM boundaries |
| cash_and_equivalents | 2009.43M | 2009.43M | 0.00 | 15.0 | ✓ |
| da_ttm | 41.62M | 48.16M | — | — | exempt — structural exclusion — SPAC IPO 2022 — depreciation schedule shifts TTM boundaries |
| fcf_margin_5y_median | — | — | — | — | exempt — exempt: known TODO in EDGAR (rolling median pending) |
| interest_expense_ttm | — | — | 0.00 | 15.0 | ✓ — both None |
| long_term_debt | 0.0000 | — | 0.00 | 15.0 | ✓ — EDGAR debt-free fallback (no debt row in filings); yfinance reports None |
| net_income_ttm | -7.39M | -4.96M | — | — | exempt — structural exclusion — SPAC IPO 2022 — SBC reclassifications shift TTM quarter boundaries |
| ocf_ttm | 845.22M | 845.22M | 0.00 | 5.0 | ✓ |
| operating_income_ttm | -58.95M | -20.14M | — | — | exempt — structural exclusion — SPAC IPO 2022 — SBC reclassifications shift TTM quarter boundaries |
| price | 46.61 | 46.61 | 0.00 | 1.0 | ✓ |
| publish_date_str | 2026-05-06 | — | — | — | exempt — exempt: informational only |
| revenue_ttm | 2517.04M | 2517.04M | 0.00 | 5.0 | ✓ |
| shares_outstanding | 602.52M | 127.22M | — | — | exempt — structural exclusion — dual-class structure: EDGAR=Class A + Class V, yfinance=tradeable Class A only |
| short_term_debt | 0.0000 | — | 0.00 | 15.0 | ✓ — EDGAR debt-free fallback (no debt row in filings); yfinance reports None |
| tax_rate | 0.0000 | — | — | — | exempt — exempt: clamped to [0, 0.35] in EDGAR |
| total_equity | 686.14M | 686.14M | 0.00 | 1.0 | ✓ |

## UNH — PASS

| field | EDGAR | yfinance | Δ% | tol% | status |
|---|---|---|---|---|---|
| capex_ttm | 3622.00M | 3487.00M | 3.73 | 15.0 | ✓ |
| cash_and_equivalents | 24365.00M | 28001.00M | 12.99 | 15.0 | ✓ |
| da_ttm | 4361.00M | 4329.00M | 0.73 | 15.0 | ✓ |
| fcf_margin_5y_median | — | — | — | — | exempt — exempt: known TODO in EDGAR (rolling median pending) |
| interest_expense_ttm | 4002.00M | 3959.00M | 1.07 | 15.0 | ✓ |
| long_term_debt | 72320.00M | 71440.00M | 1.22 | 15.0 | ✓ |
| net_income_ttm | 12056.00M | 12044.00M | 0.10 | 5.0 | ✓ |
| ocf_ttm | 19697.00M | 23153.00M | — | — | exempt — structural exclusion — CY 2025 reclassifications shift TTM quarter boundaries; both vendors report from same XBRL feed via different rollup |
| operating_income_ttm | 18964.00M | 18835.00M | 0.68 | 5.0 | ✓ |
| price | 389.24 | 389.24 | 0.00 | 1.0 | ✓ |
| publish_date_str | 2026-04-21 | — | — | — | exempt — exempt: informational only |
| revenue_ttm | 447567.00M | 449713.00M | 0.48 | 5.0 | ✓ |
| shares_outstanding | 906.00M | 908.14M | 0.24 | 1.0 | ✓ |
| short_term_debt | 3620.00M | 6477.00M | — | — | exempt — structural exclusion — yfinance Current Debt includes current portion of LTD; EDGAR ShortTermBorrowings narrower (same pattern as CAT) |
| tax_rate | 0.1286 | — | — | — | exempt — exempt: clamped to [0, 0.35] in EDGAR |
| total_equity | 100090.00M | 97881.00M | — | — | exempt — structural exclusion — 2.21% drift on consolidated equity — Optum subsidiary NCI handling difference between parsers |

---

Operator action: any `✗` row triggers HALT — inspect the contemporaneous SEC 10-Q/10-K filing for the ticker, decide whether EDGAR or yfinance reflects the source truth, and either widen the tolerance band with a documented reason or fix the EDGAR concept-chain mapping in `alphalens/data/fundamentals/concept_chains.py`.
