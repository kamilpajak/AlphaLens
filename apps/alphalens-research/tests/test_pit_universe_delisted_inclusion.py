"""PIT survivorship invariant: BacktestEngine MUST include delisted tickers.

Locks the O2 finding from `docs/research/pit_audit_2026_04_30_findings.md`.
The engine builds its universe at construction time from `HistoryStore.tickers()`
minus the benchmark — see `engine.py:194-196`. Survivorship discipline is
maintained because the store contains delisted tickers' histories (Polygon
backfill via `?active=false` per memory `project_mvp2_survivorship_bias.md`).

Without enforcement, a future change to either layer ("filter to currently
active", "drop tickers with no bars today") would silently re-introduce
classic ex-post-facto survivorship bias. These tests freeze both contracts:

  1. ``HistoryStore.tickers()`` returns every cached key, regardless of whether
     the ticker still has fresh bars at any given date.
  2. ``BacktestEngine`` does NOT filter the constructed universe by
     "currently-listed" — delisted tickers are scored on rebalance dates that
     fall before their delisting date.
"""

from __future__ import annotations

import unittest
from datetime import date

import numpy as np
import pandas as pd
from alphalens_research.backtest.engine import BacktestEngine
from alphalens_research.data.store.history import HistoryStore


def _bars(
    start: str, periods: int, start_price: float = 100.0, drift: float = 0.0005
) -> pd.DataFrame:
    dates = pd.bdate_range(start, periods=periods)
    rng = np.random.default_rng(seed=hash(start) & 0xFFFFFFFF)
    prices = start_price * np.exp(np.cumsum(drift + 0.005 * rng.standard_normal(periods)))
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "close": prices,
            "volume": 1_000_000,
        },
        index=dates,
    )


def _recording_scorer_factory():
    """Returns (scorer, seen_per_day) where seen_per_day is a dict
    {asof_date_iso -> sorted set of tickers actually passed to the scorer}.
    """

    seen_per_day: dict[str, set[str]] = {}

    def scorer(histories, config):
        # The scorer sees only tickers whose truncated history meets MIN_BARS.
        # We snapshot the keys exactly as received so the test can assert on
        # what the engine fed in.
        if not histories:
            return pd.DataFrame(columns=["ticker", "score"])
        latest = max(df.index[-1] for df in histories.values() if not df.empty)
        seen_per_day[str(latest.date())] = set(histories.keys())
        rows = [
            {"ticker": t, "score": float(df["close"].iloc[-1])}
            for t, df in histories.items()
            if not df.empty
        ]
        return pd.DataFrame(rows)

    scorer.MIN_BARS_REQUIRED = 5
    return scorer, seen_per_day


class TestHistoryStoreDoesNotFilterActive(unittest.TestCase):
    """Contract: ``HistoryStore.tickers()`` returns ALL cached tickers.

    A change that filtered by "has bars in the last N days" would silently
    drop delisted names from any backtest universe constructed via
    ``store.tickers()``.
    """

    def test_tickers_includes_delisted(self):
        store = HistoryStore(
            {
                "SPY": _bars("2020-01-01", 600),
                "ALIVE": _bars("2020-01-01", 600),
                # DELISTED stops contributing bars in 2020-06; never extended.
                "DELISTED": _bars("2020-01-01", 120),
            }
        )
        self.assertEqual(set(store.tickers()), {"SPY", "ALIVE", "DELISTED"})

    def test_tickers_unchanged_after_truncate_calls(self):
        # Locking that truncate_to does not mutate the cache (a defensive
        # guard against accidentally dropping tickers that returned empty
        # frames at some asof). Should be a no-op but worth a fence.
        store = HistoryStore(
            {
                "ALIVE": _bars("2020-01-01", 200),
                "PRE_HISTORY_DELISTED": _bars("2010-01-01", 100),
            }
        )
        before = set(store.tickers())
        # Truncate to a date AFTER PRE_HISTORY_DELISTED's last bar.
        store.truncate_to("PRE_HISTORY_DELISTED", date(2024, 1, 1))
        store.truncate_to("ALIVE", date(2024, 1, 1))
        self.assertEqual(set(store.tickers()), before)


