"""Lean batch universe screener (MVP1, rule-based).

Runs inside QuantConnect Lean (Docker container) once per invocation:
  1. Load the ticker universe from `universe.yaml` (mounted alongside this file).
  2. Add each ticker with Resolution.Daily and let Lean warm up history.
  3. On `on_end_of_algorithm`, pull the trailing OHLCV window for each symbol,
     run the pure-Python `scorer.rank_universe`, and write top-N to
     `/Results/candidates.json` as an atomic temp + rename.

Only this file imports `AlgorithmImports`; `features.py` and `scorer.py` stay
dependency-free so they can be unit-tested on the host identically.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import yaml

# Lean-only imports — resolved inside the container. AlgorithmImports is QC's
# runtime symbol bag; wildcard is intentional and required by the framework.
from AlgorithmImports import *  # noqa: F403  # pylint: disable=wildcard-import  # NOSONAR
from features import dollar_volume_average  # noqa: F401  (smoke import)
from scorer import rank_universe

SCHEMA_VERSION = "1.0"
RESULTS_FILE = "/Results/candidates.json"
UNIVERSE_FILE = Path(__file__).resolve().parent / "universe.yaml"

# History window — must cover the longest SMA + a small buffer.
_HISTORY_BARS = 260

# Scoring config duplicated here because this file runs inside Lean where the
# host-side alphalens package isn't importable. Keep in sync with
# `alphalens/screeners/lean/config.py::LEAN_DEFAULTS`.
SCORER_CONFIG = {
    "weight_roc20": 0.20,
    "weight_roc60": 0.20,
    "weight_volume_surprise": 0.20,
    "weight_trend_strength": 0.20,
    "weight_breakout": 0.10,
    "weight_near_high": 0.10,
    "roc_short": 5,
    "roc_medium": 20,
    "roc_long": 60,
    "sma_short": 20,
    "sma_medium": 50,
    "sma_long": 200,
    "volume_window": 20,
    "breakout_window": 20,
    "near_high_window": 60,
    "breakout_volume_multiple": 1.5,
    "min_price": 5.0,
    "max_price": 200.0,
    "min_avg_dollar_volume": 2_000_000.0,
    "top_n": 30,
}


def _load_universe(path: Path = UNIVERSE_FILE) -> list[str]:
    with path.open() as fh:
        raw = yaml.safe_load(fh) or {}
    tickers: set[str] = set()
    for _sector, names in raw.items():
        for name in names or []:
            tickers.add(str(name).upper())
    return sorted(tickers)


def _atomic_write_json(path: str, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".candidates_", suffix=".json", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class LeanBatchScreener(QCAlgorithm):  # type: ignore[name-defined]  # noqa: F405
    def initialize(self):
        self.SetCash(100_000)
        end = self.Time if hasattr(self, "Time") else datetime.now(UTC)
        self.SetStartDate(end.year - 1, end.month, max(end.day - 1, 1))

        self._universe = _load_universe()
        for ticker in self._universe:
            try:
                self.AddEquity(ticker, Resolution.Daily)  # noqa: F405
            except Exception as exc:
                logging.warning("skip %s: %s", ticker, exc)

        logging.info("LeanBatchScreener initialized with %d tickers", len(self._universe))

    def on_data(self, _data):
        """No per-bar work — all the computation happens at end-of-algorithm."""

    def on_end_of_algorithm(self):
        histories: dict[str, pd.DataFrame] = {}
        for ticker in self._universe:
            symbol = self.Symbol(ticker) if hasattr(self, "Symbol") else None
            if symbol is None:
                continue
            try:
                history = self.History(symbol, _HISTORY_BARS, Resolution.Daily)  # noqa: F405
            except Exception as exc:
                logging.debug("no history for %s: %s", ticker, exc)
                continue
            if history is None or history.empty:
                continue
            if isinstance(history.index, pd.MultiIndex):
                history = history.reset_index(level=0, drop=True)
            histories[ticker] = history[["open", "high", "low", "close", "volume"]]

        ranked = rank_universe(histories, SCORER_CONFIG)
        top_n = SCORER_CONFIG["top_n"]
        top = ranked.head(top_n)

        payload = {
            "status": "success",
            "timestamp": datetime.now(UTC).isoformat(),
            "version": SCHEMA_VERSION,
            "total_scored": len(ranked),
            "universe_size": len(self._universe),
            "rankings": [
                {
                    "ticker": str(row["ticker"]),
                    "rank": int(row["rank"]),
                    "score": float(row["score"]),
                    "roc5": float(row["roc5"]),
                    "roc20": float(row["roc20"]),
                    "roc60": float(row["roc60"]),
                    "volume_surprise": float(row["volume_surprise"]),
                    "trend_strength": float(row["trend_strength"]),
                    "breakout": bool(row["breakout"]),
                    "near_high": float(row["near_high"]),
                    "last_close": float(row["last_close"]),
                    "avg_dollar_volume": float(row["avg_dollar_volume"]),
                }
                for _, row in top.iterrows()
            ],
        }
        _atomic_write_json(RESULTS_FILE, payload)
        logging.info("wrote %d ranked tickers to %s", len(top), RESULTS_FILE)
