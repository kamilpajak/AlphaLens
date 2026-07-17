"""Loader + contract validator for the betlejem broker-fills export (broker-fills-v1).

The betlejem paper-trading engine (a friend's IBKR-paper stack) exports its
closed-trade journal as a full-history snapshot parquet, delivered by manual /
cron rsync into ``~/.alphalens/broker_fills/`` (dir env-overridable via
``ALPHALENS_BROKER_FILLS_DIR``, mirroring ``ALPHALENS_LADDER_OUTCOMES_DIR``).
One file PER EXPORT RUN: ``broker-fills-<YYYYMMDDTHHMMSSZ>.parquet``; the
lexically-latest file WINS, older files are prunable garbage and are never
merged.

This module is deliberately LOADER + VALIDATION ONLY. The selection A/B on this
data is pre-registered as Cluster #22 in
``docs/research/edge_hypothesis_budget_2026_07.md`` with a HARD floor of N >= 30
closed POST_C1612 trades PER ARM — no statistics function exists here, so the
look cannot be computed early by accident. Full contract (schema table, privacy
rules, R definition): ``docs/research/broker_fills_export_design_2026_07_17.md``.

Privacy defense in depth: the exporter computes every ratio at source so no
share count, notional, absolute PnL, account value, or broker order id ever
leaves the friend's machine — and this validator REJECTS any delivered file
that carries a forbidden column name anyway, so a mis-built export fails loud
instead of ingesting private data.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from alphalens_pipeline.paper.calendar import DEFAULT_EXCHANGE, session_on_or_after

logger = logging.getLogger(__name__)

#: The one schema this loader understands. Bumped exporter-side only on column
#: add/remove or a semantic change of an existing column, never on value drift.
SCHEMA_VERSION = "broker-fills-v1"

#: Full broker-fills-v1 column list, in contract order. Pinned here so the
#: loader, the tests, and the design memo cannot drift apart silently.
BROKER_FILLS_V1_COLUMNS: tuple[str, ...] = (
    "schema_version",
    "fills_source_version",
    "export_run_ts_utc",
    "trade_id_hash",
    "ticker",
    "market",
    "side",
    "strategy",
    "scanner_sources",
    "source_claims",
    "provenance_cohort",
    "fill_ts_utc",
    "close_ts_utc",
    "holding_seconds",
    "close_reason",
    "entry_price",
    "close_price",
    "close_price_is_trigger",
    "stop_loss_pct",
    "take_profit_pct",
    "realized_r",
    "pnl_pct_of_notional",
    "pnl_pct_basis",
    "commission_pct_of_notional",
    "commission_is_modeled",
    "entry_fill_vs_thesis_spot_bps",
    "joined_streams",
    "record_error",
)

#: Hard floor — a delivered file missing any of these is rejected outright.
REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "trade_id_hash",
        "ticker",
        "side",
        "close_reason",
        "provenance_cohort",
        "schema_version",
    }
)

#: Privacy tripwire — exact (case-insensitive) column names that must NEVER
#: appear in a delivered file. These are the private betlejem journal fields
#: (share counts, notionals, absolute PnL, account values, broker order ids)
#: that the exporter consumes only as intermediates. Substring matching is
#: deliberately NOT used: ``pnl_pct_of_notional`` legitimately contains
#: "notional" — the ratio is scale-free, the raw denominator is not.
FORBIDDEN_COLUMNS: frozenset[str] = frozenset(
    {
        "quantity",
        "qty",
        "shares",
        "notional",
        "notional_usd",
        "target_notional",
        "target_notional_usd",
        "realized_pnl",
        "realized_pnl_usd",
        "pnl",
        "pnl_usd",
        "commission",
        "commission_usd",
        "nlv",
        "ledger_nlv_before",
        "ledger_nlv_after",
        "account",
        "account_currency",
        "account_value",
        "position_value",
        "order_id",
        "stp_order_id",
        "tp_order_id",
    }
)

# Provenance cohort enum (derived exporter-side; normalized here when absent).
PROVENANCE_POST_C1612 = "POST_C1612"
PROVENANCE_PRE_C1612 = "PRE_C1612"
PROVENANCE_NO_ENTRY_RECORD = "NO_ENTRY_RECORD"
PROVENANCE_COHORTS: frozenset[str] = frozenset(
    {PROVENANCE_POST_C1612, PROVENANCE_PRE_C1612, PROVENANCE_NO_ENTRY_RECORD}
)

_JOINED_OUTCOMES_ONLY = "OUTCOMES_ONLY"

#: Delivery directory. Env override matters for container/host HOME mismatches
#: and operator-side test deliveries (same pattern as ALPHALENS_LADDER_OUTCOMES_DIR).
DEFAULT_BROKER_FILLS_DIR = Path(
    os.environ.get("ALPHALENS_BROKER_FILLS_DIR") or Path.home() / ".alphalens" / "broker_fills"
)

_SNAPSHOT_GLOB = "broker-fills-*.parquet"


class BrokerFillsContractError(ValueError):
    """A delivered broker-fills file violates the pinned broker-fills-v1 contract."""


def latest_snapshot_path(fills_dir: Path | None = None) -> Path | None:
    """The lexically-latest ``broker-fills-*.parquet`` in ``fills_dir``, or ``None``.

    Reader contract: filenames embed the UTC export-run timestamp, so lexical
    order IS generation order; the latest full-history snapshot supersedes all
    older files (they are never merged).
    """
    directory = Path(fills_dir) if fills_dir is not None else DEFAULT_BROKER_FILLS_DIR
    if not directory.is_dir():
        return None
    snapshots = sorted(directory.glob(_SNAPSHOT_GLOB))
    return snapshots[-1] if snapshots else None


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame({col: pd.Series(dtype=object) for col in BROKER_FILLS_V1_COLUMNS})


def _has_value(val: object) -> bool:
    """True when a cell carries a real value (list-typed cells count as present)."""
    if val is None:
        return False
    if isinstance(val, (list, tuple, np.ndarray)):
        # Empty list is still a VALUE (post-C1612 "genuinely no sources");
        # only null means "key absent from the source record".
        return True
    try:
        # cast: pd.isna's overloads want a Scalar, but this helper's whole job
        # is classifying arbitrary cells — the except arm handles the rest.
        return not bool(pd.isna(cast("Any", val)))
    except (TypeError, ValueError):
        return True


def _derive_provenance_cohort(row: pd.Series) -> str:
    """Cohort for a row whose exporter left ``provenance_cohort`` null.

    Mirrors the exporter-side derivation: no thesis join at all means no ENTRY
    record; a present provenance key (null-vs-empty distinction preserved —
    empty list counts as present) means post-C1612; otherwise the ENTRY line
    predates the provenance keys.
    """
    if str(row.get("joined_streams") or "") == _JOINED_OUTCOMES_ONLY:
        return PROVENANCE_NO_ENTRY_RECORD
    if _has_value(row.get("scanner_sources")) or _has_value(row.get("source_claims")):
        return PROVENANCE_POST_C1612
    return PROVENANCE_PRE_C1612


def _normalize_provenance_cohort(df: pd.DataFrame, *, source: str) -> pd.DataFrame:
    """Fill null cohorts via :func:`_derive_provenance_cohort`; reject unknown values."""
    normalized: list[str] = []
    for _, row in df.iterrows():
        raw = row["provenance_cohort"]
        if not _has_value(raw) or str(raw).strip() == "":
            normalized.append(_derive_provenance_cohort(row))
            continue
        value = str(raw)
        if value not in PROVENANCE_COHORTS:
            raise BrokerFillsContractError(
                f"{source}: unknown provenance_cohort {value!r} — expected one of "
                f"{sorted(PROVENANCE_COHORTS)}"
            )
        normalized.append(value)
    out = df.copy()
    out["provenance_cohort"] = normalized
    return out


def validate_broker_fills(df: pd.DataFrame, *, source: str = "<in-memory>") -> pd.DataFrame:
    """Validate a broker-fills frame against the pinned v1 contract.

    Returns a normalized copy (columns in contract order, missing nullable
    columns back-filled to ``None``, null ``provenance_cohort`` cells resolved
    to an explicit cohort). Raises :class:`BrokerFillsContractError` loudly on
    any contract breach — a bad file must never half-ingest.
    """
    columns = list(df.columns)

    # 1. Privacy tripwire FIRST — reject before touching any values.
    forbidden_hits = sorted({c for c in columns if c.lower() in FORBIDDEN_COLUMNS})
    if forbidden_hits:
        raise BrokerFillsContractError(
            f"{source}: file carries forbidden private column(s) {forbidden_hits} — "
            "the export contract is scale-free ratios only; refusing to ingest"
        )

    # 2. Required-column floor.
    missing_required = sorted(REQUIRED_COLUMNS - set(columns))
    if missing_required:
        raise BrokerFillsContractError(f"{source}: missing required column(s) {missing_required}")

    # 3. Schema-version gate — refuse unknown generations loudly (a future v2
    #    must be handled by a deliberate loader change, never silently).
    if df["schema_version"].isna().any():
        raise BrokerFillsContractError(f"{source}: null schema_version cell(s)")
    versions = {str(v) for v in df["schema_version"].unique()}
    unknown_versions = versions - {SCHEMA_VERSION}
    if unknown_versions:
        raise BrokerFillsContractError(
            f"{source}: unknown schema_version(s) {sorted(unknown_versions)} — "
            f"this loader understands only {SCHEMA_VERSION!r}"
        )

    # 4. No unknown columns under v1: the contract bumps schema_version on any
    #    column add, so an unrecognized column under v1 is a contract breach
    #    (and a privacy risk the tripwire list may not know by name yet).
    unknown_columns = sorted(set(columns) - set(BROKER_FILLS_V1_COLUMNS))
    if unknown_columns:
        raise BrokerFillsContractError(
            f"{source}: unknown column(s) {unknown_columns} under {SCHEMA_VERSION} — "
            "a column add requires a schema_version bump"
        )

    # 5. Dedup key: one row per closed trade, exporter-enforced; double-check.
    duplicated = df["trade_id_hash"].duplicated()
    if bool(duplicated.any()):
        dupes = sorted(set(df.loc[duplicated, "trade_id_hash"].astype(str)))
        raise BrokerFillsContractError(
            f"{source}: duplicate trade_id_hash value(s) {dupes[:5]} — "
            "one row per closed trade is the contract"
        )

    # 6. Back-fill missing nullable columns (strictly-additive discipline: an
    #    older exporter build may predate a nullable column; required columns
    #    were already enforced above).
    out = df.copy()
    for col in BROKER_FILLS_V1_COLUMNS:
        if col not in out.columns:
            logger.warning("%s: nullable column %r absent — back-filling None", source, col)
            out[col] = None
    out = out[list(BROKER_FILLS_V1_COLUMNS)]

    # 7. Normalize provenance, then enforce the non-null floor.
    out = _normalize_provenance_cohort(out, source=source)
    for col in sorted(REQUIRED_COLUMNS):
        if out[col].isna().any():
            raise BrokerFillsContractError(f"{source}: null cell(s) in required column {col!r}")

    return out.reset_index(drop=True)


def load_broker_fills(fills_dir: Path | None = None) -> pd.DataFrame:
    """Load + validate the latest broker-fills snapshot.

    Missing directory / no snapshot yet is BENIGN (nothing delivered) and
    returns an empty frame with the pinned columns; a present-but-invalid file
    raises :class:`BrokerFillsContractError` loudly.
    """
    path = latest_snapshot_path(fills_dir)
    if path is None:
        logger.info(
            "no broker-fills snapshot under %s — returning empty frame",
            Path(fills_dir) if fills_dir is not None else DEFAULT_BROKER_FILLS_DIR,
        )
        return _empty_frame()
    df = pd.read_parquet(path)
    return validate_broker_fills(df, source=path.name)


def ingest_jsonl_snapshot(jsonl_path: Path, *, fills_dir: Path | None = None) -> Path:
    """Land a delivered exporter JSONL as the parquet ``load_broker_fills`` reads.

    The betlejem exporter is stdlib-only, so the wire artifact is
    ``broker-fills-<runts>.jsonl`` (same schema field-for-field — memo
    transport note); this is the AL-side converter that validates it and
    writes ``broker-fills-<runts>.parquet`` next to the other snapshots
    (tmp-then-``os.replace``). Validation happens BEFORE any write, so a
    contract-violating delivery leaves the snapshot dir untouched.
    """
    import json

    jsonl_path = Path(jsonl_path)
    records = []
    with jsonl_path.open() as fh:
        for raw_line in fh:
            stripped = raw_line.strip()
            if stripped:
                records.append(json.loads(stripped))
    if not records:
        raise BrokerFillsContractError(f"{jsonl_path.name}: empty file — no valid JSON lines")
    df = pd.DataFrame.from_records(records)
    for ts_col in ("export_run_ts_utc", "fill_ts_utc", "close_ts_utc"):
        if ts_col in df.columns:
            was_present = df[ts_col].notna()
            parsed = pd.to_datetime(df[ts_col], utc=True, errors="coerce")
            # A null in the SOURCE is legitimate (nullable columns); a value
            # that fails to parse is a contract breach, never a silent NaT.
            corrupted = was_present & parsed.isna()
            if bool(corrupted.any()):
                raise BrokerFillsContractError(
                    f"{jsonl_path.name}: unparseable {ts_col} value(s), e.g. "
                    f"{df.loc[corrupted, ts_col].iloc[0]!r}"
                )
            df[ts_col] = parsed
    validated = validate_broker_fills(df, source=jsonl_path.name)
    directory = Path(fills_dir) if fills_dir is not None else DEFAULT_BROKER_FILLS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    out_path = directory / (jsonl_path.stem + ".parquet")
    tmp_path = out_path.with_suffix(".parquet.tmp")
    validated.to_parquet(tmp_path, index=False)
    os.replace(tmp_path, out_path)
    return out_path


def calibration_join_keys(df: pd.DataFrame, *, exchange: str = DEFAULT_EXCHANGE) -> pd.DataFrame:
    """Per-row ``(arrival_session, ticker)`` join keys for outcome-side joins.

    ``arrival_session`` is the first ``exchange`` session on-or-after the UTC
    date of ``fill_ts_utc`` (the exporter deliberately ships raw timestamps;
    exchange-calendar logic stays AlphaLens-side per the contract). Rows with a
    null ``fill_ts_utc`` get ``arrival_session=None`` rather than being dropped
    silently. Ticker symbology mismatches (GPW suffixes etc.) are a downstream
    join concern, not resolved here.
    """
    sessions: list[dt.date | None] = []
    for val in df["fill_ts_utc"]:
        if not _has_value(val):
            sessions.append(None)
            continue
        ts = pd.Timestamp(val)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC")
        sessions.append(session_on_or_after(ts.date(), exchange=exchange))
    return pd.DataFrame({"arrival_session": sessions, "ticker": [str(t) for t in df["ticker"]]})


__all__ = [
    "BROKER_FILLS_V1_COLUMNS",
    "DEFAULT_BROKER_FILLS_DIR",
    "FORBIDDEN_COLUMNS",
    "PROVENANCE_COHORTS",
    "PROVENANCE_NO_ENTRY_RECORD",
    "PROVENANCE_POST_C1612",
    "PROVENANCE_PRE_C1612",
    "REQUIRED_COLUMNS",
    "SCHEMA_VERSION",
    "BrokerFillsContractError",
    "calibration_join_keys",
    "latest_snapshot_path",
    "load_broker_fills",
    "validate_broker_fills",
]
