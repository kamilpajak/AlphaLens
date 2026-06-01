"""L3 golden-master replay of the score stage (test-strategy Phase 3b).

Drives the REAL ``scorer.score_candidates`` deterministically and offline over a
frozen 4-row candidates slice (DFIN/QLYS/QUBT/MANH @ 2026-05-24). The two
peer-cohort-scale data dependencies are frozen at the scorer's own fetch
boundary (the companyfacts universe is ~764 tickers / ~57MB — too big to
cassette; ``score_insider`` reads Form-4 for every peer):

* ``_build_feature_fetcher`` → frozen ``{ticker: 16-field dict}`` (features.json)
* ``insider_signal.score_insider`` → frozen ``{ticker: {score_usd, pctl}}``
* ``mcap_filter.fetch_mcap`` → frozen map
* OHLCV → frozen brief-day parquets
* catalyst → frozen map-day events/news window

The REAL score logic runs over the frozen inputs: fcff / valuation percentile-
rank over the cohort, magic-formula rank, technicals over OHLCV, catalyst
strength, deep-drawdown-reversal, industry-cohort resolution, and
``compose_weighted_score`` → ``layer4_weighted_score``. Assert the composed
score + cohort + signal decisions, not exit codes. Missing fixture is fail-loud.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.thematic.mapping import catalyst_resolver
from alphalens_pipeline.thematic.screening import insider_signal, scorer
from alphalens_pipeline.thematic.verification import mcap_filter

from tests.golden.projection import score_projection

_ASOF = dt.date(2026, 5, 24)
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "score_day"
_GOLDEN = _FIXTURES / "golden" / "projection.json"

_REAL_FIND = catalyst_resolver.find_trigger_event


def _frozen_ohlcv_reader(upper: str, asof: dt.date) -> pd.DataFrame:
    path = _FIXTURES / "ohlcv" / f"{upper}_{asof.isoformat()}.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def _replay_score() -> pd.DataFrame:
    if not _GOLDEN.exists() or not (_FIXTURES / "features.json").exists():
        raise FileNotFoundError(
            f"golden fixtures missing under {_FIXTURES} — run "
            "scripts/record_golden_score.py (one-time capture) to record them"
        )
    cand = pd.read_parquet(_FIXTURES / "candidates.parquet")
    feature_map = {
        k.upper(): v for k, v in json.loads((_FIXTURES / "features.json").read_text()).items()
    }
    insider_map = {
        k.upper(): v for k, v in json.loads((_FIXTURES / "insider.json").read_text()).items()
    }
    mcap_map = {k.upper(): v for k, v in json.loads((_FIXTURES / "mcap.json").read_text()).items()}

    def _frozen_feature_fetcher(tickers=None):
        return lambda ticker, asof: feature_map.get(ticker.upper())

    def _frozen_insider(*, ticker, asof, peers, **kwargs):
        return insider_map.get(ticker.upper(), {"score_usd": None, "sector_percentile": None})

    with tempfile.TemporaryDirectory(prefix="ohlcv_replay_") as ohlcv_tmp:
        with (
            mock.patch.object(scorer, "_build_feature_fetcher", _frozen_feature_fetcher),
            mock.patch.object(insider_signal, "score_insider", _frozen_insider),
            mock.patch.object(
                mcap_filter, "fetch_mcap", lambda ticker, *, asof=None: mcap_map.get(ticker.upper())
            ),
            mock.patch.object(scorer, "_fetch_ohlcv_via_yfinance", _frozen_ohlcv_reader),
            mock.patch.object(scorer, "_THEMATIC_OHLCV_CACHE", Path(ohlcv_tmp)),
            mock.patch.object(
                catalyst_resolver,
                "find_trigger_event",
                functools.partial(
                    _REAL_FIND, events_dir=_FIXTURES / "events", news_dir=_FIXTURES / "news"
                ),
            ),
        ):
            return scorer.score_candidates(cand, asof=_ASOF)


class TestGoldenScoreReplay(unittest.TestCase):
    def test_replay_matches_golden_projection(self):
        got = score_projection(_replay_score())
        golden = json.loads(_GOLDEN.read_text())
        self.assertEqual(got, golden)

    def test_scores_are_decisive(self):
        # Every candidate gets an integer layer-4 score in [1, 5] — proves the
        # whole signal→compose pipeline ran over the frozen inputs.
        df = _replay_score()
        self.assertEqual(sorted(df["ticker"]), ["DFIN", "MANH", "QLYS", "QUBT"])
        for _, r in df.iterrows():
            self.assertIn(int(r["layer4_weighted_score"]), range(1, 6))

    def test_cohort_resolution_not_thin(self):
        # The frozen feature fetcher must actually feed the tradeable-peer
        # filter so the industry cohort resolves (not "thin"). A regression
        # that starves the filter would collapse every cohort to thin.
        df = _replay_score()
        levels = set(df["peer_cohort_level"])
        self.assertTrue(levels <= {"sic4", "sic3", "ff48", "thin"})
        self.assertTrue(any(level != "thin" for level in levels))

    def test_replay_is_deterministic(self):
        a = score_projection(_replay_score())
        b = score_projection(_replay_score())
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
