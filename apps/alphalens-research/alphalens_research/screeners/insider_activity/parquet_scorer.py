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

from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow.dataset as ds

if TYPE_CHECKING:
    from alphalens_pipeline.data.store.delisting import DelistingEvent


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
        return value.date()  # type: ignore[attr-defined,no-any-return]
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"unsupported date cell type: {type(value).__name__}")


class ParquetInsiderScorer:
    """Drop-in for :class:`alphalens_research.archive.screeners.insider.scorer.InsiderScorer`.

    Reads pre-computed cluster features from a hive-partitioned parquet
    dataset. Intended for backtest runs over a warmed cache; not for live
    EDGAR fetches (the original scorer remains the canonical fetcher).
    """

    def __init__(
        self,
        parquet_path: Path,
        delisting_events: Iterable[DelistingEvent] | None = None,
        delisting_exclusion_days: int = 180,
    ):
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

        # F4 PIT contract (pit_audit_2026_04_30): exclude pre-delisting fire
        # sales. Insider sales in the months before bankruptcy/delisting are
        # panic unloading, not informed cluster activity — naive backtests
        # that include them inflate alpha by 100-300 bps via delisting
        # selection bias. Default 180d covers the typical pre-bankruptcy
        # window where insiders accelerate disposition.
        self._delisting_by_ticker: dict[str, list[date]] = {}
        if delisting_events is not None:
            for ev in delisting_events:
                self._delisting_by_ticker.setdefault(ev.ticker.upper(), []).append(ev.delisted_date)
        self._delisting_exclusion_days = int(delisting_exclusion_days)

    def features_as_of(self, ticker: str, asof: date) -> dict | None:
        """Return cached cluster features for (ticker, asof), or None.

        Returns ``None`` for three distinct cases:
          - Cache hit with no cluster detected (``has_features=False`` row)
          - Cache miss (no row for this key in the parquet)
          - F4 fire-sale exclusion: ticker delists within
            ``delisting_exclusion_days`` of ``asof`` (default 180d). Pre-
            bankruptcy insider sales are not informed activity; including
            them inflates apparent alpha via delisting selection bias.

        Callers downstream don't need to distinguish — all three mean
        "no usable signal".

        The returned dict's ``asof`` field echoes the caller-supplied argument
        (matching :class:`InsiderScorer` contract); the parquet's stored
        ``asof`` column is dropped at construction to save ~50 MB of redundant
        date strings across 6.5M rows.
        """
        ticker_up = ticker.upper()
        if self._delisting_exclusion_days > 0:
            cutoff = asof + timedelta(days=self._delisting_exclusion_days)
            if any(d <= cutoff for d in self._delisting_by_ticker.get(ticker_up, [])):
                return None
        feat = self._features.get((ticker_up, asof))
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
