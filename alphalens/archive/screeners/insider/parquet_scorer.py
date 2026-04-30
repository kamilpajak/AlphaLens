"""In-memory parquet-backed drop-in for ``InsiderScorer``.

The original ``InsiderScorer`` caches per-(ticker, asof) feature lookups as
JSON files in ``~/.alphalens/insider_form4/`` (~6.5M files, 25 GB). The
migration tool at ``~/.alphalens/tools/migrate_form4/`` collapses that into
``~/.alphalens/insider_form4.parquet/`` (4 161 parquet files, 94 MB,
hive-partitioned by year). This scorer exposes the same
``features_as_of(ticker, asof)`` API by loading the parquet once into a
dict at construction.

A backtest sweep over the full 2011-2026 PIT universe issues hundreds of
thousands of feature lookups. With the JSON cache each call is a syscall
+ JSON parse; with this scorer each call is one dict get. On the
Layer 2d 12-year IS sweep that turns minutes of I/O into milliseconds.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pyarrow.dataset as ds


def _to_date(value: Any) -> date:
    """Normalise a parquet date cell to ``datetime.date``.

    The migration tool emits the ``date`` column as timezone-naive
    ``datetime64[ns]``; pandas surfaces those as ``pd.Timestamp`` on
    ``itertuples``. ``pd.Timestamp == datetime.date`` is False, which
    silently turned every cache lookup into a miss prior to this guard.
    """
    if isinstance(value, date) and not hasattr(value, "to_pydatetime"):
        return value
    if hasattr(value, "date"):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"unsupported date cell type: {type(value).__name__}")


class ParquetInsiderScorer:
    """Drop-in for :class:`alphalens.archive.screeners.insider.scorer.InsiderScorer`.

    Reads pre-computed cluster features from a hive-partitioned parquet
    dataset. Intended for backtest runs over a warmed cache; not for live
    EDGAR fetches (the original scorer remains the canonical fetcher).
    """

    def __init__(self, parquet_path: Path):
        if not parquet_path.exists():
            raise FileNotFoundError(f"parquet dataset missing: {parquet_path}")

        dataset = ds.dataset(str(parquet_path), partitioning="hive")
        table = dataset.to_table(
            columns=[
                "ticker",
                "date",
                "has_features",
                "insider_count",
                "aggregate_dollar",
                "cluster_window_days",
            ]
        )
        df = table.to_pandas()

        features: dict[tuple[str, date], dict | None] = {}
        for row in df.itertuples(index=False):
            key = (row.ticker.upper(), _to_date(row.date))
            if not row.has_features:
                features[key] = None
                continue
            features[key] = {
                "insider_count": int(row.insider_count),
                "aggregate_dollar": float(row.aggregate_dollar),
                "cluster_window_days": int(row.cluster_window_days),
            }

        self._features = features
        self._row_count = len(df)
        self._with_features = sum(1 for v in features.values() if v is not None)

    def features_as_of(self, ticker: str, asof: date) -> dict | None:
        """Return cached cluster features for (ticker, asof), or None.

        Returns ``None`` for two distinct cases (matching the original
        scorer's contract):
          - Cache hit with no cluster detected (``has_features=False`` row)
          - Cache miss (no row for this key in the parquet)
        Callers downstream don't need to distinguish — both mean "no signal".

        The returned dict's ``asof`` field echoes the caller-supplied argument
        (matching :class:`InsiderScorer` contract); the parquet's stored
        ``asof`` column is dropped at construction to save ~50 MB of redundant
        date strings across 6.5M rows.
        """
        feat = self._features.get((ticker.upper(), asof))
        if feat is None:
            return None
        return {**feat, "asof": asof.isoformat()}

    @property
    def stats(self) -> dict[str, int]:
        return {
            "total_rows": self._row_count,
            "with_features": self._with_features,
            "no_cluster": self._row_count - self._with_features,
        }
