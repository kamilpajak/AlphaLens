"""MIN_BARS_REQUIRED filter parity for _CompoundInsiderPcScorer.prebuild_pc_panel.

The engine's `_build_histories` drops tickers whose `truncate_to(ticker, day)`
returns fewer than `MIN_BARS_REQUIRED` bars (default 220). The compound's
`prebuild_pc_panel` MUST mirror this filter — otherwise recent-IPO tickers
that have iVolatility SMD coverage but short OHLCV history slip into the
per-asof OLS as phantom rows. Because the P/C score uses cross-sectional
z-score standardisation, a single phantom row shifts μ and σ for EVERY
ticker at that asof, silently drifting α.

The cap=300 golden master fixture happens to use mature Russell 2000
names with long histories, so this regression vector is invisible there.
This test isolates the filter logic with a synthesised 2-ticker universe
(one mature, one IPO) to guarantee the filter runs correctly at full
universe scale.

Memo §3.5 + experiment_insider_pc_compound.py:444-457.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from alphalens_research.data.store.history import HistoryStore  # noqa: E402
from scripts import experiment_insider_pc_compound as exp  # noqa: E402


def _ohlcv_frame(start: pd.Timestamp, n_bars: int) -> pd.DataFrame:
    """Synthetic daily OHLCV with `n_bars` business-day index."""
    idx = pd.bdate_range(start=start, periods=n_bars)
    return pd.DataFrame(
        {
            "open": np.full(n_bars, 100.0),
            "high": np.full(n_bars, 101.0),
            "low": np.full(n_bars, 99.0),
            "close": np.full(n_bars, 100.0),
            "volume": np.full(n_bars, 1_000_000),
        },
        index=idx,
    )


class TestPrebuildPcPanelMinBarsFilter(unittest.TestCase):
    """Assert prebuild_pc_panel drops tickers with insufficient history."""

    def _make_scorer(self) -> exp._CompoundInsiderPcScorer:
        # Bypass __init__ — we only exercise prebuild_pc_panel's filter,
        # which depends on the class-level attribute lookup chain
        # (`getattr(self, "MIN_BARS_REQUIRED", _Engine.MIN_BARS_REQUIRED)`).
        # No need to wire up real Form4 / shares stores for this isolation
        # test.
        return exp._CompoundInsiderPcScorer.__new__(exp._CompoundInsiderPcScorer)

    def test_ipo_ticker_excluded_from_pc_panel(self):
        asof = date(2020, 6, 30)
        asof_ts = pd.Timestamp(asof)

        # MATURE: 500 bars ending at asof → 500 >= 220 → KEEP
        mature_hist = _ohlcv_frame(asof_ts - pd.Timedelta(days=800), 500)
        # IPO: 100 bars ending at asof → 100 < 220 → DROP
        ipo_hist = _ohlcv_frame(asof_ts - pd.Timedelta(days=150), 100)

        history_store = HistoryStore({"MATURE": mature_hist, "IPO": ipo_hist})

        # Simulate the post-build_feature_frame state: BOTH tickers were
        # scored by P/C (build_feature_frame doesn't consult OHLCV
        # length; it works off iVolatility SMD). Without the parity
        # filter, both would land in self._pc_panel.
        feature_frame = pd.DataFrame(
            {
                "asof": [asof.strftime("%Y-%m-%d")] * 2,
                "ticker": ["MATURE", "IPO"],
                # Minimal columns score_pc_abnormal_residual would consume.
                "abnormal_pcr": [0.5, -0.3],
                "reversal_1m": [0.0, 0.0],
                "momentum_6m": [0.0, 0.0],
                "rv_30d": [0.2, 0.2],
            }
        )

        with (
            mock.patch.object(exp, "build_feature_frame", return_value=feature_frame),
            mock.patch.object(
                exp,
                "score_pc_abnormal_residual",
                return_value=pd.Series([1.0, -1.0]),
            ),
        ):
            scorer = self._make_scorer()
            scorer._pc_panel = None
            scorer._smd_loader = lambda t: None  # never invoked (mocked)
            scorer.prebuild_pc_panel(
                universe=["MATURE", "IPO"],
                asof_dates=[asof],
                history_store=history_store,
            )

        panel = scorer._pc_panel
        self.assertIsNotNone(panel, "_pc_panel should be populated")
        # Panel is indexed by (asof, ticker).
        tickers_in_panel = {idx[1] for idx in panel.index}
        self.assertIn("MATURE", tickers_in_panel, "long-history ticker must remain")
        self.assertNotIn(
            "IPO",
            tickers_in_panel,
            "short-history ticker (<220 bars) must be filtered out — "
            "otherwise it pollutes cross-sectional z-score normalisation",
        )

    def test_threshold_is_engine_default_220(self):
        """Boundary test: exactly 220 bars passes, 219 fails."""
        asof = date(2020, 6, 30)
        asof_ts = pd.Timestamp(asof)

        passes_hist = _ohlcv_frame(asof_ts - pd.Timedelta(days=400), 220)
        fails_hist = _ohlcv_frame(asof_ts - pd.Timedelta(days=400), 219)

        history_store = HistoryStore({"AT220": passes_hist, "AT219": fails_hist})
        feature_frame = pd.DataFrame(
            {
                "asof": [asof.strftime("%Y-%m-%d")] * 2,
                "ticker": ["AT220", "AT219"],
                "abnormal_pcr": [0.5, -0.3],
                "reversal_1m": [0.0, 0.0],
                "momentum_6m": [0.0, 0.0],
                "rv_30d": [0.2, 0.2],
            }
        )

        with (
            mock.patch.object(exp, "build_feature_frame", return_value=feature_frame),
            mock.patch.object(
                exp,
                "score_pc_abnormal_residual",
                return_value=pd.Series([1.0, -1.0]),
            ),
        ):
            scorer = self._make_scorer()
            scorer._pc_panel = None
            scorer._smd_loader = lambda t: None
            scorer.prebuild_pc_panel(
                universe=["AT220", "AT219"],
                asof_dates=[asof],
                history_store=history_store,
            )

        tickers_in_panel = {idx[1] for idx in scorer._pc_panel.index}
        self.assertIn("AT220", tickers_in_panel, "exactly 220 bars must pass (>= threshold)")
        self.assertNotIn("AT219", tickers_in_panel, "219 bars must fail (< threshold)")


if __name__ == "__main__":
    unittest.main()
