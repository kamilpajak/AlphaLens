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
from tests.golden.replay_client import GOLDEN_RECORDED_MAX_OUTPUT_TOKENS, RecordingOpenRouter

# The frozen day + the slice. Re-cut 2026-08-20 for the grounding-and-prose-
# honesty golden re-baseline (docs/research/golden_rebaseline_recorded_2026_08_20.md):
# the previous slice (2026-05-24) predates PR #1066 and carries zero channel_*
# columns, so replaying it would render an EMPTY channel block on every row and
# never exercise the new causal-support / grounding contract at all.
#
# ASOF is a genuine post-#1066, post-grounding-and-prose-honesty live run of
# THIS branch's map-themes + score stages (real OpenRouter/Polygon/SEC/yfinance
# calls, real news/event window synced from the VPS store) — not a frozen
# fixture. All five tickers have a matching OHLCV cache file for this asof.
#
# The five candidates cover: channel_support_status in {suggestive,
# not_established} and channel_grounding_status in {grounded, candidate_misfit,
# theme_misroute} — PSNL/ABUS/MRVI are suggestive+grounded, CRSP is
# not_established+candidate_misfit, RDN is not_established+theme_misroute.
#
# NO `established` row is included because none occurred: two full days of live
# map-themes runs on this branch (2026-08-18 + 2026-08-19, 22 verified
# candidates total) produced zero. See the provenance memo for the reading of
# why. All five score layer4_weighted_score < 4, so this recording exercises
# only the Flash routing path — Pro-path coverage is not re-verified by this
# recording (it was covered by the superseded 2026-05-24 slice).
ASOF = dt.date(2026, 8, 19)
SLICE_TICKERS = ("PSNL", "CRSP", "ABUS", "MRVI", "RDN")

_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "golden" / "fixtures" / "brief_day"
_ALPHALENS = Path.home() / ".alphalens"


def _frozen_earnings(*, ticker: str, asof: dt.date, today: dt.date | None = None):
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
            # Record at the pinned golden cap so the cassette keys (which include
            # max_tokens) match what the replay test drives — decoupled from the
            # production default (see GOLDEN_RECORDED_MAX_OUTPUT_TOKENS).
            base_max_output_tokens=GOLDEN_RECORDED_MAX_OUTPUT_TOKENS,
        )

    (golden_dir / "projection.json").write_text(
        json.dumps(brief_projection(brief), indent=2, sort_keys=True)
    )
    print(
        f"captured {len(brief)} briefs, {len(list(cassettes.glob('*.json')))} cassettes → {_FIXTURES}"
    )


if __name__ == "__main__":
    main()
