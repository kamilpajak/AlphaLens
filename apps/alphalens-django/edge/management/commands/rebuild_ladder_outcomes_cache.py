"""``python manage.py rebuild_ladder_outcomes_cache`` — parquet → DB sync.

Thin wrapper around ``edge.ingest.parquet.rebuild_from_parquet``. Mirrors
``rebuild_briefs_cache``: reuses the shared migration-skew guard BEFORE touching
the DB (the #331/#340 deploy-env-drift incident — a stale rebuild image whose
LadderOutcome model disagrees with the migrated schema must fail loud, not write
a silent partial cache). Runs hourly from ``alphalens-edge-mirror.service`` and
on the compute job's ``OnSuccess=`` handoff.

After the ingest the command publishes what it saw as Prometheus textfile gauges
(#1436). The unit's own ``alphalens_job_last_success_timestamp_seconds`` advances
on every exit-0 run, including a run that refused the whole store because the
nightly was killed before writing the ingest watermark (2026-09-13: ``unsettled=117``
hourly, /edge a day behind, no page). The three gauges below are what the /edge
staleness rules read instead:

- ``alphalens_edge_mirror_watermark_timestamp_seconds`` — ``completed_at`` of the
  settled watermark this run read (``0`` when there is none). Nothing newer than
  that completed compute run can be in /edge.
- ``alphalens_edge_mirror_unsettled_dates`` — dates refused this run because their
  parquet is newer than the watermark. Non-zero for one hour every morning while
  the nightly is mid-run; non-zero for hours means the nightly never completed or
  the store was rewritten outside it.
- ``alphalens_edge_mirror_newest_brief_date_timestamp_seconds`` — the newest brief
  date in the mirror's per-date ledger (``DayMetaLadderOutcome``) after this run,
  at midnight UTC (``0`` before the first ingest). The terminal state, read back
  rather than inferred from the run's own counters; the ledger rather than the
  outcome rows because a 0-candidate day ingests as a ledger row with no rows.

The names are pinned from the research side by
``tests/test_edge_mirror_metrics_parity.py`` (the rules file reads them by name).
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from briefs.migration_guard import SchemaSkewError, assert_schema_current
from edge.ingest.parquet import (
    DEFAULT_LADDER_OUTCOMES_DIR,
    RebuildResult,
    newest_mirrored_brief_date,
    rebuild_from_parquet,
)
from edge.ingest.textfile import emit_domain_metrics

logger = logging.getLogger(__name__)

# The ``job`` the unit's bash hook uses too, so the two emitters land in sibling
# files (alphalens_job_edge-mirror.prom / alphalens_domain_edge-mirror.prom).
METRICS_JOB = "edge-mirror"
WATERMARK_GAUGE = "alphalens_edge_mirror_watermark_timestamp_seconds"
UNSETTLED_GAUGE = "alphalens_edge_mirror_unsettled_dates"
NEWEST_BRIEF_DATE_GAUGE = "alphalens_edge_mirror_newest_brief_date_timestamp_seconds"


def _midnight_utc_epoch(date: dt.date) -> int:
    return int(dt.datetime.combine(date, dt.time(), tzinfo=dt.UTC).timestamp())


def mirror_gauges(result: RebuildResult, newest: dt.date | None) -> dict[str, int]:
    """The three gauges for one run. ``0`` stands in for "no watermark" and "empty
    table" so the series always exist (a missing series disarms a stale rule)."""
    return {
        WATERMARK_GAUGE: int(result.watermark) if result.watermark is not None else 0,
        UNSETTLED_GAUGE: result.n_unsettled,
        NEWEST_BRIEF_DATE_GAUGE: _midnight_utc_epoch(newest) if newest is not None else 0,
    }


class Command(BaseCommand):
    help = (
        "Rebuild the LadderOutcome / DayMetaLadderOutcome tables from "
        "population-ladder parquets. Default directory is "
        "ALPHALENS_LADDER_OUTCOMES_DIR (compose-managed in prod) or "
        "~/.alphalens/population_ladders locally; --store-dir overrides."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--store-dir",
            type=Path,
            default=DEFAULT_LADDER_OUTCOMES_DIR,
            help="Directory containing YYYY-MM-DD.parquet population-ladder files.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Ignore mtime gate; rebuild every date present.",
        )

    def handle(self, *args, **options) -> None:
        try:
            assert_schema_current()
        except SchemaSkewError as exc:
            raise CommandError(str(exc)) from exc

        result = rebuild_from_parquet(store_dir=options["store_dir"], force=options["force"])
        self.stdout.write(
            self.style.SUCCESS(
                f"rebuilt={result.n_rebuilt} skipped={result.n_skipped} "
                f"deleted={result.n_deleted} unsettled={result.n_unsettled} "
                f"total_rows={result.total_rows}"
            )
        )

        # After the ingest, never before: a metrics failure must not cost data.
        written = emit_domain_metrics(
            METRICS_JOB, mirror_gauges(result, newest_mirrored_brief_date())
        )
        if written is not None:
            logger.info("edge-mirror: wrote %s", written)