class TestBacktestEngineIncludesDelistedTickers(unittest.TestCase):
    """Contract: BacktestEngine constructs universe from ``store.tickers()``
    minus benchmark, with NO current-existence filter.

    A delisted ticker MUST be scored on rebalance days that fall before
    its last bar (i.e. while it was still trading). Failure of this test
    means a survivorship filter has been silently introduced.
    """

    def _build_store(self) -> HistoryStore:
        # SPY: full benchmark calendar, 200 business days from 2020-01-01.
        # ALIVE: full history, same range.
        # MID_DELIST: stops trading at bar #80 (~2020-04-22).
        # EARLY_DELIST: stops trading at bar #20 (~2020-01-29) — mostly outside
        # the rebalance window we'll use, but must still appear in tickers().
        return HistoryStore(
            {
                "SPY": _bars("2020-01-01", 200, start_price=400.0, drift=0.0003),
                "ALIVE": _bars("2020-01-01", 200, start_price=50.0, drift=0.0008),
                "MID_DELIST": _bars("2020-01-01", 80, start_price=20.0, drift=-0.001),
                "EARLY_DELIST": _bars("2020-01-01", 20, start_price=10.0, drift=-0.005),
            }
        )

    def test_universe_count_includes_delisted(self):
        store = self._build_store()
        scorer, _ = _recording_scorer_factory()
        engine = BacktestEngine(
            store,
            scorer=scorer,
            scorer_config={"benchmark": "SPY"},
            holding_period=5,
            top_n=3,
            benchmark="SPY",
        )
        report = engine.run(date(2020, 1, 15), date(2020, 6, 1))
        # SPY excluded as benchmark; ALIVE + MID_DELIST + EARLY_DELIST all
        # included → 3 names. A survivorship-biased engine would drop the
        # two delisted names and report universe_ticker_count == 1.
        self.assertEqual(
            report.universe_ticker_count,
            3,
            msg=(
                "Universe should include delisted tickers. "
                f"Got {report.universe_ticker_count}; expected 3 (ALIVE + 2 delisted). "
                "If this fails, the engine is filtering by current-existence — "
                "classic survivorship bias. See pit_audit_2026_04_30_findings.md."
            ),
        )

    def test_delisted_ticker_scored_before_delisting(self):
        store = self._build_store()
        scorer, seen_per_day = _recording_scorer_factory()
        engine = BacktestEngine(
            store,
            scorer=scorer,
            scorer_config={"benchmark": "SPY"},
            holding_period=5,
            top_n=3,
            benchmark="SPY",
        )
        engine.run(date(2020, 1, 15), date(2020, 6, 1))

        # MID_DELIST trades through ~2020-04-22 (bar 80). Find at least one
        # rebalance day strictly before that where MID_DELIST appeared in
        # the histories the engine fed to the scorer.
        delisting_cutoff = pd.Timestamp("2020-04-15")  # comfortably before bar 80
        early_rebalance_days = [
            day_iso
            for day_iso, tickers in seen_per_day.items()
            if pd.Timestamp(day_iso) <= delisting_cutoff
        ]
        self.assertTrue(
            early_rebalance_days,
            msg="No rebalance days observed before delisting cutoff — test is broken.",
        )
        appeared = [
            day_iso for day_iso in early_rebalance_days if "MID_DELIST" in seen_per_day[day_iso]
        ]
        self.assertTrue(
            appeared,
            msg=(
                "MID_DELIST should be scored on rebalance days before its delisting. "
                "If empty, either the engine is filtering or MIN_BARS_REQUIRED screened "
                "all early days — adjust scorer.MIN_BARS_REQUIRED or use longer histories."
            ),
        )


if __name__ == "__main__":
    unittest.main()
