"""Rate-limited reclaim of the elevated Saxo LIVE session.

Only ONE session per user may hold the elevated capability (verified
empirically 2026-08-07: the operator logging into SaxoTraderGO dropped the API
session to OrdersOnly and its prices to 15 minutes old). SaxoTraderGO shows the
loser a banner with a resume button, so a reclaim never leaves the operator
confused - but that button means an unlimited reclaim would ping-pong forever.

The budget makes the outcome fair: the unattended daemon wins by default, and an
operator who keeps pressing resume wins by persistence.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import deque
from collections.abc import Callable

logger = logging.getLogger(__name__)

DEFAULT_MAX_PER_HOUR = 4
_WINDOW = dt.timedelta(hours=1)


class ReclaimLimiter:
    def __init__(
        self,
        *,
        max_per_hour: int = DEFAULT_MAX_PER_HOUR,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._max = max_per_hour
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self._attempts: deque[dt.datetime] = deque()

    def try_reclaim(self, elevate: Callable[[], bool]) -> str:
        now = self._clock()
        while self._attempts and now - self._attempts[0] > _WINDOW:
            self._attempts.popleft()
        if len(self._attempts) >= self._max:
            return "budget-exhausted"
        # Recorded BEFORE the outcome: a failing elevation must consume budget
        # too, or a broken session retries without limit.
        self._attempts.append(now)
        if elevate():
            logger.info(
                "Saxo LIVE session reclaimed (%s/%s this hour)", len(self._attempts), self._max
            )
            return "reclaimed"
        logger.warning("Saxo LIVE session reclaim attempt failed")
        return "failed"
