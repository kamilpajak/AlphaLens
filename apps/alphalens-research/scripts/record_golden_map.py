"""Record the L3 golden-master fixtures for the map-themes stage (Phase 3b).

ONE-TIME live capture. Drives the REAL ``orchestrator.map_themes`` once over a
single theme + asof and freezes everything the hermetic replay test needs.

The map-themes stage hits SIX external surfaces; this recorder captures each
faithfully so the replay drives the REAL parsing / gate logic offline:

  1. Pro LLM (theme_mapper)         -> ReplayOpenRouter cassette  (cassettes_llm/)
  2. Polygon press (recent_press)   -> VendorCassette cassette    (cassettes_vendor/)
  3. SEC 10-K (tenk_grep)           -> VendorCassette cassette    (cassettes_vendor/)
  4. yfinance mcap (mcap_filter)    -> frozen {ticker: mcap} map  (mcap.json)
  5. Form-4 insider (insider)       -> trimmed hive parquet       (form4_parquet/)
  6. Catalyst (catalyst_resolver)   -> frozen events/news window  (events/, news/)

Surfaces 1-3 have a canonical HTTP client, so they go through cassettes and the
replay exercises the real parse/gate code. Surfaces 4-6 have NO client
(yfinance, on-disk parquet), so they are frozen seams + dir redirects — the
honest choice, and the real classifier / resolver logic still runs over the
frozen data.

    OPENROUTER_API_KEY=... POLYGON_API_KEY=... SEC_EDGAR_USER_AGENT=... \
        uv run python -m scripts.record_golden_map
    # (run from apps/alphalens-research; needs ~/.alphalens/{thematic_events,
    #  thematic_news,form4_parquet} populated for the window)
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.data.alt_data.openrouter_client import OpenRouterClient
from alphalens_pipeline.data.alt_data.polygon_client import PolygonClient
from alphalens_pipeline.data.alt_data.sec_edgar_client import get_default_sec_client
from alphalens_pipeline.thematic.mapping import catalyst_resolver, orchestrator
from alphalens_pipeline.thematic.verification import mcap_filter, recent_press, tenk_grep
from alphalens_pipeline.thematic.verification.tenk_grep import _find_cached
from tests.golden.projection import map_themes_projection
from tests.golden.replay_client import RecordingOpenRouter
from tests.golden.vendor_cassette import RecordingVendor

THEME = "quantum_computing"
ASOF = dt.date(2026, 5, 24)
# 30-day catalyst + press window around ASOF (verified on-disk).
_WINDOW_DATES = ("2026-05-15", "2026-05-18", "2026-05-24")
# _classification_years(2026) = {2023, 2024, 2025, 2026}.
_FORM4_YEARS = (2023, 2024, 2025, 2026)

_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "golden" / "fixtures" / "map_day"
_ALPHALENS = Path.home() / ".alphalens"


def _freeze_window(area: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for date in _WINDOW_DATES:
        src = _ALPHALENS / area / f"{date}.parquet"
        if src.exists():
            shutil.copyfile(src, dest / f"{date}.parquet")


def _build_form4_fixture(tickers: set[str]) -> None:
    """Trim the 37MB hive Form-4 corpus to the candidate tickers' insiders.

    Two-pass per the Cohen-Malloy contract: (1) find the CIKs that traded any
    candidate ticker in the classification years; (2) keep EVERY row for those
    CIKs (cross-ticker history) so the classifier sees the full pattern — the
    same view ``has_opportunistic_buy`` loads. Writes the same hive layout.
    """
    root = _ALPHALENS / "form4_parquet"
    out_root = _FIXTURES / "form4_parquet"
    upper = {t.upper() for t in tickers}
    ciks: set = set()
    per_year: dict[int, pd.DataFrame] = {}
    for year in _FORM4_YEARS:
        part = root / f"transaction_year={year}" / "compacted.parquet"
        if not part.exists():
            continue
        df = pd.read_parquet(part)
        per_year[year] = df
        ciks |= set(df[df["ticker"].isin(upper)]["reporting_owner_cik"].unique())
    total = 0
    for year, df in per_year.items():
        slice_df = df[df["reporting_owner_cik"].isin(ciks)].reset_index(drop=True)
        if slice_df.empty:
            continue
        dest = out_root / f"transaction_year={year}"
        dest.mkdir(parents=True, exist_ok=True)
        slice_df.to_parquet(dest / "compacted.parquet", index=False)
        total += len(slice_df)
    print(f"  form4 fixture: {len(ciks)} insider CIKs, {total} rows across {len(per_year)} years")


def _freeze_tenk_cache(tickers: set[str]) -> None:
    """Copy each kept ticker's selected 10-K text cache file into the fixture.

    Uses the real ``_find_cached`` selector so the frozen file is exactly the
    one ``fetch_10k_text`` would pick at ``ASOF`` — the replay then reads it
    back identically (cache hit before any CIK/SEC resolution).
    """
    dest = _FIXTURES / "tenk_cache"
    dest.mkdir(parents=True, exist_ok=True)
    real_cache = _ALPHALENS / "thematic_tenk"
    frozen = []
    for ticker in sorted(tickers):
        selected = _find_cached(ticker, real_cache, asof=ASOF)
        if selected is not None:
            shutil.copyfile(selected, dest / selected.name)
            frozen.append(selected.name)
    print(f"  tenk fixture: {len(frozen)} 10-K text files {frozen}")


def _trim_polygon_cassettes(tickers: set[str]) -> None:
    """Shrink the recorded Polygon firehose to candidate-ticker rows only.

    The window-universe call fetches ALL Polygon news over 30 days
    (``ticker=None``) — ~6000 items / ~15MB, far too big to commit. The press
    gate (``has_theme_in_press_frame``) masks the frame to rows tagged with the
    candidate ticker BEFORE grepping, so rows for other tickers cannot change
    any verdict. Keep only rows mentioning a candidate ticker — verdict-
    equivalent, ~100KB. The cassette key is over the request args (unchanged),
    so the trimmed payload still serves the same call.
    """
    upper = {t.upper() for t in tickers}
    vendor_dir = _FIXTURES / "cassettes_vendor"
    for path in vendor_dir.glob("*.json"):
        rec = json.loads(path.read_text())
        if rec.get("method") != "get_news_range":
            continue
        before = len(rec["payload"])
        rec["payload"] = [
            it
            for it in rec["payload"]
            if upper & {str(t).upper() for t in (it.get("tickers") or [])}
        ]
        path.write_text(json.dumps(rec, indent=2, sort_keys=True, ensure_ascii=False, default=str))
        print(
            f"  trimmed polygon cassette {path.name[:12]}: {before} -> {len(rec['payload'])} rows"
        )


def main() -> None:
    for env in ("OPENROUTER_API_KEY", "POLYGON_API_KEY", "SEC_EDGAR_USER_AGENT"):
        if not os.environ.get(env):
            raise SystemExit(f"{env} must be set for the live capture")

    events_fix = _FIXTURES / "events"
    news_fix = _FIXTURES / "news"
    golden_dir = _FIXTURES / "golden"
    llm_dir = _FIXTURES / "cassettes_llm"
    vendor_dir = _FIXTURES / "cassettes_vendor"
    for d in (golden_dir, llm_dir, vendor_dir):
        d.mkdir(parents=True, exist_ok=True)

    _freeze_window("thematic_events", events_fix)
    _freeze_window("thematic_news", news_fix)

    # Capture the genuine functions BEFORE patching so the partials wrap the
    # real logic (dir / cache_dir redirected), not the patched stand-in.
    real_find = catalyst_resolver.find_trigger_event
    real_fwu = recent_press.fetch_window_universe
    real_htirp = recent_press.has_theme_in_recent_press
    real_mcap = mcap_filter.fetch_mcap

    mcap_capture: dict[str, float | None] = {}

    def _teed_mcap(ticker: str, *, asof: dt.date | None = None):
        value = real_mcap(ticker, asof=asof)
        mcap_capture[ticker.upper()] = value
        return value

    rec_pro = RecordingOpenRouter(
        OpenRouterClient(api_key=os.environ["OPENROUTER_API_KEY"]), llm_dir
    )
    rec_poly = RecordingVendor(PolygonClient(os.environ["POLYGON_API_KEY"]), vendor_dir)
    rec_sec = RecordingVendor(get_default_sec_client(), vendor_dir)

    # Fresh empty press cache so the Polygon firehose actually fires (gets
    # recorded); TemporaryDirectory cleans it on exit (no /tmp leak).
    with tempfile.TemporaryDirectory(prefix="press_record_") as press_tmp_str:
        press_tmp = Path(press_tmp_str)
        with (
            mock.patch.object(orchestrator, "_init_pro_client", lambda api_key: rec_pro),
            mock.patch.object(orchestrator, "PolygonClient", lambda *a, **k: rec_poly),
            mock.patch.object(orchestrator, "get_default_polygon_client", lambda: rec_poly),
            mock.patch.object(tenk_grep, "get_default_sec_client", lambda: rec_sec),
            mock.patch.object(
                catalyst_resolver,
                "find_trigger_event",
                functools.partial(real_find, events_dir=events_fix, news_dir=news_fix),
            ),
            mock.patch.object(
                recent_press,
                "fetch_window_universe",
                functools.partial(real_fwu, cache_dir=press_tmp),
            ),
            mock.patch.object(
                recent_press,
                "has_theme_in_recent_press",
                functools.partial(real_htirp, cache_dir=press_tmp),
            ),
            mock.patch.object(mcap_filter, "fetch_mcap", _teed_mcap),
        ):
            df = orchestrator.map_themes(
                themes=[THEME],
                asof=ASOF,
                api_key=os.environ["OPENROUTER_API_KEY"],
                polygon_api_key="dummy",  # forces the `if polygon_api_key:` branch
                output_dir=golden_dir,
                market_cap_range=orchestrator.DEFAULT_MCAP_RANGE,
            )

    # Form-4 fixture: trim to every ticker an mcap lookup touched (= every
    # proposed candidate that survived to the verify stage and beyond).
    _build_form4_fixture(set(mcap_capture.keys()))

    # tenk fixture: the tenk gate read its 10-K text from the on-disk cache
    # (~/.alphalens/thematic_tenk) — no live SEC call fired (verified: zero SEC
    # cassettes). Freeze the selected 10-K text per kept ticker so the replay
    # drives the REAL grep over frozen text, offline, no SEC client at all.
    _freeze_tenk_cache(set(df["ticker"]) if len(df) else set())

    # Shrink the 30-day Polygon firehose cassette (~15MB) to candidate rows.
    _trim_polygon_cassettes(set(df["ticker"]) if len(df) else set())

    (_FIXTURES / "mcap.json").write_text(
        json.dumps(dict(sorted(mcap_capture.items())), indent=2, sort_keys=True)
    )
    (golden_dir / "projection.json").write_text(
        json.dumps(map_themes_projection(df), indent=2, sort_keys=True)
    )
    n_llm = len(list(llm_dir.glob("*.json")))
    n_vendor = len(list(vendor_dir.glob("*.json")))
    print(
        f"captured {len(df)} mapped rows; tickers={sorted(df['ticker']) if len(df) else []}; "
        f"{n_llm} LLM + {n_vendor} vendor cassettes; mcap for {len(mcap_capture)} tickers -> {_FIXTURES}"
    )


if __name__ == "__main__":
    main()
