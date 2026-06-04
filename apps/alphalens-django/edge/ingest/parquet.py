"""Parquet → ORM ingest for the population-ladder outcome cache.

Mirrors ``briefs.ingest.parquet`` exactly: one parquet per brief date
(``population_ladders/2026-05-27.parquet``), a per-date ``parquet_mtime`` gate
for incremental rebuilds, and an atomic delete-then-bulk_create per date.

Source of the parquets: the broker-free
``alphalens_pipeline.feedback.population_ladder_monitor`` (every plannable
candidate, automatic, full population), with the two benchmark-excess columns
written by ``alphalens_pipeline.feedback.benchmark_excess`` (computed in the
PIPELINE container — the slim Django image has no Polygon / calendar; see that
module's docstring). Older parquets that predate the size / benchmark columns
ingest those columns as NULL, exactly the way the briefs ingest tolerates a
missing column.

Reuses the briefs coerce helpers (pure stdlib, no Django / pipeline import) so
the slim image needs nothing new.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from briefs.ingest.coerce import (
    coerce_bool,
    coerce_date,
    coerce_float,
    coerce_int,
    coerce_str,
)
from django.db import models as django_models
from django.db import transaction
from django.utils import timezone

from edge.models import DayMetaLadderOutcome, LadderOutcome

logger = logging.getLogger(__name__)

# Same container-vs-host HOME trap as the briefs ingest: inside the Django
# container HOME=/home/django but the compose bind mount lands elsewhere, so the
# directory MUST be settable via env in prod. Locally (no env) fall back to the
# host-side default so dev flow works unchanged.
DEFAULT_LADDER_OUTCOMES_DIR = Path(
    os.environ.get("ALPHALENS_LADDER_OUTCOMES_DIR")
    or Path.home() / ".alphalens" / "population_ladders"
)

REQUIRED_PARQUET_COLUMNS: frozenset[str] = frozenset({"brief_date", "ticker"})

# Mtime equality tolerance: float seconds, sub-microsecond stability across
# filesystems is not guaranteed.
_MTIME_EPS = 1e-6


@dataclass(frozen=True, slots=True)
class RebuildResult:
    rebuilt_dates: tuple[dt.date, ...]
    skipped_dates: tuple[dt.date, ...]
    deleted_dates: tuple[dt.date, ...]
    total_rows: int

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

    ``bulk_create`` does NOT honour model-level defaults, so every column has to
    be populated by the caller; a NULL on a NOT-NULL-with-default field
    (``plannable`` / ``terminal``) is substituted with the field default.
    """
    if isinstance(field, django_models.BooleanField):
        return coerce_bool(raw)
    if isinstance(field, django_models.DateField) and not isinstance(
        field, django_models.DateTimeField
    ):
        value = coerce_date(raw)
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


# LadderOutcome fields populated by parquet rows. Skip the synthetic composite-pk
# descriptor and ``brief_date`` (assigned from the file stem, NOT the row, so a
# mislabeled in-row date can never split a date across two files).
def _payload_fields() -> list[django_models.Field]:
    skip = {"pk", "brief_date"}
    return [
        f
        for f in LadderOutcome._meta.get_fields()
        if isinstance(f, django_models.Field) and f.name not in skip
    ]


def _row_to_outcome(
    brief_date: dt.date, row: pd.Series, fields: Iterable[django_models.Field]
) -> LadderOutcome:
    kwargs: dict[str, object] = {"brief_date": brief_date}
    for field in fields:
        raw = row.get(field.name) if field.name in row.index else None
        kwargs[field.name] = _coerce_for_field(field, raw)
    return LadderOutcome(**kwargs)


def _scan_parquets(store_dir: Path) -> dict[dt.date, Path]:
    """Map every ``YYYY-MM-DD.parquet`` in ``store_dir`` to its date.

    Files whose stem is not a valid ISO date are logged and skipped (e.g. the
    per-(ticker, arrival) bar cache lives in a ``bars/`` subdir, not here).
    """
    out: dict[dt.date, Path] = {}
    if not store_dir.exists():
        return out
    for path in sorted(store_dir.glob("*.parquet")):
        try:
            d = dt.date.fromisoformat(path.stem)
        except ValueError:
            logger.warning("ladder-ingest: skipping parquet with non-ISO stem: %s", path.name)
            continue
        out[d] = path
    return out


def _stored_mtimes() -> dict[dt.date, float]:
    return {
        d: float(m)
        for d, m in DayMetaLadderOutcome.objects.values_list("brief_date", "parquet_mtime")
    }


@transaction.atomic
def _rebuild_one_date(*, date: dt.date, parquet_path: Path, mtime: float, now: dt.datetime) -> int:
    df = pd.read_parquet(parquet_path)
    missing = REQUIRED_PARQUET_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"parquet {parquet_path.name} missing required columns: {sorted(missing)}")

    fields = _payload_fields()
    rows = [_row_to_outcome(date, row, fields) for _, row in df.iterrows()]

    LadderOutcome.objects.filter(brief_date=date).delete()
    if rows:
        LadderOutcome.objects.bulk_create(rows)

    n_plannable = int(df["plannable"].fillna(False).astype(bool).sum()) if "plannable" in df else 0
    n_terminal = int(df["terminal"].fillna(False).astype(bool).sum()) if "terminal" in df else 0
    DayMetaLadderOutcome.objects.update_or_create(
        brief_date=date,
        defaults={
            "n_rows": len(df),
            "n_plannable": n_plannable,
            "n_terminal": n_terminal,
            "parquet_mtime": mtime,
            "rebuilt_at": now,
        },
    )
    return len(rows)


def rebuild_from_parquet(
    store_dir: Path | str | None = None,
    *,
    force: bool = False,
) -> RebuildResult:
    """Bring the DB in line with ``store_dir`` (one parquet file per brief date)."""
    resolved = Path(store_dir) if store_dir is not None else DEFAULT_LADDER_OUTCOMES_DIR

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
        logger.info("ladder-ingest: rebuilt %s (%d rows)", date.isoformat(), n)

    deleted = sorted(set(stored_mtimes) - set(parquet_by_date))
    if deleted:
        with transaction.atomic():
            LadderOutcome.objects.filter(brief_date__in=deleted).delete()
            DayMetaLadderOutcome.objects.filter(brief_date__in=deleted).delete()
        for d in deleted:
            logger.info("ladder-ingest: dropped %s (parquet missing)", d.isoformat())

    return RebuildResult(
        rebuilt_dates=tuple(rebuilt),
        skipped_dates=tuple(skipped),
        deleted_dates=tuple(deleted),
        total_rows=total,
    )
