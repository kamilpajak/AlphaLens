"""Registry-driven qualitative brief enrichment — read-once / write-once atomic.

The daily brief parquet is read ONCE; each qual-capable expert (a
:class:`~alphalens_pipeline.experts.base.QualEnrichExpert`) stamps ITS columns into
the one shared in-memory frame; the result is written ONCE via
:func:`~alphalens_pipeline.data.parquet_io.write_parquet_atomic`. The single atomic
swap makes a truncated read impossible for the 6x/day Django re-ingest that races
this write. Experts without the eager-qual capability (e.g. a numeric-only O'Neil)
are skipped at zero cost.

Display-only: the stamped columns are characteristics, never inputs to candidate
selection or ordering.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Iterable
from pathlib import Path

import pandas as pd

from alphalens_pipeline.data.parquet_io import write_parquet_atomic
from alphalens_pipeline.experts.base import Expert, QualEnrichExpert

logger = logging.getLogger(__name__)

_DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"


def enrich_briefs(
    brief_date: dt.date,
    *,
    experts: Iterable[Expert],
    briefs_dir: Path | None = None,
    store: object,
    mcap_fn: Callable[..., object],
    dividends_fn: Callable[..., object],
    exec_comp_fn: Callable[..., object] | None = None,
    scuttlebutt: bool = False,
    cache_dir: Path | None = None,
) -> dict[str, int]:
    """Read the brief parquet once, let each qual-capable expert stamp into one
    frame, write once atomically. Returns ``{expert_id: real-classification count}``.

    Per-expert fail-soft: one expert raising does NOT abort the others; the single
    atomic write still happens after every expert has run (a partial failure leaves
    that expert's cells null on the written file — the same behaviour as the prior
    in-place write, not a regression). Experts that are not a
    :class:`QualEnrichExpert` are skipped with no panel build / no LLM cost.

    Raises ``FileNotFoundError`` if the brief parquet is absent (the CLI maps it to
    a user error).
    """
    resolved_dir = briefs_dir if briefs_dir is not None else _DEFAULT_BRIEFS_DIR
    path = Path(resolved_dir) / f"{brief_date.isoformat()}.parquet"
    df = pd.read_parquet(path)  # read ONCE

    counts: dict[str, int] = {}
    stamped = False
    for expert in experts:
        if not isinstance(expert, QualEnrichExpert):
            continue  # numeric-only expert — no eager qualitative layer
        try:
            df, n_real = expert.enrich_brief_frame(
                df,
                brief_date,
                briefs_dir=resolved_dir,
                store=store,
                mcap_fn=mcap_fn,
                dividends_fn=dividends_fn,
                exec_comp_fn=exec_comp_fn,
                scuttlebutt=scuttlebutt,
                cache_dir=cache_dir,
            )
            counts[expert.id] = n_real
            stamped = True
        except Exception as exc:  # one expert must not abort the batch
            # exc_info so a persistent failure carries the full traceback (the
            # only other signal is the count-0 in the CLI echo).
            logger.warning("experts enrich: %s failed: %s", expert.id, exc, exc_info=True)
            counts[expert.id] = 0

    if stamped:
        write_parquet_atomic(df, path, index=False)  # write ONCE
    return counts


__all__ = ["enrich_briefs"]
