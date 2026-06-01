"""Record the L3 golden-master fixtures for the brief-generation stage.

ONE-TIME live capture (test-strategy Phase 3). Drives the REAL
``generate_briefs`` once against the live OpenRouter API on a small, frozen
slice of REAL scored candidates, and freezes everything the hermetic replay
test needs:

  fixtures/brief_day/
    scored.parquet          – the frozen input slice (real, from ~/.alphalens)
    ohlcv/<T>_<asof>.parquet – frozen OHLCV per ticker (real, from ~/.alphalens)
    cassettes/<key>.json     – real DeepSeek responses, keyed on the request
    golden/brief.parquet     – the produced brief parquet (the golden artifact)
    golden/projection.json   – schema + row-count + aggregates + stable exemplar

The replay test (``test_golden_brief_replay.py``) reads ONLY these fixtures —
no network. Re-run this script (with OPENROUTER_API_KEY set) to refresh the
cassettes after a deliberate prompt / model change; review the fixture diff in
the PR.

    OPENROUTER_API_KEY=... uv run python -m scripts.record_golden_brief
    # (run from apps/alphalens-research; needs ~/.alphalens/thematic_{scored,ohlcv})
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.data.alt_data.openrouter_client import OpenRouterClient
from alphalens_pipeline.thematic.argumentation import orchestrator as brief_orch
from tests.golden.projection import brief_projection
from tests.golden.replay_client import RecordingOpenRouter

# The frozen day + the slice. All four tickers have a matching OHLCV cache file
# for this asof; DFIN/QLYS score >=4 (route to Pro), QUBT/MANH <4 (route to
# Flash) — so the golden exercises both model paths.
ASOF = dt.date(2026, 5, 24)
SLICE_TICKERS = ("DFIN", "QLYS", "QUBT", "MANH")

_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "golden" / "fixtures" / "brief_day"
_ALPHALENS = Path.home() / ".alphalens"


def _frozen_earnings(*, ticker: str, asof: dt.date):
    """Deterministic stand-in for the yfinance earnings lookup."""
    return None


def _build_ohlcv_loader(ohlcv_dir: Path):
    def _loader(ticker: str, asof: dt.date) -> pd.DataFrame:
        path = ohlcv_dir / f"{ticker}_{asof.isoformat()}.parquet"
        return pd.read_parquet(path) if path.exists() else pd.DataFrame()

    return _loader


def main() -> None:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY must be set for the live capture")

    cassettes = _FIXTURES / "cassettes"
    ohlcv_dir = _FIXTURES / "ohlcv"
    golden_dir = _FIXTURES / "golden"
    for d in (cassettes, ohlcv_dir, golden_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Freeze the real scored slice.
    scored_src = _ALPHALENS / "thematic_scored" / f"{ASOF.isoformat()}.parquet"
    scored = pd.read_parquet(scored_src)
    scored = (
        scored[scored["ticker"].isin(SLICE_TICKERS)]
        .drop_duplicates("ticker")
        .reset_index(drop=True)
    )
    scored.to_parquet(_FIXTURES / "scored.parquet", index=False)

    # Freeze the matching real OHLCV.
    for ticker in scored["ticker"]:
        src = _ALPHALENS / "thematic_ohlcv" / f"{ticker}_{ASOF.isoformat()}.parquet"
        if src.exists():
            shutil.copyfile(src, ohlcv_dir / src.name)

    recorder = RecordingOpenRouter(OpenRouterClient(api_key=api_key), cassettes)

    with (
        mock.patch.object(brief_orch, "_build_clients", return_value=(recorder, recorder)),
        mock.patch(
            "alphalens_pipeline.thematic.sources.earnings_calendar.fetch_next_earnings",
            _frozen_earnings,
        ),
    ):
        brief = brief_orch.generate_briefs(
            scored,
            asof=ASOF,
            output_dir=golden_dir,
            ohlcv_loader=_build_ohlcv_loader(ohlcv_dir),
        )

    (golden_dir / "projection.json").write_text(
        json.dumps(brief_projection(brief), indent=2, sort_keys=True)
    )
    print(
        f"captured {len(brief)} briefs, {len(list(cassettes.glob('*.json')))} cassettes → {_FIXTURES}"
    )


if __name__ == "__main__":
    main()
