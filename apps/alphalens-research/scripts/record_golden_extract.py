"""Record the L3 golden-master fixtures for the extract stage (Phase 3b).

ONE-TIME live capture. Drives the REAL ``extract_daily`` once over a frozen,
6-row news slice and freezes everything the hermetic replay test needs:

  fixtures/extract_day/
    2026-05-24.parquet      – the frozen 6-row input (news_dir entry)
    company_tickers.json    – trimmed resolver table (no secrets)
    cassettes/<key>.json     – real DeepSeek Flash responses, keyed on the request
    golden/projection.json   – schema + per-row routing + typed-field presence

The 6 rows are chosen to exercise BOTH extraction paths:

  * 3 SYNTHETIC press-release rows that deterministically hit the M&A /
    earnings / guidance templates (verified against the shipped engine). The
    template path is free + LLM-free, so these need no cassette and produce
    rich ``template_fields_json``. They are synthetic because the live news
    corpus is ~all Motley-Fool listicles that almost never hit a template
    (empirically 1/200 on 2026-05-24) — a real slice would be Flash-only and
    would not lock the typed-template path at all.
  * 3 REAL Flash-fallback rows sliced from ``~/.alphalens/thematic_news`` by
    id. These fall through to DeepSeek Flash; the recorder tees the real
    response into a cassette so the replay is byte-identical + offline.

The replay test (``test_golden_extract_replay.py``) reads ONLY these fixtures
— no network. Re-run this script (with OPENROUTER_API_KEY set) to refresh the
cassettes after a deliberate prompt / model / template change; review the
fixture diff in the PR.

    OPENROUTER_API_KEY=... uv run python -m scripts.record_golden_extract
    # (run from apps/alphalens-research; needs ~/.alphalens/thematic_news/2026-05-24.parquet)
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pandas as pd
from alphalens_pipeline.data.alt_data.openrouter_client import OpenRouterClient
from alphalens_pipeline.thematic.extraction import event_extractor
from alphalens_pipeline.thematic.extraction.templates.entity_resolver import EntityResolver
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS
from tests.golden.projection import extract_projection
from tests.golden.replay_client import RecordingOpenRouter

ASOF = dt.date(2026, 5, 24)
_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "golden" / "fixtures" / "extract_day"
_ALPHALENS = Path.home() / ".alphalens"

# 3 synthetic press-release rows. Each is crafted (and verified against the
# shipped TemplateEngine) to hit exactly one template via the positional
# entity-role assignment + the named predicates. Kept free of cross-template
# vocabulary so first-match-wins lands on the intended template:
#   - M&A: "$4.5 billion" + "acquire" + 2 entities (acquirer, target)
#   - earnings: "EPS of $1.42" + "beats/topped estimates" (earnings tried first)
#   - guidance: "Raises Full-Year Guidance" + NO $ amount (so financing, which
#     needs amount_mentioned, drops and guidance_update wins)
_SYNTHETIC: list[dict] = [
    {
        "id": "syn_mna",
        "source": "businesswire",
        "tickers": ["ACQR", "TGTC"],
        "title": "Acquirer Corp to Acquire Target Systems for $4.5 billion",
        "body": (
            "Acquirer Corp (NASDAQ: ACQR) today announced a definitive agreement to "
            "acquire Target Systems for $4.5 billion in cash. The deal closes in Q3."
        ),
        "url": "https://example.com/syn_mna",
    },
    {
        "id": "syn_earn",
        "source": "globenewswire",
        "tickers": ["ERNG"],
        "title": "Earnings Inc Reports Q1 Results and Beats Estimates",
        "body": (
            "Earnings Inc (NASDAQ: ERNG) reported quarterly EPS of $1.42 and topped "
            "estimates. Revenue of $2.1 billion grew 12% year over year."
        ),
        "url": "https://example.com/syn_earn",
    },
    {
        "id": "syn_guid",
        "source": "prnewswire",
        "tickers": ["GUID"],
        "title": "Guidance Co Raises Full-Year Guidance",
        "body": (
            "Guidance Co (NYSE: GUID) today raised its full-year guidance for fiscal "
            "2026, citing strong demand across all segments."
        ),
        "url": "https://example.com/syn_guid",
    },
]

# 3 real Flash-fallback rows, sliced by id from the live 2026-05-24 corpus.
_REAL_IDS: list[str] = [
    "d72a7180febbe7b99db5ff442302f18a1bdc543b10902fa34e44a5b735f7f088",  # INTU
    "590fda05083a0878dbac928677d286db00d6e1266205ca0d8c181f55af551d00",  # MU
    "54920a60fee4231ee5650ccbf0b73c18b1c1c3ceeaf393778952720e750e2ec9",  # RTX
]

# Trimmed resolver table — display names only, no secrets. The resolver is
# permissive (unknown ticker → name=ticker), so this only enriches the
# human-readable name; the projection keys on ticker, not name.
_COMPANY_TICKERS: dict[str, dict] = {
    "0": {"cik_str": 1, "ticker": "ACQR", "title": "Acquirer Corp"},
    "1": {"cik_str": 2, "ticker": "TGTC", "title": "Target Systems Inc"},
    "2": {"cik_str": 3, "ticker": "ERNG", "title": "Earnings Inc"},
    "3": {"cik_str": 4, "ticker": "GUID", "title": "Guidance Co"},
    "4": {"cik_str": 5, "ticker": "INTU", "title": "Intuit Inc."},
    "5": {"cik_str": 6, "ticker": "MU", "title": "Micron Technology Inc."},
    "6": {"cik_str": 7, "ticker": "RTX", "title": "RTX Corporation"},
}


def _build_news_frame() -> pd.DataFrame:
    """Deterministic 6-row news frame: 3 synthetic + 3 real (sliced by id)."""
    real_src = _ALPHALENS / "thematic_news" / f"{ASOF.isoformat()}.parquet"
    real_all = pd.read_parquet(real_src)
    real = real_all[real_all["id"].isin(_REAL_IDS)].reset_index(drop=True)
    if len(real) != len(_REAL_IDS):
        missing = set(_REAL_IDS) - set(real["id"])
        raise SystemExit(f"real flash rows missing from {real_src}: {missing}")

    synth_rows = []
    for row in _SYNTHETIC:
        synth_rows.append(
            {
                "id": row["id"],
                "source": row["source"],
                "timestamp": pd.Timestamp("2026-05-24T12:00:00Z"),
                "tickers": row["tickers"],
                "title": row["title"],
                "body": row["body"],
                "url": row["url"],
                "keywords": [],
                "extra": "{}",
            }
        )
    synth = pd.DataFrame(synth_rows, columns=NEWS_COLUMNS)
    synth["timestamp"] = pd.to_datetime(synth["timestamp"], utc=True)

    combined = pd.concat([synth, real[NEWS_COLUMNS]], ignore_index=True)
    return combined


def main() -> None:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY must be set for the live capture")

    cassettes = _FIXTURES / "cassettes"
    golden_dir = _FIXTURES / "golden"
    for d in (cassettes, golden_dir):
        d.mkdir(parents=True, exist_ok=True)

    news = _build_news_frame()
    news.to_parquet(_FIXTURES / f"{ASOF.isoformat()}.parquet", index=False)

    company_tickers_path = _FIXTURES / "company_tickers.json"
    company_tickers_path.write_text(json.dumps(_COMPANY_TICKERS, indent=2, sort_keys=True))

    recorder = RecordingOpenRouter(OpenRouterClient(api_key=api_key), cassettes)
    resolver = EntityResolver(company_tickers_path=company_tickers_path)

    # events_dir=golden_dir writes golden/{asof}.parquet as a side effect. The
    # replay test asserts against projection.json (below), NOT this parquet —
    # it is kept only as a human-readable golden artifact so a reviewer can see
    # the full extracted rows (themes, second-order implications, typed fields)
    # behind the projection. The replay test writes to its own temp dir.
    events = event_extractor.extract_daily(
        date=ASOF,
        news_dir=_FIXTURES,
        events_dir=golden_dir,
        llm_client=recorder,
        resolver=resolver,
    )

    (golden_dir / "projection.json").write_text(
        json.dumps(extract_projection(events), indent=2, sort_keys=True)
    )
    n_template = int((events["extraction_method"] == "template").sum())
    n_flash = int((events["extraction_method"] == "flash").sum())
    print(
        f"captured {len(events)} events ({n_template} template, {n_flash} flash), "
        f"{len(list(cassettes.glob('*.json')))} cassettes → {_FIXTURES}"
    )


if __name__ == "__main__":
    main()
