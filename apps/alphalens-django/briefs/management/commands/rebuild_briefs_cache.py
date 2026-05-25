"""``python manage.py rebuild_briefs_cache`` — parquet → DB sync.

Thin wrapper around ``briefs.ingest.parquet.rebuild_from_parquet``.
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand

from briefs.ingest.parquet import DEFAULT_BRIEFS_DIR, rebuild_from_parquet


class Command(BaseCommand):
    help = "Rebuild the Brief / DayMeta tables from ~/.alphalens/thematic_briefs/*.parquet."

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

    def handle(self, *args, **options) -> None:
        result = rebuild_from_parquet(briefs_dir=options["briefs_dir"], force=options["force"])
        self.stdout.write(
            self.style.SUCCESS(
                f"rebuilt={result.n_rebuilt} skipped={result.n_skipped} "
                f"deleted={result.n_deleted} total_briefs={result.total_briefs}"
            )
        )
