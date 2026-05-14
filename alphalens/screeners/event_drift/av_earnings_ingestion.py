"""AV EARNINGS ingestion reader — paradigm-14 PEAD v2 A2.

Reads per-ticker JSON payloads written by ``av_earnings_client.fetch_earnings``
and yields ``AVEarningsAnnouncement`` objects suitable for cross-sectional
PSS ranking. Filters per memo §5 + plan §1.A2:

  * Drop entries with ``reportedDate`` < ``min_reported_date`` (default
    2017-06-01: one fiscal quarter buffer before the IS phase start at
    2018-01-01, so PIT calibration of the 45-day trailing cohort has a
    warm-up window before any backtest day).
  * Drop entries where ``|estimatedEPS|`` < 0.10 — price-scaled surprise
    PSS = (rEPS − eEPS) / close(t-1) explodes near zero-EPS reports
    (penny-stock noise, not statistical surprise).
  * Drop entries missing any of ``reportedDate``, ``reportedEPS``, or
    ``estimatedEPS`` (AV sometimes returns the literal string ``"None"``
    for stale historical filings).
  * Default missing ``reportTime`` to ``"post-market"`` per ledger
    ``pead_v5_pss_2026_05_13.outcome.av_pit_validation_summary.av_pit_validation_method``
    — conservative entry at close(t+1) avoids intraday lookahead.

Returns events sorted by ``reported_date`` ascending so downstream B1
cohort-rank scoring iterates forward in time without re-sorting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

ReportTime = Literal["pre-market", "post-market"]

_DEFAULT_MIN_REPORTED_DATE = date(2017, 6, 1)
_ABS_ESTIMATED_EPS_FLOOR = 0.10


@dataclass(frozen=True)
class AVEarningsAnnouncement:
    """A single PEAD-relevant earnings event sourced from Alpha Vantage.

    Distinct from ``screeners.event_drift.announcement_dates.EarningsAnnouncement``
    (EDGAR-sourced, no EPS values) because PSS requires
    ``estimated_eps`` and ``reported_eps`` at the announcement.
    """

    ticker: str
    period_end: date
    reported_date: date
    reported_eps: float
    estimated_eps: float
    report_time: ReportTime


def _parse_float(raw: object) -> float | None:
    """Convert AV's stringly-typed EPS values to float. None / 'None' / empty
    string / non-numeric all collapse to None so the eligibility filter can
    drop the entry without raising."""
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        if not s or s.lower() == "none":
            return None
        try:
            return float(s)
        except ValueError:
            return None
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_date(raw: object) -> date | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _coerce_report_time(raw: object) -> ReportTime:
    """Conservative default. AV historical (pre-2010) frequently lacks
    reportTime; defaulting to post-market shifts the engine's entry-offset
    one trading day later — net safe re: intraday-info lookahead."""
    if isinstance(raw, str) and raw.strip().lower() == "pre-market":
        return "pre-market"
    return "post-market"


def load_av_earnings(
    ticker: str,
    *,
    cache_dir: Path,
    min_reported_date: date = _DEFAULT_MIN_REPORTED_DATE,
) -> list[AVEarningsAnnouncement]:
    """Read + filter a ticker's cached AV EARNINGS payload.

    Raises ``FileNotFoundError`` if the ticker is not in the cache — the
    caller (typically a B1 cohort builder) should pre-check coverage via
    the universe-vs-cache intersection rather than silently treat a missing
    ticker as "no events".
    """
    path = cache_dir / f"earnings_{ticker.upper()}.json"
    if not path.exists():
        raise FileNotFoundError(f"AV cache miss for {ticker!r}: expected {path}")

    payload = json.loads(path.read_text())
    out: list[AVEarningsAnnouncement] = []
    for entry in payload.get("quarterlyEarnings") or []:
        reported = _parse_date(entry.get("reportedDate"))
        if reported is None or reported < min_reported_date:
            continue
        reported_eps = _parse_float(entry.get("reportedEPS"))
        estimated_eps = _parse_float(entry.get("estimatedEPS"))
        if reported_eps is None or estimated_eps is None:
            continue
        if abs(estimated_eps) < _ABS_ESTIMATED_EPS_FLOOR:
            continue
        period_end = _parse_date(entry.get("fiscalDateEnding"))
        if period_end is None:
            continue
        out.append(
            AVEarningsAnnouncement(
                ticker=ticker.upper(),
                period_end=period_end,
                reported_date=reported,
                reported_eps=reported_eps,
                estimated_eps=estimated_eps,
                report_time=_coerce_report_time(entry.get("reportTime")),
            )
        )
    out.sort(key=lambda e: e.reported_date)
    return out
