"""Subtractive teardown for the removed feedback ``decisions`` table.

The Track-A user-action click ledger was removed (#465). No code writes the
``decisions`` table any more, and the per-decision ladder replay that read it
was removed too. The whole ``store`` subsystem is gone — nothing opens
``~/.alphalens/feedback.db`` at runtime, so a stale file is simply orphaned and
harmless.

This module is the OPERATOR teardown: an explicit, idempotent helper that drops
the dead ``decisions`` table (and its indexes) from a populated legacy
``feedback.db`` so the orphaned file does not keep dead historical click rows
around. It is NOT wired into any hot path — the runtime no longer opens the file
at all — and is safe to run zero, one, or many times.

Safety contract (the LIVE feedback is untouched):
    The live market-behavior feedback is the broker-free POPULATION ladder
    monitor, which reads briefs + Polygon and writes the SEPARATE
    ``~/.alphalens/population_ladders/*.parquet`` files. Those parquets are
    independent files — this teardown only opens ``feedback.db`` and only ever
    issues ``DROP TABLE IF EXISTS decisions`` (+ its indexes). It never reads,
    writes, or even references the population parquets, so the live edge signal
    cannot be affected. Losing the dead historical decision rows is expected and
    acceptable; the parquets carry all the live data.

Idempotency / robustness:
    * ``DROP TABLE IF EXISTS`` is a no-op when the table is already gone (a
      fresh or already-migrated db), so re-running never raises "no such table".
    * A missing ``feedback.db`` file is a no-op (returns ``False``) — there is
      nothing to tear down.
    * ``DROP TABLE`` is supported on every SQLite version (unlike
      ``ALTER TABLE DROP COLUMN``, which needs 3.35+), so there is no VPS
      version risk: this works regardless of the host SQLite build.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# The dead table + its indexes, dropped by :func:`drop_decisions_table`. Listed
# explicitly so the teardown is self-documenting and leaves no dangling index.
_DEAD_TABLE = "decisions"
_DEAD_INDEXES = (
    "idx_decisions_brief_date",
    "idx_decisions_ticker",
    "idx_decisions_action",
)


def drop_decisions_table(feedback_path: Path) -> bool:
    """Idempotently drop the dead ``decisions`` table from a legacy feedback.db.

    Returns ``True`` when the file existed and the teardown ran (whether or not
    the table was actually present), ``False`` when there was no file to touch.
    Never raises on a fresh / already-migrated / missing database.

    NEVER touches the population-ladder parquets — they are separate files and
    are the sole source of the live edge signal (see module docstring).
    """
    feedback_path = Path(feedback_path)
    if not feedback_path.exists():
        logger.info("feedback teardown: %s does not exist — nothing to drop.", feedback_path)
        return False

    # isolation_level=None -> autocommit; each DDL statement commits on its own.
    conn = sqlite3.connect(str(feedback_path), isolation_level=None)
    try:
        for index_name in _DEAD_INDEXES:
            # Index/table names are module constants, never user input.
            conn.execute(f"DROP INDEX IF EXISTS {index_name}")
        conn.execute(f"DROP TABLE IF EXISTS {_DEAD_TABLE}")
    finally:
        conn.close()
    logger.info("feedback teardown: dropped dead %r table from %s.", _DEAD_TABLE, feedback_path)
    return True


__all__ = ["drop_decisions_table"]
