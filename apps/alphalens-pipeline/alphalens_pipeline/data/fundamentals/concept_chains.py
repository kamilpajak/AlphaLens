"""Canonical XBRL concept fallback chains per financial metric.

Different issuers report the same economic quantity under different us-gaap
tags. SaaS / post-2018 issuers report revenue as
``RevenueFromContractWithCustomerExcludingAssessedTax`` (ASC 606), legacy
industrials as ``SalesRevenueNet`` or ``Revenues``. Debt-free tech firms
omit ``LongTermDebt`` rows entirely. Each chain below documents which
issuer archetype motivates each fallback so additions stay auditable.

Chains are tried in order; first non-empty hit wins. All chains target
the us-gaap taxonomy and USD unit unless otherwise noted in the metric's
docstring.

Reused across:
- alphalens_pipeline.data.fundamentals.ttm_aggregator (Compustat TTM formula)
- alphalens_pipeline.data.store.edgar_fundamentals (parity dict assembly)
"""

from __future__ import annotations

from typing import Final

# --- Duration concepts (P&L / cash flow) -----------------------------------

REVENUE: Final[tuple[str, ...]] = (
    # ASC 606 contract revenue — modern SaaS / tech (SYM, post-2018 issuers).
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    # Catch-all revenue — large-cap diversified (AAPL, GOOG).
    "Revenues",
    # Legacy industrial reporters who never adopted the ASC 606 tag.
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
)

OPERATING_INCOME: Final[tuple[str, ...]] = ("OperatingIncomeLoss",)

# Depreciation + amortisation chain. Capital-intensive issuers report
# ``DepreciationDepletionAndAmortization`` (SYM, mining, energy); most others
# use the simpler ``DepreciationAndAmortization``. The third entry is a
# composite fallback handled specially by the aggregator: sum of the two
# component concepts when neither single-tag form is present.
DEPRECIATION_AMORTISATION: Final[tuple[str, ...]] = (
    "DepreciationAndAmortization",
    "DepreciationDepletionAndAmortization",
)
DEPRECIATION_AMORTISATION_COMPONENTS: Final[tuple[str, ...]] = (
    "Depreciation",
    "AmortizationOfIntangibleAssets",
)

OPERATING_CASH_FLOW: Final[tuple[str, ...]] = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)

# EDGAR reports CapEx with a positive sign (cash outflow magnitude); no
# sign flip needed — unlike SimFin which stores it negative.
CAPEX: Final[tuple[str, ...]] = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
)

INTEREST_EXPENSE: Final[tuple[str, ...]] = (
    "InterestExpense",
    "InterestExpenseDebt",
)

# Tax rate is the only derived metric — numerator / denominator —
# clamped to [0, 0.35] in the aggregator.
INCOME_TAX_EXPENSE: Final[tuple[str, ...]] = ("IncomeTaxExpenseBenefit",)
PRETAX_INCOME: Final[tuple[str, ...]] = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
)

NET_INCOME: Final[tuple[str, ...]] = (
    "NetIncomeLoss",
    "ProfitLoss",  # consolidated fallback (matched-pair convention)
)


# --- Instant concepts (balance sheet) --------------------------------------

LONG_TERM_DEBT: Final[tuple[str, ...]] = (
    "LongTermDebtNoncurrent",
    "LongTermDebt",
)

# Confusing XBRL naming: LongTermDebtCurrent IS short-term debt (the
# current portion of long-term debt due within 12 months).
SHORT_TERM_DEBT: Final[tuple[str, ...]] = (
    "LongTermDebtCurrent",
    "ShortTermBorrowings",
)

# Marker concepts proving the issuer files balance sheets at all. Used by
# the debt-free fallback: if any of these are present AND no LT/ST debt
# row ever appears, treat debt as 0.0 instead of None. Without this
# heuristic, debt-free tech issuers (MANH, CSCO when net cash) lose
# EV/EBITDA because compute_net_debt can't distinguish "missing data"
# from "structurally zero".
BALANCE_SHEET_MARKERS: Final[tuple[str, ...]] = (
    "Liabilities",
    "Assets",
    "StockholdersEquity",
)

CASH: Final[tuple[str, ...]] = (
    "CashAndCashEquivalentsAtCarryingValue",
    "Cash",
)

EQUITY: Final[tuple[str, ...]] = (
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)


# --- Shares-outstanding (different taxonomy + unit) ------------------------

# Shares Outstanding can live in either us-gaap or dei (DEI = Document and
# Entity Information). DEI is the modern primary; us-gaap is the legacy
# fallback. The store handles taxonomy dispatch separately because Arrow
# tables include taxonomy/unit columns.
SHARES_OUTSTANDING_US_GAAP: Final[tuple[str, ...]] = ("CommonStockSharesOutstanding",)
SHARES_OUTSTANDING_DEI: Final[tuple[str, ...]] = ("EntityCommonStockSharesOutstanding",)


__all__ = [
    "BALANCE_SHEET_MARKERS",
    "CAPEX",
    "CASH",
    "DEPRECIATION_AMORTISATION",
    "DEPRECIATION_AMORTISATION_COMPONENTS",
    "EQUITY",
    "INCOME_TAX_EXPENSE",
    "INTEREST_EXPENSE",
    "LONG_TERM_DEBT",
    "NET_INCOME",
    "OPERATING_CASH_FLOW",
    "OPERATING_INCOME",
    "PRETAX_INCOME",
    "REVENUE",
    "SHARES_OUTSTANDING_DEI",
    "SHARES_OUTSTANDING_US_GAAP",
    "SHORT_TERM_DEBT",
]
