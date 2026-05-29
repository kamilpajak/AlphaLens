"""VIX-bucket market regime stamp for the feedback ledger.

v1 keeps it pure: classifier takes a VIX value and returns a bucket
label. Caller (Django POST handler) is responsible for sourcing the VIX
value before stamping the row, so the hot path of an API insert is not
held up by network I/O on every feedback submission.

Thresholds picked to match the common practitioner convention
(low <15 / mid 15-25 / high ≥25) that already appears in the project's
``signal_vol_regime`` attribution module — keeping the bucket vocabulary
consistent across the codebase.

SPX trend and sector trend are intentionally deferred to v2 / post-hoc
analysis per the locked design memo (Q6) — they need yfinance calls +
sector lookup that we'd otherwise pay on every POST.
"""

from __future__ import annotations


def classify_vix(vix_value: float | None) -> str:
    """Bucket a VIX value into ``low`` / ``mid`` / ``high`` / ``unknown``.

    None → ``unknown`` so a transient VIX fetch failure in the POST path
    degrades to a missing regime stamp instead of dropping the whole row.
    Better to lose one column of context than the user-authored decision.
    """
    if vix_value is None:
        return "unknown"
    if vix_value < 15.0:
        return "low"
    if vix_value < 25.0:
        return "mid"
    return "high"
