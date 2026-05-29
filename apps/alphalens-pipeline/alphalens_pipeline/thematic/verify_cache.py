"""Gap-detection for the daily ``thematic_news`` parquet cache.

The daily ``alphalens-thematic-build.timer`` writes one parquet per UTC
calendar date to ``~/.alphalens/thematic_news/{YYYY-MM-DD}.parquet``.
Downstream consumers (``catalyst_resolver._load_window``) iterate the
files present in the directory; a silent missing day produces a silent
missing-news bug in the brief-generation window.

This module surfaces missing days as a HARD signal:

* **missing-day** → no parquet file at all (or unreadable). Ingest
  crashed before write OR the systemd timer didn't fire at all.
  ``VerifyResult.missing_days`` non-empty → caller logs/alerts/exits-1.

* **no-news day** → parquet exists with zero rows. Legitimately quiet
  (full-day US holiday, weekend overnight). Surfaced on
  ``VerifyResult.zero_row_days`` for observability but NOT counted as
  a fault.

Both ``news_ingest.ingest_daily`` (current behaviour) and
``catalyst_resolver._load_window`` already treat 0-row parquets as
"legitimate quiet day"; the verifier matches that convention.

Design memo: ``docs/research/paper_trading_non_trading_day_2026_05_29.md``
§5.1 Risk A.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

# Default cache root — keep in lock-step with
# ``alphalens_pipeline.thematic.news_ingest.DEFAULT_CACHE_DIR``. Bound here
# rather than imported lazily to keep the CLI surface (which reads this
# constant for the ``--cache-dir`` default) decoupled from the heavy
# ingest module.
DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_news"


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of one verification pass.

    ``missing_days`` carries the dates that should have a parquet but
    don't (the alert-worthy condition). ``zero_row_days`` carries
    parquets that exist but legitimately have no rows — informational
    only. ``checked_days`` echoes the requested window size so the
    caller can build a "5/7 dates OK" status line without re-deriving
    the count.
    """

    missing_days: list[dt.date] = field(default_factory=list)
    zero_row_days: list[dt.date] = field(default_factory=list)
    checked_days: int = 0

    @property
    def ok(self) -> bool:
        """True when the cache is gap-free (zero-row days are tolerated)."""
        return not self.missing_days


def verify_cache(
    *,
    cache_dir: Path,
    days: int,
    today: dt.date | None = None,
) -> VerifyResult:
    """Check that ``cache_dir`` has a parquet for every date in the
    window ``[today - days + 1, today]`` (today inclusive).

    Args:
        cache_dir: Root containing ``{YYYY-MM-DD}.parquet`` files.
            Missing directory is treated as "every requested day is
            missing" — accommodates the first-ever timer run before
            the directory has been bootstrapped.
        days: Window size in calendar days, inclusive of ``today``.
            Must be >= 1; 0 and negative raise ``ValueError``.
        today: Anchor date. Defaults to UTC today so the CLI's
            ``alphalens thematic verify-cache --days 7`` covers the
            past week including today. Tests pin a fixed anchor for
            determinism.

    Reading a corrupted parquet (truncated write, foreign content at
    the expected path) is treated as **missing** — the next ingest run
    will overwrite it, but in the meantime the operator should be
    alerted so they can investigate the upstream cause.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    anchor = today if today is not None else dt.datetime.now(dt.UTC).date()

    missing: list[dt.date] = []
    zero_row: list[dt.date] = []

    # ``[today - days + 1, today]`` — inclusive of today. iso-sorted
    # order so the resulting lists are also chronological.
    expected_dates = [anchor - dt.timedelta(days=i) for i in range(days - 1, -1, -1)]

    for d in expected_dates:
        path = cache_dir / f"{d.isoformat()}.parquet"
        if not path.is_file():
            missing.append(d)
            continue
        try:
            df = pd.read_parquet(path)
        except Exception as exc:
            logger.warning(
                "verify-cache: %s exists but unreadable (%s); treating as missing",
                path,
                exc,
            )
            missing.append(d)
            continue
        if len(df) == 0:
            zero_row.append(d)

    return VerifyResult(
        missing_days=missing,
        zero_row_days=zero_row,
        checked_days=days,
    )
