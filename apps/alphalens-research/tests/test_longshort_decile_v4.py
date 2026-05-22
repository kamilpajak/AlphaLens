"""Unit tests for v4 long/short decile builder (alt_data_screener_v4_2026_05_01).

Validates the new selection rule logic in isolation:
- Decile size = floor(n / 10) for each leg.
- Short-eligible filter: SI %% float <= 0.15 (ex-ante HTB safeguard).
- Asof skipped when either leg's decile size < 3.
- Per-asof return = long_mean - short_mean.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Import after path mutation so unit test can find the driver module.
import experiment_alt_data_lasso_longshort_20d as v4_driver  # noqa: E402


class _StubHistoryStore:
    """Minimal stub of HistoryStore.forward_return for unit testing."""

    def __init__(self, returns: dict[tuple[str, date], float]):
        self._returns = returns

    def forward_return(self, ticker: str, asof: date, holding: int) -> float | None:
        return self._returns.get((ticker, asof))


class _StubPolygonSI:
    """Returns a stub features_as_of record with controlled short_interest."""

    def __init__(self, si_by_ticker: dict[str, float]):
        self._si_by_ticker = si_by_ticker

    def features_as_of(self, ticker: str, asof: date):
        si_pct = self._si_by_ticker.get(ticker)
        if si_pct is None:
            return None
        rec = MagicMock()
        rec.short_interest = int(si_pct * 1_000_000)
        rec.settlement_date = asof
        return rec


def _stub_shares_lookup(ticker: str, asof: date) -> int:
    return 1_000_000


def _make_holdout_features(
    asof: date,
    n_long: int,
    n_short_eligible: int,
    n_short_ineligible: int,
    si_pct_long: float = 0.05,
    si_pct_short_eligible: float = 0.10,
    si_pct_short_ineligible: float = 0.30,
) -> tuple[pd.DataFrame, pd.Series, dict[str, float], dict[tuple[str, date], float]]:
    rows = []
    si_lookup: dict[str, float] = {}
    forward_returns: dict[tuple[str, date], float] = {}

    # Long-eligible bucket (low SI, high score → ends up in top decile)
    for i in range(n_long):
        tk = f"LONG{i:03d}"
        rows.append({"asof": asof, "ticker": tk})
        si_lookup[tk] = si_pct_long
        forward_returns[(tk, asof)] = 0.05  # +5%

    # Short-eligible bucket (low-mid SI, low score → ends up in bottom decile)
    for i in range(n_short_eligible):
        tk = f"SHRTOK{i:03d}"
        rows.append({"asof": asof, "ticker": tk})
        si_lookup[tk] = si_pct_short_eligible
        forward_returns[(tk, asof)] = -0.03  # -3%

    # Short-ineligible bucket (high SI > 15%, low score; should be FILTERED out)
    for i in range(n_short_ineligible):
        tk = f"HTBOUT{i:03d}"
        rows.append({"asof": asof, "ticker": tk})
        si_lookup[tk] = si_pct_short_ineligible
        forward_returns[(tk, asof)] = -0.10  # -10% (would lower short return if included)

    df = pd.DataFrame(rows)

    # Score: long names get high scores, short-eligible get medium-low scores,
    # short-ineligible get the LOWEST scores (so without filter they would dominate short leg).
    scores = []
    for r in df.itertuples(index=False):
        if r.ticker.startswith("LONG"):
            scores.append(1.0 + np.random.RandomState(hash(r.ticker) % 1000).uniform(-0.01, 0.01))
        elif r.ticker.startswith("SHRTOK"):
            scores.append(0.0 + np.random.RandomState(hash(r.ticker) % 1000).uniform(-0.01, 0.01))
        else:
            scores.append(-1.0 + np.random.RandomState(hash(r.ticker) % 1000).uniform(-0.01, 0.01))
    score_series = pd.Series(scores, index=df.index, dtype=float)

    return df, score_series, si_lookup, forward_returns


class LongShortDecileBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.asof = date(2024, 6, 1)

    def test_decile_size_floor_division(self) -> None:
        """Decile size = floor(n_eligible / 10) on each leg independently."""
        df, scores, si_lookup, returns = _make_holdout_features(
            self.asof,
            n_long=100,
            n_short_eligible=50,
            n_short_ineligible=20,
        )
        history = _StubHistoryStore(returns)
        history._returns[("SPY", self.asof)] = 0.01
        si_client = _StubPolygonSI(si_lookup)

        (
            ls_returns,
            _bench,
            long_lists,
            short_lists,
            _l,
            _s,
            long_sizes,
            short_sizes,
        ) = v4_driver._holdout_longshort_decile_returns_20d(
            df,
            scores,
            history,
            benchmark="SPY",
            polygon_si_client=si_client,
            shares_lookup=_stub_shares_lookup,
            si_max_short=0.15,
        )

        self.assertEqual(len(ls_returns), 1, "single-asof input should produce 1 rebalance")
        # Long-eligible = 170 (all rows), decile_long = 17
        self.assertEqual(long_sizes[0], 17)
        # Short-eligible = 150 (LONG[100] + SHRTOK[50] both <= 15%; HTBOUT[20] filtered out)
        self.assertEqual(short_sizes[0], 15)

    def test_short_leg_excludes_si_above_15pct(self) -> None:
        """Names with SI > 15% must NOT appear in the short leg."""
        df, scores, si_lookup, returns = _make_holdout_features(
            self.asof,
            n_long=100,
            n_short_eligible=50,
            n_short_ineligible=20,
        )
        history = _StubHistoryStore(returns)
        history._returns[("SPY", self.asof)] = 0.01
        si_client = _StubPolygonSI(si_lookup)

        (
            _ls,
            _bench,
            _ll,
            short_lists,
            _l,
            _s,
            _lsize,
            _ssize,
        ) = v4_driver._holdout_longshort_decile_returns_20d(
            df,
            scores,
            history,
            benchmark="SPY",
            polygon_si_client=si_client,
            shares_lookup=_stub_shares_lookup,
            si_max_short=0.15,
        )

        for tk in short_lists[0]:
            self.assertFalse(
                tk.startswith("HTBOUT"),
                f"short leg contains HTB ticker {tk} (SI > 15%)",
            )

    def test_skip_asof_when_either_leg_decile_below_3(self) -> None:
        """Asof with insufficient names on either leg is dropped.
        Total cross-section = 20 → long_decile = 20 // 10 = 2 < 3 → skip."""
        df, scores, si_lookup, returns = _make_holdout_features(
            self.asof,
            n_long=10,
            n_short_eligible=10,
            n_short_ineligible=0,
        )
        history = _StubHistoryStore(returns)
        history._returns[("SPY", self.asof)] = 0.01
        si_client = _StubPolygonSI(si_lookup)

        (ls_returns, *_rest) = v4_driver._holdout_longshort_decile_returns_20d(
            df,
            scores,
            history,
            benchmark="SPY",
            polygon_si_client=si_client,
            shares_lookup=_stub_shares_lookup,
            si_max_short=0.15,
        )

        self.assertEqual(len(ls_returns), 0)

    def test_per_asof_return_equals_long_mean_minus_short_mean(self) -> None:
        """L/S spread is the difference of leg means; equal-weight."""
        df, scores, si_lookup, returns = _make_holdout_features(
            self.asof,
            n_long=100,
            n_short_eligible=50,
            n_short_ineligible=0,
        )
        history = _StubHistoryStore(returns)
        history._returns[("SPY", self.asof)] = 0.01
        si_client = _StubPolygonSI(si_lookup)

        (
            ls_returns,
            _bench,
            _ll,
            _sl,
            long_returns,
            short_returns,
            *_,
        ) = v4_driver._holdout_longshort_decile_returns_20d(
            df,
            scores,
            history,
            benchmark="SPY",
            polygon_si_client=si_client,
            shares_lookup=_stub_shares_lookup,
            si_max_short=0.15,
        )

        self.assertEqual(len(ls_returns), 1)
        self.assertAlmostEqual(
            float(ls_returns.iloc[0]),
            float(long_returns.iloc[0] - short_returns.iloc[0]),
            places=10,
        )
        # Long mean = +0.05, short mean = -0.03 → L/S = +0.08
        self.assertAlmostEqual(float(ls_returns.iloc[0]), 0.08, places=4)

    def test_nan_si_treated_as_short_ineligible(self) -> None:
        """NaN SI from polygon_si_client makes a name ineligible for shorting."""
        # 50 names, half with NaN SI (no polygon data)
        rows = []
        si_lookup: dict[str, float] = {}
        returns: dict[tuple[str, date], float] = {}
        for i in range(40):
            tk = f"OK{i:03d}"
            rows.append({"asof": self.asof, "ticker": tk})
            si_lookup[tk] = 0.05  # eligible
            returns[(tk, self.asof)] = 0.0
        for i in range(40):
            tk = f"NAN{i:03d}"
            rows.append({"asof": self.asof, "ticker": tk})
            # not in si_lookup → polygon returns None → si NaN
            returns[(tk, self.asof)] = 0.0
        df = pd.DataFrame(rows)
        scores = pd.Series(np.linspace(-1, 1, len(df)), index=df.index, dtype=float)
        history = _StubHistoryStore(returns)
        history._returns[("SPY", self.asof)] = 0.01
        si_client = _StubPolygonSI(si_lookup)

        (
            ls_returns,
            _bench,
            _ll,
            short_lists,
            *_rest,
        ) = v4_driver._holdout_longshort_decile_returns_20d(
            df,
            scores,
            history,
            benchmark="SPY",
            polygon_si_client=si_client,
            shares_lookup=_stub_shares_lookup,
            si_max_short=0.15,
        )

        self.assertEqual(len(ls_returns), 1)
        for tk in short_lists[0]:
            self.assertTrue(tk.startswith("OK"), f"NaN-SI name {tk} leaked into short leg")


if __name__ == "__main__":
    unittest.main()
