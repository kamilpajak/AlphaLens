"""SIC → SPDR sector-ETF map for the sector-relative EDGE outcome (PR-2b, D4).

The sector-excess metric measures a candidate's forward return against ITS OWN
sector ETF rather than SPY, so the outcome benchmark is a different series from
the SPY-derived market_state label — breaking the SPY-on-SPY confound
(memo §4.2, D4 resolution). This module resolves a ticker → its 4-digit SEC SIC
code (via ``sic_index``) → one of the 11 SPDR Select Sector ETFs.

v1 is UNVALIDATED and maps at SIC 2-digit granularity (finer than the 10 coarse
SEC SIC divisions, coarser than GICS sub-industries). The exact assignments are
a hyperparameter logged in ``SECTOR_ETF_MAP_VERSION`` — a later refinement (FF48
or an equal-weight peer-cohort benchmark) is a separate registered outcome
version. Unmapped SIC (e.g. public administration) → ``None``; the caller writes
``sector_excess_return = None`` and excludes the row, never falling back to SPY.
"""

from __future__ import annotations

from alphalens_pipeline.data.fundamentals.sic_index import get_sic

SECTOR_ETF_MAP_VERSION = "sic2-spdr-v1"

# The 11 SPDR Select Sector ETFs (one per GICS sector).
SPDR_SECTOR_ETFS: frozenset[str] = frozenset(
    {"XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"}
)

# Ascending, NON-OVERLAPPING [lo, hi] SIC-4 ranges → SPDR ticker. Gaps (e.g.
# 9100-9729 public administration) resolve to None. Boundaries follow the
# conventional SIC→GICS-sector correspondence; pinned by TestSicRangesWellFormed.
_SIC_RANGES: tuple[tuple[int, int, str], ...] = (
    (100, 999, "XLP"),  # Agriculture, forestry, fishing → Staples
    (1000, 1099, "XLB"),  # Metal mining → Materials
    (1200, 1299, "XLE"),  # Coal mining → Energy
    (1300, 1399, "XLE"),  # Oil & gas extraction → Energy
    (1400, 1499, "XLB"),  # Nonmetallic minerals → Materials
    (1500, 1799, "XLI"),  # Construction → Industrials
    (2000, 2199, "XLP"),  # Food & tobacco → Staples
    (2200, 2399, "XLY"),  # Textiles & apparel → Discretionary
    (2400, 2499, "XLB"),  # Lumber & wood → Materials
    (2500, 2599, "XLY"),  # Furniture → Discretionary
    (2600, 2699, "XLB"),  # Paper → Materials
    (2700, 2799, "XLC"),  # Printing & publishing → Communication Services
    (2800, 2829, "XLB"),  # Industrial chemicals → Materials
    (2830, 2836, "XLV"),  # Drugs & biologicals → Health Care
    (2840, 2899, "XLB"),  # Other chemicals → Materials
    (2900, 2999, "XLE"),  # Petroleum refining → Energy
    (3000, 3099, "XLB"),  # Rubber & plastics → Materials
    (3100, 3199, "XLY"),  # Leather → Discretionary
    (3200, 3399, "XLB"),  # Stone/clay/glass + primary metals → Materials
    (3400, 3569, "XLI"),  # Fabricated metal + industrial machinery → Industrials
    (3570, 3579, "XLK"),  # Computer & office equipment → Technology
    (3580, 3599, "XLI"),  # Other machinery → Industrials
    (3600, 3659, "XLI"),  # Electrical industrial equipment → Industrials
    (3660, 3689, "XLK"),  # Comm equipment / electronic components / computers → Technology
    (3690, 3699, "XLI"),  # Misc electrical → Industrials
    (3700, 3719, "XLY"),  # Motor vehicles → Discretionary
    (3720, 3799, "XLI"),  # Aircraft / ships / rail equipment → Industrials
    (3800, 3839, "XLK"),  # Instruments / lab / optical → Technology
    (3840, 3851, "XLV"),  # Medical instruments & supplies → Health Care
    (3860, 3899, "XLI"),  # Photo / misc precision manufacturing → Industrials
    (3900, 3999, "XLY"),  # Misc manufacturing (jewelry, toys) → Discretionary
    (4000, 4599, "XLI"),  # Transportation (rail/truck/air/water) → Industrials
    (4600, 4699, "XLE"),  # Pipelines → Energy
    (4700, 4799, "XLI"),  # Transportation services → Industrials
    (4800, 4899, "XLC"),  # Communications → Communication Services
    (4900, 4999, "XLU"),  # Electric/gas/sanitary utilities → Utilities
    (5000, 5399, "XLY"),  # Wholesale + retail (building/general merch) → Discretionary
    (5400, 5499, "XLP"),  # Food stores → Staples
    (5500, 5999, "XLY"),  # Retail (auto/apparel/furniture/eating/misc) → Discretionary
    (6000, 6499, "XLF"),  # Banks / securities / insurance → Financials
    (6500, 6599, "XLRE"),  # Real estate → Real Estate
    (6600, 6797, "XLF"),  # Holding & investment offices → Financials
    (6798, 6798, "XLRE"),  # REIT → Real Estate
    (6799, 6799, "XLF"),  # Investors NEC → Financials
    (7000, 7099, "XLY"),  # Hotels → Discretionary
    (7200, 7299, "XLY"),  # Personal services → Discretionary
    (7300, 7369, "XLI"),  # Business services → Industrials
    (7370, 7379, "XLK"),  # Computer / software services → Technology
    (7380, 7389, "XLI"),  # Misc business services → Industrials
    (7400, 7799, "XLY"),  # Services (auto repair, misc) → Discretionary
    (7800, 7899, "XLC"),  # Motion pictures → Communication Services
    (7900, 7999, "XLY"),  # Amusement & recreation → Discretionary
    (8000, 8099, "XLV"),  # Health services → Health Care
    (8100, 8199, "XLI"),  # Legal services → Industrials
    (8200, 8399, "XLY"),  # Educational & social services → Discretionary
    (8400, 8499, "XLY"),  # Museums / botanical → Discretionary
    (8600, 8699, "XLY"),  # Membership organisations → Discretionary
    (8700, 8799, "XLI"),  # Engineering / accounting / management → Industrials
    (8800, 8899, "XLY"),  # Private households / services → Discretionary
)


def sector_etf_for_sic(sic: int | None) -> str | None:
    """Return the SPDR sector ETF for a 4-digit SIC code, or ``None`` if unmapped."""
    if sic is None:
        return None
    for lo, hi, etf in _SIC_RANGES:
        if lo <= sic <= hi:
            return etf
    return None


def sector_etf_for_ticker(ticker: str) -> str | None:
    """Return the SPDR sector ETF for ``ticker`` via its SIC, or ``None``.

    ``None`` when the ticker is empty, absent from the SIC index, or maps to a
    SIC with no equity-sector proxy (e.g. public administration).
    """
    if not ticker:
        return None
    return sector_etf_for_sic(get_sic(ticker))


__all__ = [
    "SECTOR_ETF_MAP_VERSION",
    "SPDR_SECTOR_ETFS",
    "sector_etf_for_sic",
    "sector_etf_for_ticker",
]
