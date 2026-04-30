"""Filter Form4Records down to Layer 2d-eligible purchases.

Signal spec (design doc §6 per Perplexity R5):
- Transaction code ``P`` only (open-market purchase).
- Reporting owner must be an officer or director. Pure 10%-beneficial-owners
  (e.g. activist funds) are excluded because their information asymmetry is
  orthogonal to the Kelley-Tetlock cluster-buy effect (Cohen et al. 2012).
- 10b5-1 plan age is applied downstream (M4 cluster detection) because
  the age lookup depends on M3 regex extraction.
"""

from __future__ import annotations

from collections.abc import Iterable

from .form4_records import Form4Record


def filter_eligible(records: Iterable[Form4Record]) -> list[Form4Record]:
    return [r for r in records if _is_eligible(r)]


def _is_eligible(r: Form4Record) -> bool:
    if r.transaction_code != "P":
        return False
    return r.is_officer or r.is_director
