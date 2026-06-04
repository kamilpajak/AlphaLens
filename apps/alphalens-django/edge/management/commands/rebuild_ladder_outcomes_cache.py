"""``python manage.py rebuild_ladder_outcomes_cache`` — parquet → DB sync.

Thin wrapper around ``edge.ingest.parquet.rebuild_from_parquet``. Mirrors
``rebuild_briefs_cache``: reuses the shared migration-skew guard BEFORE touching
the DB (the #331/#340 deploy-env-drift incident — a stale rebuild image whose
LadderOutcome model disagrees with the migrated schema must fail loud, not write
a silent partial cache). NOT wired to a new systemd unit here — that is a deploy
step, out of scope for this PR.
"""

from __future__ import annotations

from pathlib import Path

from briefs.migration_guard import SchemaSkewError, assert_schema_current
from django.core.management.base import BaseCommand, CommandError

from edge.ingest.parquet import DEFAULT_LADDER_OUTCOMES_DIR, rebuild_from_parquet


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
                f"deleted={result.n_deleted} total_rows={result.total_rows}"
            )
        )
