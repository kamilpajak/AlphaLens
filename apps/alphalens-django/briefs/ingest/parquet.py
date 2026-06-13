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
import os
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
    coerce_expert_blob,
    coerce_float,
    coerce_int,
    coerce_json_obj,
    coerce_list_str,
    coerce_optional_bool,
    coerce_str,
)
from briefs.models import Brief, DayMeta

logger = logging.getLogger(__name__)

# Container-vs-host trap: inside the Django container HOME=/home/django
# but the compose bind mount lands at /var/lib/alphalens/thematic_briefs.
# The legacy Path.home() default resolved to a non-existent container path
# and `rebuild_briefs_cache --force` silently DELETED every date from the
# DB. The compose now exports ALPHALENS_BRIEFS_DIR pointing at the mount
# target; locally (no env) we still fall back to the host-side default so
# dev flow works unchanged. Read at module-init so argparse's default also
# picks it up.
DEFAULT_BRIEFS_DIR = Path(
    os.environ.get("ALPHALENS_BRIEFS_DIR") or Path.home() / ".alphalens" / "thematic_briefs"
)

REQUIRED_PARQUET_COLUMNS: frozenset[str] = frozenset({"ticker", "theme"})

# JSONFields that hold a dict (parsed from a json.dumps string), NOT a list[str].
# Anything not listed here is coerced as a list of strings.
_OBJECT_JSON_FIELDS: frozenset[str] = frozenset({"brief_trade_setup", "brief_template_facts"})

# Django-local mirror of the pipeline registry's per-expert column set (slim image
# must NOT import alphalens_pipeline). Mirrors
# alphalens_pipeline.experts.buffett.expert.BuffettExpert.column_names — 6 quant +
# 5 qual-content + 3 qual-provenance, INCLUDING buffett_qual_config_version (which
# has no flat Brief field but rides inside the blob for the deferred Buffett×EDGE
# calibration corpus). The expert_assessments JSONField is ASSEMBLED from these
# flat parquet columns at ingest (PR-3); PR-5 has the pipeline emit the blob
# directly + drops the flat columns. Pinned by
# test_expert_columns_match_frozen_buffett_tuple — the only cross-boundary drift
# guard, since Django cannot import the pipeline. Adding O'Neil (PR-6) means adding
# its id + columns here AND extending that pin in lockstep with the registry.
# REMOVE in PR-5: once the pipeline emits the blob directly (and drops the flat
# buffett_* columns), this map + coerce_expert_blob + the special-case in
# _row_to_brief become dead and the ingest reads the blob column via coerce_json_obj.
_EXPERT_COLUMNS: dict[str, tuple[str, ...]] = {
    "buffett": (
        "buffett_owner_earnings_yield_pct",
        "buffett_roic_latest",
        "buffett_roic_3y_avg",
        "buffett_margin_of_safety_pct",
        "buffett_data_coverage",
        "buffett_quality_score",
        "buffett_moat_type",
        "buffett_moat_trend",
        "buffett_management_candor",
        "buffett_understandable",
        "buffett_qualitative_rationale",
        "buffett_used_scuttlebutt",
        "buffett_qual_computed_at",
        "buffett_qual_config_version",
    ),
}

# Parquet → model field renames. Pipeline persists ``brief_template_facts_json``
# (matching the source ``catalyst_template_facts_json`` column shape) but the
# Django field is named ``brief_template_facts`` since the JSON is parsed at
# ingest. Listing the rename here keeps both sides documented in one place.
_PARQUET_COLUMN_ALIASES: dict[str, str] = {
    "brief_template_facts": "brief_template_facts_json",
}

# Mtime equality tolerance: float seconds, sub-microsecond stability across
# filesystems is not guaranteed.
_MTIME_EPS = 1e-6


@dataclass(frozen=True, slots=True)
class RebuildResult:
    rebuilt_dates: tuple[dt.date, ...]
    skipped_dates: tuple[dt.date, ...]
    deleted_dates: tuple[dt.date, ...]
    total_briefs: int
    # Dates whose parquet is gone but whose Brief rows were RETAINED by the
    # retention guard (prune_missing=False, the default). Empty when prune_missing
    # is True (those dates land in deleted_dates instead).
    retained_dates: tuple[dt.date, ...] = ()

    @property
    def n_rebuilt(self) -> int:
        return len(self.rebuilt_dates)

    @property
    def n_skipped(self) -> int:
        return len(self.skipped_dates)

    @property
    def n_deleted(self) -> int:
        return len(self.deleted_dates)

    @property
    def n_retained(self) -> int:
        return len(self.retained_dates)


