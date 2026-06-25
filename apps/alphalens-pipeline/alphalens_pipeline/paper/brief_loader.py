"""Read one day's thematic brief parquet into a ``CandidateBrief`` shape.

Source of truth is the parquet file written by the daily thematic pipeline
(``~/.alphalens/thematic_briefs/<date>.parquet``). The Django DB is a
downstream consumer of the same parquet — reading directly from parquet keeps
this loader runnable on the laptop without the API up and matches the design
memo's "parquet is SoT" framing.

This module does only loading + decoding (``brief_trade_setup`` is persisted as
a JSON string in parquet; we parse it here). The decoded rows feed the
broker-free feedback replay engines (``alphalens_pipeline.feedback``).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CandidateBrief:
    """A single brief row decoded for the planner.

    ``trade_setup`` is the parsed dict (or ``None`` if the row's
    ``brief_trade_setup`` was null / empty / unparseable). The planner
    decides whether to shadow-log the candidate.
    """

    brief_date: dt.date
    ticker: str
    theme: str
    verified: bool
    suggested_size_pct: float | None
    trade_setup: dict | None
    n_gates_passed: int
    n_gates_failed: int
    layer4_weighted_score: float | None
    scorer_config_version: str


def _int_or_zero(row: pd.Series, key: str) -> int:
    """Return ``int(row[key])`` or ``0`` when the key is absent, null, or un-castable."""
    if key not in row.index:
        return 0
    val = row[key]
    try:
        return int(val) if pd.notna(val) else 0
    except (TypeError, ValueError):
        return 0


def _float_or_none(row: pd.Series, key: str) -> float | None:
    """Return ``float(row[key])`` or ``None`` when the key is absent, null, or un-castable."""
    if key not in row.index:
        return None
    val = row[key]
    try:
        return float(val) if pd.notna(val) else None
    except (TypeError, ValueError):
        return None


def _str_or_empty(row: pd.Series, key: str) -> str:
    """Return ``str(row[key])`` or ``""`` when the key is absent or null (including NaN)."""
    if key not in row.index:
        return ""
    val = row[key]
    return "" if pd.isna(val) else str(val)


def _coerce_trade_setup(raw: object) -> dict | None:
    """Best-effort decode of the ``brief_trade_setup`` column.

    Persisted as a JSON-string in parquet (see
    ``apps/alphalens-django/briefs/ingest/parquet.py`` _OBJECT_JSON_FIELDS).
    Some legacy rows have already been parsed to dict by the Django ingest
    (after the JSON roundtrip); accept both.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return None
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("brief_trade_setup not JSON-parseable: %s", exc)
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def _row_to_candidate(row: pd.Series, brief_date: dt.date) -> CandidateBrief:
    setup_raw = row.get("brief_trade_setup") if "brief_trade_setup" in row.index else None
    setup = _coerce_trade_setup(setup_raw)
    suggested = (
        float(setup["suggested_size_pct"])
        if setup and setup.get("suggested_size_pct") is not None
        else None
    )
    return CandidateBrief(
        brief_date=brief_date,
        ticker=str(row["ticker"]),
        theme=str(row.get("theme", "")),
        verified=bool(row.get("verified", False)),
        suggested_size_pct=suggested,
        trade_setup=setup,
        n_gates_passed=_int_or_zero(row, "n_gates_passed"),
        n_gates_failed=_int_or_zero(row, "n_gates_failed"),
        layer4_weighted_score=_float_or_none(row, "layer4_weighted_score"),
        scorer_config_version=_str_or_empty(row, "scorer_config_version"),
    )


def load_brief(brief_date: dt.date, briefs_dir: Path) -> list[CandidateBrief]:
    """Load + decode the brief parquet for ``brief_date``.

    Raises ``FileNotFoundError`` if the parquet is missing — the planner
    surfaces that as an actionable CLI error rather than silently treating
    a missing brief as "no candidates today" (which would never get planned
    on a missed-pipeline-run day).
    """
    path = briefs_dir / f"{brief_date.isoformat()}.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"thematic brief parquet not found: {path} — run the daily pipeline"
            " or pass --briefs-dir to a snapshot directory"
        )
    df = pd.read_parquet(path)
    if "ticker" not in df.columns:
        raise ValueError(f"parquet {path.name} is missing required column 'ticker'")
    return [_row_to_candidate(row, brief_date) for _, row in df.iterrows()]


__all__ = [
    "CandidateBrief",
    "load_brief",
]
