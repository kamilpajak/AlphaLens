"""Parquet → ORM ingest.

``rebuild_from_parquet(briefs_dir)`` mirrors the directory of daily brief
parquets into Postgres (or SQLite, in dev). Each parquet file's stem is the
ISO date (``2026-05-22.parquet`` → ``date(2026, 5, 22)``). A per-date mtime
gate via ``DayMeta.parquet_mtime`` keeps repeat rebuilds incremental.

Within one date:

1. Read parquet → DataFrame.
2. Validate required columns (``ticker``, ``theme``).
3. Inside an atomic block: delete existing ``Brief`` rows for the date, then
   ``bulk_create`` the fresh batch, then ``update_or_create`` ``DayMeta``.

Mismatches between the parquet column set and the ``Brief`` model are
tolerated: unknown parquet columns are dropped, missing model columns get
their default value. Older parquet files (pre-2024) lack ``catalyst_*``,
``also_in_themes`` etc. — by design they ingest with NULL/[] for those.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from django.db import models as django_models
from django.db import transaction
from django.utils import timezone

from briefs.ingest.coerce import (
    coerce_bool,
    coerce_date,
    coerce_datetime,
    coerce_float,
    coerce_int,
    coerce_json_obj,
    coerce_list_str,
    coerce_str,
)
from briefs.models import Brief, DayMeta

logger = logging.getLogger(__name__)

DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"

REQUIRED_PARQUET_COLUMNS: frozenset[str] = frozenset({"ticker", "theme"})

# Mtime equality tolerance: float seconds, sub-microsecond stability across
# filesystems is not guaranteed.
_MTIME_EPS = 1e-6


@dataclass(frozen=True, slots=True)
class RebuildResult:
    rebuilt_dates: tuple[dt.date, ...]
    skipped_dates: tuple[dt.date, ...]
    deleted_dates: tuple[dt.date, ...]
    total_briefs: int

    @property
    def n_rebuilt(self) -> int:
        return len(self.rebuilt_dates)

    @property
    def n_skipped(self) -> int:
        return len(self.skipped_dates)

    @property
    def n_deleted(self) -> int:
        return len(self.deleted_dates)


def _coerce_for_field(field: django_models.Field, raw):
    """Dispatch by Django field type to the matching coerce helper.

    ``bulk_create`` does NOT honour model-level defaults — every column has
    to be populated by the caller. So when a parquet row omits an optional
    column whose field is NOT NULL with a default (e.g. ``n_gates_failed``),
    we substitute the field default here rather than letting NULL violate
    the constraint.
    """
    if isinstance(field, django_models.JSONField):
        # brief_trade_setup is an OBJECT JSONField (a dict the pipeline stores as
        # a json.dumps string); every other JSONField holds a list[str].
        if field.name == "brief_trade_setup":
            return coerce_json_obj(raw)
        return coerce_list_str(raw)
    if isinstance(field, django_models.DateTimeField):
        value = coerce_datetime(raw)
    elif isinstance(field, django_models.DateField):
        value = coerce_date(raw)
    elif isinstance(field, django_models.BooleanField):
        return coerce_bool(raw)
    elif isinstance(field, django_models.FloatField):
        value = coerce_float(raw)
    elif isinstance(field, django_models.IntegerField):
        value = coerce_int(raw)
    else:
        text = coerce_str(raw)
        return text if text is not None else ""

    if value is None and not field.null:
        return field.get_default()
    return value


# Brief fields populated by parquet rows. We skip the synthetic composite-pk
# descriptor and ``date`` (assigned from the file stem, not the row).
def _payload_fields() -> list[django_models.Field]:
    skip = {"pk", "date"}
    return [
        f
        for f in Brief._meta.get_fields()
        if isinstance(f, django_models.Field) and f.name not in skip
    ]


def _row_to_brief(date: dt.date, row: pd.Series, fields: Iterable[django_models.Field]) -> Brief:
    kwargs: dict[str, object] = {"date": date}
    for field in fields:
        raw = row.get(field.name) if field.name in row.index else None
        kwargs[field.name] = _coerce_for_field(field, raw)
    return Brief(**kwargs)


def _theme_counts(df: pd.DataFrame) -> dict[str, int]:
    if "theme" not in df.columns or df.empty:
        return {}
    return {str(k): int(v) for k, v in df["theme"].value_counts().to_dict().items()}


def _top_theme(theme_counts: dict[str, int]) -> str:
    if not theme_counts:
        return ""
    return min(theme_counts.keys(), key=lambda k: (-theme_counts[k], k))


def _scan_parquets(briefs_dir: Path) -> dict[dt.date, Path]:
    """Map every ``YYYY-MM-DD.parquet`` in ``briefs_dir`` to its date.

    Files whose stem is not a valid ISO date are logged and skipped — they
    are probably stale debug dumps, not briefs.
    """
    out: dict[dt.date, Path] = {}
    if not briefs_dir.exists():
        return out
    for path in sorted(briefs_dir.glob("*.parquet")):
        try:
            d = dt.date.fromisoformat(path.stem)
        except ValueError:
            logger.warning("ingest: skipping parquet with non-ISO stem: %s", path.name)
            continue
        out[d] = path
    return out


def _stored_mtimes() -> dict[dt.date, float]:
    return {d: float(m) for d, m in DayMeta.objects.values_list("date", "parquet_mtime")}


@transaction.atomic
def _rebuild_one_date(*, date: dt.date, parquet_path: Path, mtime: float, now: dt.datetime) -> int:
    df = pd.read_parquet(parquet_path)
    missing = REQUIRED_PARQUET_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"parquet {parquet_path.name} missing required columns: {sorted(missing)}")

    fields = _payload_fields()
    briefs = [_row_to_brief(date, row, fields) for _, row in df.iterrows()]

    Brief.objects.filter(date=date).delete()
    if briefs:
        Brief.objects.bulk_create(briefs)

    counts = _theme_counts(df)
    DayMeta.objects.update_or_create(
        date=date,
        defaults={
            "n_candidates": len(df),
            "n_themes": len(counts),
            "top_theme": _top_theme(counts),
            "theme_counts": counts,
            "parquet_mtime": mtime,
            "rebuilt_at": now,
        },
    )
    return len(briefs)


def rebuild_from_parquet(
    briefs_dir: Path | str | None = None,
    *,
    force: bool = False,
) -> RebuildResult:
    """Bring the DB in line with ``briefs_dir`` (one parquet file per date)."""
    resolved = Path(briefs_dir) if briefs_dir is not None else DEFAULT_BRIEFS_DIR

    parquet_by_date = _scan_parquets(resolved)
    stored_mtimes = _stored_mtimes()

    rebuilt: list[dt.date] = []
    skipped: list[dt.date] = []
    total = 0
    now = timezone.now()

    for date in sorted(parquet_by_date):
        parquet_path = parquet_by_date[date]
        mtime = parquet_path.stat().st_mtime
        if not force and abs(stored_mtimes.get(date, -1.0) - mtime) < _MTIME_EPS:
            skipped.append(date)
            continue
        n = _rebuild_one_date(date=date, parquet_path=parquet_path, mtime=mtime, now=now)
        rebuilt.append(date)
        total += n
        logger.info("ingest: rebuilt %s (%d rows)", date.isoformat(), n)

    deleted = sorted(set(stored_mtimes) - set(parquet_by_date))
    if deleted:
        with transaction.atomic():
            Brief.objects.filter(date__in=deleted).delete()
            DayMeta.objects.filter(date__in=deleted).delete()
        for d in deleted:
            logger.info("ingest: dropped %s (parquet missing)", d.isoformat())

    return RebuildResult(
        rebuilt_dates=tuple(rebuilt),
        skipped_dates=tuple(skipped),
        deleted_dates=tuple(deleted),
        total_briefs=total,
    )
