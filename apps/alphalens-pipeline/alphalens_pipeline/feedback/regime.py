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

v2 PR-2 adds the VIX SOURCE without breaking the hot-path rule: a separate
process (``alphalens cache refresh-vix``, hung off the daily thematic build)
fetches VIXCLS from FRED and writes a tiny JSON cache; the POST path only
READS that cache via :func:`get_cached_vix` — one local file read, zero
network. Any miss / stale / unreadable case returns ``None`` so
:func:`classify_vix` still degrades to ``unknown``. Keep this module free of
heavy imports (no pandas / requests / FRED) so importing it on the request
path stays cheap.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Staleness ceiling for the VIX cache, measured on ``fetched_at``. 96h (4
# days) tolerates a normal weekend plus one adjacent market holiday without
# ever classifying on data older than the last real close + that holiday;
# past it the refresh process is presumed dead and we degrade to "unknown".
_VIX_MAX_AGE_SECONDS = 96 * 3600


def default_vix_cache_path() -> Path:
    """Default location of the VIX regime cache JSON (host ``~/.alphalens``)."""
    return Path.home() / ".alphalens" / "macro" / "vix_regime_cache.json"


def get_cached_vix(
    cache_path: str | Path | None = None,
    *,
    now: dt.datetime | None = None,
) -> float | None:
    """Read the cached VIX value, or ``None`` on any miss / stale / error.

    ONE local file read, zero network — safe for the Django POST hot path.
    ``cache_path=None`` falls back to :func:`default_vix_cache_path`. Returns
    the stored VIX float when the cache was refreshed within
    ``_VIX_MAX_AGE_SECONDS``; otherwise ``None`` so :func:`classify_vix`
    stamps ``unknown`` (missing file, malformed JSON, missing/!parseable
    ``fetched_at``, stale, or non-numeric ``vix`` all degrade to None — the
    decision row is never blocked on a regime stamp).
    """
    path = Path(cache_path) if cache_path is not None else default_vix_cache_path()
    try:
        payload = json.loads(path.read_text())
        fetched_at = dt.datetime.fromisoformat(payload["fetched_at"])
        vix = float(payload["vix"])
        now = now or dt.datetime.now(dt.UTC)
        if (now - fetched_at).total_seconds() > _VIX_MAX_AGE_SECONDS:
            logger.warning(
                "VIX cache at %s is stale (fetched_at=%s) — stamping unknown.",
                path,
                payload.get("fetched_at"),
            )
            return None
        logger.debug(
            "VIX cache hit: vix=%s observation_date=%s fetched_at=%s",
            vix,
            payload.get("observation_date"),
            payload.get("fetched_at"),
        )
        return vix
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


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
