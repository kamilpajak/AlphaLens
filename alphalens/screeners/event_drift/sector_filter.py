"""GICS sector filter for the PEAD universe — exclude Financials and Utilities.

Pre-reg ``event_drift_v3_pead_quality_clean`` locks
``sector_exclusions_gics: [40, 55]``. This module translates GICS sector
codes into the SIC ranges actually present in EDGAR companyfacts:

  - GICS 40 Financials -> SIC 6000-6999 (banks, S&Ls, insurance, REITs,
    securities, holding companies, blank checks)
  - GICS 55 Utilities  -> SIC 4900-4999 (electric, gas, water, sanitary)

Sloan accruals are unstable on banks/REITs because their balance sheet
denominators (Avg Total Assets) span loan books and security inventories
rather than operating-business assets. Excluding these sectors avoids the
known accrual blowup in PEAD x quality strategies (zen review 2026-05-03).

The filter takes an injectable ``sic_map: Mapping[str, int]`` so tests can
stub. Production code populates the map via
``alphalens.screeners.event_drift.sic_provider`` reading cached SEC
submission JSONs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Literal

# (GICS sector code, low SIC, high SIC) — inclusive at both ends.
_DEFAULT_EXCLUDED_RANGES: Sequence[tuple[int, int]] = (
    (4900, 4999),  # GICS 55 Utilities
    (6000, 6999),  # GICS 40 Financials
)


class SectorFilter:
    """Exclude tickers whose SIC falls within ``excluded_ranges``.

    Parameters
    ----------
    sic_map:
        Ticker (case-insensitive) -> SIC integer. Typically built from
        cached SEC submission JSONs.
    excluded_ranges:
        Inclusive (low, high) SIC ranges to exclude. Default excludes
        Financials and Utilities per pre-reg.
    unknown_policy:
        ``"include"`` (default) — missing SIC means do not exclude (rely on
        other universe gates). ``"exclude"`` — missing SIC means exclude.
    """

    def __init__(
        self,
        sic_map: Mapping[str, int],
        *,
        excluded_ranges: Sequence[tuple[int, int]] = _DEFAULT_EXCLUDED_RANGES,
        unknown_policy: Literal["include", "exclude"] = "include",
    ) -> None:
        self._sic_map = {k.upper(): int(v) for k, v in sic_map.items()}
        self._excluded_ranges = tuple((int(lo), int(hi)) for lo, hi in excluded_ranges)
        if unknown_policy not in {"include", "exclude"}:
            raise ValueError(
                f"unknown_policy must be 'include' or 'exclude', got {unknown_policy!r}"
            )
        self._unknown_excludes = unknown_policy == "exclude"

    def sic(self, ticker: str) -> int | None:
        return self._sic_map.get(ticker.upper())

    def is_excluded(self, ticker: str) -> bool:
        sic = self.sic(ticker)
        if sic is None:
            return self._unknown_excludes
        return any(lo <= sic <= hi for lo, hi in self._excluded_ranges)

    def filter(self, tickers: Iterable[str]) -> list[str]:
        """Return tickers in input order with excluded ones dropped."""
        return [t for t in tickers if not self.is_excluded(t)]
