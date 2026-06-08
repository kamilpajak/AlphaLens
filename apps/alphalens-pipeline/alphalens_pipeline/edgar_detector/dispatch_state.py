"""Calendar-aware no-dispatch state for the edgar-detect cron.

The edgar-detect cron emits a gauge
``alphalens_edgar_trading_days_since_last_dispatch`` on every run so a
Prometheus rule can page when the watchlist has gone quiet for a full trading
week. PromQL cannot consult a holiday calendar, so the calendar math lives here
(Python, where ``exchange_calendars`` ships the session table via
``alphalens_pipeline.paper.calendar``).

Two halves:

* the durable single-field state ``last_dispatch_date`` persisted as ISO date
  in ``<home>/dispatch_state.json`` — a tiny JSON file mirroring the atomic
  ``os.replace`` write pattern of ``observability/textfile.py``. A new JSON
  file (not ``seen_events.db`` / ``digest.db``, which carry purpose-specific
  schemas, nor user-owned ``portfolio.yaml``) keeps concerns separated.
* the pure, exchange-aware gap computation built on
  ``paper.calendar.is_trading_day`` so it is unit-testable in isolation.

The ``alphalens_pipeline.edgar_detector -> alphalens_pipeline.paper.calendar``
import is intra-pipeline (pipeline -> pipeline), so it does not cross the
workspace DAG boundary the dependency-direction guard enforces (that guard only
forbids ``alphalens_pipeline.* -> alphalens_research.*`` at top level).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
from pathlib import Path

from alphalens_pipeline.paper.calendar import DEFAULT_EXCHANGE, is_trading_day

logger = logging.getLogger(__name__)

_STATE_FILENAME = "dispatch_state.json"
_LAST_DISPATCH_KEY = "last_dispatch_date"


def state_path(home: Path) -> Path:
    """Path to the dispatch-state JSON file under the edgar-detect home dir."""
    return home / _STATE_FILENAME


def load_last_dispatch_date(home: Path) -> dt.date | None:
    """Return the persisted ``last_dispatch_date`` or ``None`` if absent.

    A missing or corrupt file is treated as cold start (``None``) — the cron
    must never crash on a truncated state file; the next dispatch re-stamps it
    cleanly.
    """
    path = state_path(home)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload[_LAST_DISPATCH_KEY]
        return dt.date.fromisoformat(raw)
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError, OSError) as exc:
        logger.warning("dispatch_state.json unreadable (%s); treating as cold start", exc)
        return None


def stamp_last_dispatch_date(home: Path, date: dt.date) -> Path:
    """Persist ``last_dispatch_date`` = ``date`` (ISO string) atomically.

    Tempfile-in-same-dir + ``os.replace`` so a concurrent read never sees a
    half-written file — the same atomic-write idiom as
    ``observability/textfile.py``.
    """
    home.mkdir(parents=True, exist_ok=True)
    target = state_path(home)
    payload = {_LAST_DISPATCH_KEY: date.isoformat()}
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=home,
        delete=False,
        suffix=".tmp",
        encoding="utf-8",
    ) as tmp:
        json.dump(payload, tmp)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, target)
    return target


def trading_days_between(
    start_exclusive: dt.date,
    end_exclusive: dt.date,
    exchange: str = DEFAULT_EXCHANGE,
) -> int:
    """Count exchange sessions in the OPEN interval ``(start, end)``.

    Both endpoints are excluded. A pure weekend gap (Fri -> Mon) holds no
    session -> 0; a holiday in the interval never counts because
    ``is_trading_day`` returns False for it. Returns 0 when ``end_exclusive``
    is not strictly after ``start_exclusive`` (clamped, never negative).
    """
    if end_exclusive <= start_exclusive:
        return 0
    count = 0
    day = start_exclusive + dt.timedelta(days=1)
    while day < end_exclusive:
        if is_trading_day(day, exchange):
            count += 1
        day += dt.timedelta(days=1)
    return count


def compute_trading_days_since_dispatch(
    last_dispatch_date: dt.date | None,
    today: dt.date,
    exchange: str = DEFAULT_EXCHANGE,
) -> int:
    """Gauge value: trading days elapsed since the last dispatch, today excluded.

    * Cold start (``last_dispatch_date is None``) -> 0; never emit a huge value
      on a fresh deploy.
    * A dispatch run sets ``last_dispatch_date == today`` -> 0.
    * Otherwise count sessions strictly between ``last_dispatch_date`` and
      ``today`` (both excluded). ``today`` is excluded because a dispatch may
      still arrive later today; ``last_dispatch_date`` itself is excluded
      because that is the day the dispatch happened.
    """
    if last_dispatch_date is None:
        return 0
    return trading_days_between(last_dispatch_date, today, exchange)
