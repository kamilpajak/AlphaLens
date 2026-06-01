"""Record the L3 golden-master fixtures for the score stage (Phase 3b).

ONE-TIME capture. Drives the REAL ``scorer.score_candidates`` over a frozen
4-row candidates slice and freezes everything the hermetic replay needs.

## Boundary choice (why freeze the feature/insider fetchers, not cassette SEC)
The score stage expands each candidate into its FULL SIC peer cohort —
``_collect_universe`` yields ~764 tickers for the 4-ticker slice. A faithful
companyfacts capture would be ~57MB of parquet (or larger as SEC cassettes),
and ``score_insider`` reads Form-4 for every one of those peers. Both are
data-layer dependencies, not score logic. So the golden freezes at the
data-fetch boundary the scorer itself uses:

  * ``_build_feature_fetcher`` output  -> features.json  ({ticker: 16-field dict})
  * ``insider_signal.score_insider``   -> insider.json   ({ticker: {score_usd, pctl}})
  * ``mcap_filter.fetch_mcap``         -> mcap.json
  * OHLCV (``_fetch_ohlcv_via_yfinance``) -> reuse brief_day/ohlcv (4 tickers)
  * catalyst window                    -> reuse map_day/{events,news}

The REAL score logic still runs over the frozen inputs: the fcff / valuation
percentile-rank over the cohort, magic-formula rank, technicals over OHLCV,
catalyst strength, deep-drawdown-reversal, the industry-cohort resolution, and
``compose_weighted_score`` -> ``layer4_weighted_score``. This is the same
boundary the brief golden uses (freeze OHLCV, not yfinance internals).

    SEC_EDGAR_USER_AGENT=... uv run python -m scripts.record_golden_score
    # (run from apps/alphalens-research; companyfacts cache must be warm in
    #  ~/.alphalens/companyfacts_parquet — no live SEC fetch on a warm cache)
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
from alphalens_pipeline.thematic.mapping import catalyst_resolver
from alphalens_pipeline.thematic.screening import insider_signal, scorer
from alphalens_pipeline.thematic.verification import mcap_filter
from tests.golden.projection import score_projection

ASOF = dt.date(2026, 5, 24)
SLICE_TICKERS = ("DFIN", "QLYS", "QUBT", "MANH")

_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "golden" / "fixtures" / "score_day"
_BRIEF = _FIXTURES.parent / "brief_day"
_MAP = _FIXTURES.parent / "map_day"
_ALPHALENS = Path.home() / ".alphalens"


def _json_default(obj):
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return str(obj)


def _frozen_ohlcv_reader(ohlcv_dir: Path):
    def _reader(upper: str, asof: dt.date) -> pd.DataFrame:
        path = ohlcv_dir / f"{upper}_{asof.isoformat()}.parquet"
        return pd.read_parquet(path) if path.exists() else pd.DataFrame()

    return _reader


def main() -> None:
    ohlcv_dir = _FIXTURES / "ohlcv"
    events_dir = _FIXTURES / "events"
    news_dir = _FIXTURES / "news"
    golden_dir = _FIXTURES / "golden"
    for d in (ohlcv_dir, events_dir, news_dir, golden_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Freeze the 4-row candidates slice from the real map output.
    cand_all = pd.read_parquet(_ALPHALENS / "thematic_candidates" / f"{ASOF.isoformat()}.parquet")
    cand = (
        cand_all[cand_all["ticker"].isin(SLICE_TICKERS)]
        .drop_duplicates("ticker")
        .reset_index(drop=True)
    )
    cand.to_parquet(_FIXTURES / "candidates.parquet", index=False)

    # Reuse the brief OHLCV (same tickers/asof) + map catalyst window.
    for t in SLICE_TICKERS:
        src = _BRIEF / "ohlcv" / f"{t}_{ASOF.isoformat()}.parquet"
        if src.exists():
            shutil.copyfile(src, ohlcv_dir / src.name)
    for area, dest in (("events", events_dir), ("news", news_dir)):
        for p in (_MAP / area).glob("*.parquet"):
            shutil.copyfile(p, dest / p.name)

    real_build = scorer._build_feature_fetcher
    real_insider = insider_signal.score_insider
    real_mcap = mcap_filter.fetch_mcap
    real_find = catalyst_resolver.find_trigger_event
    feature_cap: dict[str, dict] = {}
    insider_cap: dict[str, dict] = {}
    mcap_cap: dict[str, float | None] = {}

    def _teed_build(tickers):
        real_fetcher = real_build(tickers)

        def _teed(ticker, asof):
            out = real_fetcher(ticker, asof)
            if out is not None:
                feature_cap[ticker.upper()] = out
            return out

        return _teed

    def _teed_insider(*, ticker, asof, peers, **kw):
        out = real_insider(ticker=ticker, asof=asof, peers=peers, **kw)
        insider_cap[ticker.upper()] = out
        return out

    def _teed_mcap(ticker, *, asof=None):
        v = real_mcap(ticker, asof=asof)
        mcap_cap[ticker.upper()] = v
        return v

    with (
        tempfile.TemporaryDirectory(prefix="ohlcv_record_") as ohlcv_tmp,
        mock.patch.object(scorer, "_build_feature_fetcher", _teed_build),
        mock.patch.object(insider_signal, "score_insider", _teed_insider),
        mock.patch.object(mcap_filter, "fetch_mcap", _teed_mcap),
        mock.patch.object(scorer, "_fetch_ohlcv_via_yfinance", _frozen_ohlcv_reader(ohlcv_dir)),
        mock.patch.object(scorer, "_THEMATIC_OHLCV_CACHE", Path(ohlcv_tmp)),
        mock.patch.object(
            catalyst_resolver,
            "find_trigger_event",
            functools.partial(real_find, events_dir=events_dir, news_dir=news_dir),
        ),
    ):
        scored = scorer.score_candidates(cand, asof=ASOF)

    (_FIXTURES / "features.json").write_text(
        json.dumps(
            dict(sorted(feature_cap.items())), indent=2, sort_keys=True, default=_json_default
        )
    )
    (_FIXTURES / "insider.json").write_text(
        json.dumps(
            dict(sorted(insider_cap.items())), indent=2, sort_keys=True, default=_json_default
        )
    )
    (_FIXTURES / "mcap.json").write_text(
        json.dumps(dict(sorted(mcap_cap.items())), indent=2, sort_keys=True, default=_json_default)
    )
    (golden_dir / "projection.json").write_text(
        json.dumps(score_projection(scored), indent=2, sort_keys=True)
    )
    print(
        f"captured {len(scored)} scored rows; tickers={sorted(scored['ticker'])}; "
        f"features={len(feature_cap)}, insider={len(insider_cap)}, mcap={len(mcap_cap)} -> {_FIXTURES}"
    )


if __name__ == "__main__":
    main()
