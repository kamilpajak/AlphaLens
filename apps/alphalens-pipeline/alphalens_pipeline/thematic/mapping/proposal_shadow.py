"""V-forward proposal-shadow logger (D4 §8).

Shadow-logs, per (theme, brief_date), BOTH ungated candidate sources so a clean,
forward, size/attention-controlled head-to-head can settle whether a mechanical
news-reading rule beats the LLM's free-association proposal:

- ``source="llm"`` — the LLM's **pre-gate** proposals (the full post-mcap set,
  before the verification gates that today discard everything but survivors);
- ``source="mechanical"`` — the **salience-membership** set: equal-weight tickers
  named in the theme's own news (``thematic_events.primary_entities``) over the
  production 30-day catalyst window. NO frequency ranking (news-frequency is an
  attention proxy — see the design memo §6/§12); ``mech_article_count`` is logged
  only as a descriptor, never as a weight.

This is telemetry only — parquet-only SoT, forward-only, display/analysis-lane,
never a production ``theme_mapper`` change. Size-matching + forward
``market_excess`` are computed later at analysis time from persistent stores
(``grouped_daily_history`` for dollar-volume + returns), so nothing here needs an
mcap or price fetch.

Design: docs/research/theme_mapper_mechanical_rule_headtohead_design_2026_07_12.md
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from alphalens_pipeline.data.parquet_io import write_parquet_atomic
from alphalens_pipeline.thematic.mapping.catalyst_resolver import DEFAULT_EVENTS_DIR

# Poolability keys — a downstream analysis must never merge rows produced under
# different rule/config tokens (same discipline as options_*/novelty_* stamps).
MECH_RULE_VERSION = "mech-salience-equalweight-v1"
PROPOSAL_SHADOW_VERSION = "proposal-shadow-v1"

DEFAULT_SHADOW_DIR = Path.home() / ".alphalens" / "proposal_shadow"

# Production catalyst window (catalyst_resolver.DEFAULT_LOOKBACK_DAYS).
_DEFAULT_LOOKBACK_DAYS = 30

_SHADOW_COLUMNS = (
    "brief_date",
    "theme",
    "ticker",
    "source",
    "llm_confidence",
    "mech_article_count",
    "mapper_config_version",
    "mech_rule_version",
    "proposal_shadow_version",
)


def _iter_list(value: Any) -> list[str]:
    """Coerce a parquet cell (list / numpy array / None) to a list of strings."""
    if value is None:
        return []
    try:
        return [str(v) for v in list(value)]
    except TypeError:
        return []


def mechanical_salience_candidates(
    theme: str,
    asof: dt.date,
    *,
    events_dir: Path = DEFAULT_EVENTS_DIR,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """Equal-weight salience-membership candidates for ``theme`` as of ``asof``.

    A ticker is a candidate iff it appears in ``primary_entities`` of at least one
    ``thematic_events`` row tagged with ``theme`` within ``[asof - lookback, asof]``.
    Returned most-frequent-first (descriptor only — the rule is equal-weight, so
    ordering carries no selection meaning). Tickers upper-cased.
    """
    counts: Counter[str] = Counter()
    for k in range(lookback_days + 1):
        day = asof - dt.timedelta(days=k)
        path = Path(events_dir) / f"{day.isoformat()}.parquet"
        if not path.exists():
            continue
        frame = pd.read_parquet(path)
        if "themes" not in frame.columns or "primary_entities" not in frame.columns:
            continue
        for themes_cell, ents_cell in zip(frame["themes"], frame["primary_entities"], strict=True):
            if theme in _iter_list(themes_cell):
                for ent in _iter_list(ents_cell):
                    counts[ent.upper()] += 1
    return [
        {"ticker": ticker, "mech_article_count": count} for ticker, count in counts.most_common()
    ]


def build_shadow_frame(
    asof: dt.date,
    llm_proposals: Sequence[Mapping[str, Any]],
    *,
    events_dir: Path = DEFAULT_EVENTS_DIR,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    mapper_config_version: str | None,
) -> pd.DataFrame:
    """Combine the LLM pre-gate proposals + the mechanical salience set into one frame.

    ``llm_proposals`` items carry ``theme``, ``ticker`` and ``llm_confidence``. The
    mechanical arm is generated per distinct theme present in ``llm_proposals`` (the
    same themes the pipeline actually mapped that day).
    """
    rows: list[dict[str, Any]] = []
    for prop in llm_proposals:
        rows.append(
            {
                "brief_date": asof,
                "theme": str(prop["theme"]),
                "ticker": str(prop["ticker"]).upper(),
                "source": "llm",
                "llm_confidence": prop.get("llm_confidence"),
                "mech_article_count": pd.NA,
            }
        )
    for theme in dict.fromkeys(str(p["theme"]) for p in llm_proposals):
        for cand in mechanical_salience_candidates(
            theme, asof, events_dir=events_dir, lookback_days=lookback_days
        ):
            rows.append(
                {
                    "brief_date": asof,
                    "theme": theme,
                    "ticker": cand["ticker"],
                    "source": "mechanical",
                    "llm_confidence": pd.NA,
                    "mech_article_count": cand["mech_article_count"],
                }
            )
    frame = pd.DataFrame(rows, columns=list(_SHADOW_COLUMNS[:6]))
    frame["llm_confidence"] = pd.to_numeric(frame["llm_confidence"], errors="coerce")
    frame["mech_article_count"] = frame["mech_article_count"].astype("Int64")
    frame["mapper_config_version"] = mapper_config_version
    frame["mech_rule_version"] = MECH_RULE_VERSION
    frame["proposal_shadow_version"] = PROPOSAL_SHADOW_VERSION
    return frame


def write_proposal_shadow(
    asof: dt.date,
    llm_proposals: Sequence[Mapping[str, Any]],
    *,
    events_dir: Path = DEFAULT_EVENTS_DIR,
    out_dir: Path = DEFAULT_SHADOW_DIR,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    mapper_config_version: str | None,
) -> Path | None:
    """Build + atomically write the shadow parquet for ``asof``; return its path.

    Returns ``None`` (writes nothing) when there is neither an LLM proposal nor a
    mechanical candidate — a genuinely empty day leaves no file.
    """
    frame = build_shadow_frame(
        asof,
        llm_proposals,
        events_dir=events_dir,
        lookback_days=lookback_days,
        mapper_config_version=mapper_config_version,
    )
    if frame.empty:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{asof.isoformat()}.parquet"
    write_parquet_atomic(frame, path, index=False)
    return path
