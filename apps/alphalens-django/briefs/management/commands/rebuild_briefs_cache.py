"""``python manage.py rebuild_briefs_cache`` — parquet → DB sync.

Thin wrapper around ``briefs.ingest.parquet.rebuild_from_parquet``.
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from briefs.ingest.parquet import DEFAULT_BRIEFS_DIR, rebuild_from_parquet
from briefs.migration_guard import SchemaSkewError, assert_schema_current


class Command(BaseCommand):
    help = (
        "Rebuild the Brief / DayMeta tables from thematic-brief parquets. "
        "Default directory is ALPHALENS_BRIEFS_DIR (compose-managed in prod) "
        "or ~/.alphalens/thematic_briefs locally; --briefs-dir overrides."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--briefs-dir",
            type=Path,
            default=DEFAULT_BRIEFS_DIR,
            help="Directory containing YYYY-MM-DD.parquet brief files.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Ignore mtime gate; rebuild every date present.",
        )
        parser.add_argument(
            "--prune-missing",
            action="store_true",
            help=(
                "Delete Brief rows whose parquet is gone. Default is to RETAIN "
                "them (retention guard) so maturing EDGE outcomes keep their "
                "selection-covariate join target."
            ),
        )

    def handle(self, *args, **options) -> None:
        # Refuse to run if this image's migration graph disagrees with the DB
        # schema (the #331/#340 deploy-env-drift incident). Without this the
        # mismatch surfaces as a late per-row UndefinedColumn and the 6x/day
        # rebuild dies silently. Run BEFORE touching the DB.
        try:
            assert_schema_current()
        except SchemaSkewError as exc:
            raise CommandError(str(exc)) from exc

        result = rebuild_from_parquet(
            briefs_dir=options["briefs_dir"],
            force=options["force"],
            prune_missing=options["prune_missing"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"rebuilt={result.n_rebuilt} skipped={result.n_skipped} "
                f"deleted={result.n_deleted} retained={result.n_retained} "
                f"total_briefs={result.total_briefs}"
            )
        )
