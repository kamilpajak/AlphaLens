"""Cover the ``_close_at`` helper after dropping its redundant inner slice.

The helper assumes the caller has already filtered the history frame to
``index <= asof``; this regression test pins both the empty-frame return
path and the latest-close return on a pre-sliced input.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import experiment_insider_pc_compound as exp  # noqa: E402


class TestCloseAt(unittest.TestCase):
    def _hist(self) -> pd.DataFrame:
        idx = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"], utc=True)
        return pd.DataFrame({"close": [10.0, 11.0, 12.0, 13.0]}, index=idx)

    def test_returns_last_close_on_sliced_history(self):
        history = self._hist()
        asof = pd.Timestamp("2024-01-03", tz="UTC")
        sliced = history[history.index <= asof]
        self.assertEqual(exp._close_at(sliced, asof), 12.0)

    def test_returns_none_on_empty_sliced_history(self):
        empty = self._hist().iloc[0:0]
        asof = pd.Timestamp("2024-01-01", tz="UTC")
        self.assertIsNone(exp._close_at(empty, asof))