def _coerce_for_field(field: django_models.Field, raw):
    """Dispatch by Django field type to the matching coerce helper.

    ``bulk_create`` does NOT honour model-level defaults — every column has
    to be populated by the caller. So when a parquet row omits an optional
    column whose field is NOT NULL with a default (e.g. ``n_gates_failed``),
    we substitute the field default here rather than letting NULL violate
    the constraint.
    """
    if isinstance(field, django_models.JSONField):
        # Object-shaped JSONFields (a dict the pipeline stores as a json.dumps
        # string) go through coerce_json_obj; every other JSONField holds a
        # list[str] (gates_*, also_in_themes, …). A NEW object field must be
        # added to _OBJECT_JSON_FIELDS or it will be silently corrupted by
        # coerce_list_str (which would iterate the dict's keys).
        if field.name in _OBJECT_JSON_FIELDS:
            return coerce_json_obj(raw)
        return coerce_list_str(raw)
    if isinstance(field, django_models.DateTimeField):
        value = coerce_datetime(raw)
    elif isinstance(field, django_models.DateField):
        value = coerce_date(raw)
    elif isinstance(field, django_models.BooleanField):
        # Nullable bool (e.g. buffett_understandable) keeps the None/True/False
        # tri-state; a NOT-NULL bool floors missing -> field default (False).
        return coerce_optional_bool(raw) if field.null else coerce_bool(raw)
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
        if field.name == "expert_assessments":
            # MUST be handled before the generic dispatch: a bare JSONField not in
            # _OBJECT_JSON_FIELDS routes to coerce_list_str, which would iterate the
            # assembled dict's KEYS into a list[str] and silently corrupt the blob.
            # The blob is ASSEMBLED from the sibling flat buffett_* columns (PR-3),
            # not read from a single json.dumps cell, so it does NOT belong in
            # _OBJECT_JSON_FIELDS. PR-5 has the pipeline emit the blob directly.
            blob: dict[str, object] = {}
            for expert_id, cols in _EXPERT_COLUMNS.items():
                assessment = coerce_expert_blob(row, cols)
                if assessment is not None:
                    blob[expert_id] = assessment
            kwargs[field.name] = blob or None
            continue
        # Honour the parquet → model rename table so the source column
        # name can differ from the Django field name (e.g. PR-3 ingests
        # parquet's brief_template_facts_json into model's brief_template_facts).
        parquet_col = _PARQUET_COLUMN_ALIASES.get(field.name, field.name)
        raw = row.get(parquet_col) if parquet_col in row.index else None
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
    prune_missing: bool = False,
) -> RebuildResult:
    """Bring the DB in line with ``briefs_dir`` (one parquet file per date).

    ``prune_missing`` (PR-5 retention guard, default False): when a date's parquet
    is GONE from ``briefs_dir``, the default is to RETAIN its Brief rows rather
    than cascade-delete them. Those rows are the join target for maturing EDGE
    outcomes (the selection covariates a tuning analyst needs), so a transient or
    accidental parquet removal must not silently destroy them. Pass
    ``prune_missing=True`` to restore the old delete behaviour. In steady state the
    parquet dir is append-only, so the missing set is normally empty and this flag
    is a no-op.
    """
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

    missing = sorted(set(stored_mtimes) - set(parquet_by_date))
    deleted: list[dt.date] = []
    retained: list[dt.date] = []
    if missing and prune_missing:
        with transaction.atomic():
            Brief.objects.filter(date__in=missing).delete()
            DayMeta.objects.filter(date__in=missing).delete()
        deleted = missing
        for d in missing:
            logger.info("ingest: dropped %s (parquet missing, prune_missing=True)", d.isoformat())
    elif missing:
        # Retention guard: keep the Brief rows so maturing EDGE outcomes keep
        # their selection-covariate join target. Loud so an operator notices a
        # parquet that genuinely should be pruned (pass prune_missing=True then).
        retained = missing
        logger.warning(
            "ingest: %d date(s) have no parquet; RETAINING their Brief rows "
            "(retention guard). Pass prune_missing=True to delete: %s",
            len(missing),
            [d.isoformat() for d in missing],
        )

    return RebuildResult(
        rebuilt_dates=tuple(rebuilt),
        skipped_dates=tuple(skipped),
        deleted_dates=tuple(deleted),
        total_briefs=total,
        retained_dates=tuple(retained),
    )
