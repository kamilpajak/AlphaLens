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
    lag_days: int = 1,
) -> VerifyResult:
    """Check that ``cache_dir`` has a parquet for every date in the
    window ``[today - lag_days - days + 1, today - lag_days]``
    (last-expected-day inclusive).

    Args:
        cache_dir: Root containing ``{YYYY-MM-DD}.parquet`` files.
            Missing directory is treated as "every requested day is
            missing" — accommodates the first-ever timer run before
            the directory has been bootstrapped.
        days: Window size in calendar days, inclusive of the last
            expected day (= ``today - lag_days``). Must be >= 1; 0 and
            negative raise ``ValueError``.
        today: Anchor date for "now". Defaults to UTC today. Tests pin
            a fixed value for determinism; the systemd hook leaves it
            unset so wall-clock UTC drives the check.
        lag_days: Offset between the anchor and the LAST date the
            window expects to find. The ingest pipeline writes the
            daily news parquet keyed on ``asof = today - 1`` (the
            previous calendar day), so the default ``lag_days=1``
            matches: the verifier's window ends on yesterday, not
            today. Pass ``lag_days=0`` to inspect a window that
            includes the anchor itself (used in tests that pre-seed a
            cache up to + including the anchor date). Must be >= 0.

            Why: PR-E shipped the verifier with ``lag_days=0`` and the
            6:30 UTC systemd timer fired against an anchor (UTC today)
            for which the ingest had NOT yet written a file — guaranteed
            false-positive MISSING alert + a halt on the rebuild-cache
            ExecStartPost. Caught by the manual fire 2026-05-30.

    Reading a corrupted parquet (truncated write, foreign content at
    the expected path) is treated as **missing** — the next ingest run
    will overwrite it, but in the meantime the operator should be
    alerted so they can investigate the upstream cause.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    if lag_days < 0:
        raise ValueError(f"lag_days must be >= 0, got {lag_days}")
    anchor = today if today is not None else dt.datetime.now(dt.UTC).date()
    # Guard against an accidental far-future anchor (e.g. operator
    # passes ``--today 2099-01-01`` during incident response). Without
    # this the verifier would silently report every requested day as
    # missing and fire a false-positive Telegram avalanche. One-day
    # tolerance covers DST / cross-tz anchor calls; anything beyond
    # tomorrow's UTC date is clearly a typo.
    today_utc = dt.datetime.now(dt.UTC).date()
    if anchor > today_utc + dt.timedelta(days=1):
        raise ValueError(
            f"today={anchor.isoformat()} is more than one day in the "
            f"future (UTC today={today_utc.isoformat()}); refusing to "
            f"generate spurious missing-day alerts."
        )

    missing: list[dt.date] = []
    zero_row: list[dt.date] = []

    # Window ends on ``anchor - lag_days`` (== "the most recent date the
    # ingest pipeline has had a chance to write"). Walks back ``days``
    # calendar days from there. iso-sorted order so the resulting lists
    # are also chronological.
    last_expected = anchor - dt.timedelta(days=lag_days)
    expected_dates = [last_expected - dt.timedelta(days=i) for i in range(days - 1, -1, -1)]

    for d in expected_dates:
        path = cache_dir / f"{d.isoformat()}.parquet"
        if not path.is_file():
            missing.append(d)
            continue
        try:
            df = pd.read_parquet(path)
        except Exception:
            # ``logger.exception`` captures the full traceback in
            # journalctl alongside the warning message so post-mortem
            # investigation distinguishes a real pyarrow/encoding bug
            # from the more common truncated-write case. The bucketing
            # decision stays the same (treat as missing → alert) — only
            # the diagnostic depth improves.
            logger.exception(
                "verify-cache: %s exists but unreadable; treating as missing",
                path,
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
