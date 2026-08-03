"""Record the L3 golden-master fixtures for the map-themes stage (Phase 3b).

Live capture. Drives the REAL ``orchestrator.map_themes`` once over one
fixture's theme + asof and freezes everything the hermetic replay test needs.

The map-themes stage hits SIX external surfaces; this recorder captures each
faithfully so the replay drives the REAL parsing / gate logic offline:

  1. Pro LLM (theme_mapper)         -> ReplayOpenRouter cassette  (cassettes_llm/)
  2. Polygon press (recent_press)   -> VendorCassette cassette    (cassettes_vendor/)
  3. SEC 10-K (tenk_grep)           -> frozen 10-K text cache     (tenk_cache/)
  4. yfinance mcap (mcap_filter)    -> frozen {ticker: mcap} map  (mcap.json)
  5. Form-4 insider (insider)       -> trimmed hive parquet       (form4_parquet/)
  6. Catalyst (catalyst_resolver)   -> frozen events/news window  (events/, news/)

Surfaces 1-2 have a canonical HTTP client, so they go through cassettes and the
replay exercises the real parse/gate code. Surfaces 3-6 have NO client at the
seam the replay needs (on-disk caches, yfinance, parquet), so they are frozen
files + dir redirects — the honest choice, and the real grep / classifier /
resolver logic still runs over the frozen data.

WHICH FIXTURE
-------------
``--fixture NAME`` selects one recorded case from
``tests.golden.map_fixtures.MAP_FIXTURES``; the fixture owns its theme, asof,
event window and output directory. Fixtures are ADDED, never swapped — a second
case answers a different question from the first.

TWO MODES
---------
Full capture — re-records ALL SIX surfaces from the live vendors and the local
``~/.alphalens`` caches::

    OPENROUTER_API_KEY=... POLYGON_API_KEY=... SEC_EDGAR_USER_AGENT=... \
        uv run python -m scripts.record_golden_map --fixture nvda_ising_2026_04_14
    # (run from apps/alphalens-research; needs ~/.alphalens/{thematic_events,
    #  thematic_news,thematic_tenk,form4_parquet} populated for the window)

LLM-only re-baseline — re-records ONLY the Pro LLM cassette, serving the other
five surfaces from the already-frozen fixtures::

    OPENROUTER_API_KEY=... uv run python -m scripts.record_golden_map \
        --fixture quantum_2026_05_24 --llm-only

Use ``--llm-only`` whenever the mapper prompt / model / sampling config changed
and nothing else did. A full capture would move the vendor payloads, the
market-cap snapshot, the 10-K text and the event window at the same time as the
prompt, so any change in the projection would be unattributable. Both modes
write into the fixture's CURRENT recording directory; both refuse to touch a
version that already holds a cassette, so a re-baseline must bump the
descriptor's ``current_recording`` and leave the old recording in place.
"""

from __future__ import annotations

import argparse
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
from alphalens_pipeline.thematic.mapping import catalyst_resolver, orchestrator
from alphalens_pipeline.thematic.sources.form4_store import classification_years
from alphalens_pipeline.thematic.verification import mcap_filter, recent_press, tenk_grep
from alphalens_pipeline.thematic.verification.tenk_grep import _find_cached
from tests.golden.map_fixtures import MAP_FIXTURES, MapFixture, fixture_by_name, frozen_surfaces
from tests.golden.projection import map_themes_projection
from tests.golden.replay_client import RecordingOpenRouter
from tests.golden.vendor_cassette import RecordingVendor

_ALPHALENS = Path.home() / ".alphalens"


def _freeze_window(fixture: MapFixture, area: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for date in fixture.window_dates:
        src = _ALPHALENS / area / f"{date}.parquet"
        if not src.exists():
            raise SystemExit(
                f"{src} is missing — the fixture declares it in window_dates, so a "
                "capture without it would silently freeze a narrower window than "
                "the one the fixture claims"
            )
        shutil.copyfile(src, dest / f"{date}.parquet")


def _build_form4_fixture(fixture: MapFixture, tickers: set[str]) -> None:
    """Trim the 37MB hive Form-4 corpus to the candidate tickers' insiders.

    Two-pass per the Cohen-Malloy contract: (1) find the CIKs that traded any
    candidate ticker in the classification years; (2) keep EVERY row for those
    CIKs (cross-ticker history) so the classifier sees the full pattern — the
    same view ``has_opportunistic_buy`` loads. Writes the same hive layout.
    """
    root = _ALPHALENS / "form4_parquet"
    out_root = fixture.form4_root
    upper = {t.upper() for t in tickers}
    ciks: set = set()
    per_year: dict[int, pd.DataFrame] = {}
    for year in sorted(classification_years(fixture.asof)):
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


def _freeze_tenk_cache(fixture: MapFixture, tickers: set[str]) -> None:
    """Copy the 10-K text cache file of every ticker the tenk gate consulted.

    Freezes the gate's INPUT SET, not just the kept rows: every in-bracket
    candidate reaches the gate, and the replay redirects the gate at this
    frozen cache. A ticker missing from it would send the replay off to CIK
    resolution and a live SEC fetch — the one thing a hermetic golden must not
    do. Uses the real ``_find_cached`` selector so each frozen file is exactly
    the one ``fetch_10k_text`` picks at the fixture's asof.
    """
    dest = fixture.tenk_cache_dir
    dest.mkdir(parents=True, exist_ok=True)
    real_cache = _ALPHALENS / "thematic_tenk"
    frozen, missing = [], []
    for ticker in sorted(tickers):
        selected = _find_cached(ticker, real_cache, asof=fixture.asof)
        if selected is None:
            missing.append(ticker)
            continue
        shutil.copyfile(selected, dest / selected.name)
        frozen.append(selected.name)
    print(f"  tenk fixture: {len(frozen)} 10-K text files {frozen}")
    if missing:
        print(
            f"  WARNING: no cached 10-K at asof for {missing} — the replay would "
            "attempt a live SEC fetch for those tickers"
        )


def _trim_polygon_cassettes(fixture: MapFixture, tickers: set[str]) -> None:
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
    for path in fixture.vendor_cassette_dir.glob("*.json"):
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


def _write_golden(fixture: MapFixture, df: pd.DataFrame, out_dir: Path) -> None:
    """Publish one capture: candidates parquet + projection into the golden dir.

    ``map_themes`` runs against a throwaway ``out_dir`` rather than straight
    into the fixture tree, because it FREEZES per date: an existing
    ``<asof>.parquet`` whose ``mapper_config_version`` matches is loaded back
    instead of recomputed. Writing the recorder's output where the recorder
    later reads it would silently serve the previous capture and never fire the
    live call this script exists to make.
    """
    dest = fixture.golden_dir()
    dest.mkdir(parents=True, exist_ok=True)
    parquet = out_dir / f"{fixture.asof.isoformat()}.parquet"
    if parquet.exists():
        shutil.copyfile(parquet, dest / parquet.name)
    (dest / "projection.json").write_text(
        json.dumps(map_themes_projection(df), indent=2, sort_keys=True)
    )


def _guard_recording_dir(fixture: MapFixture) -> Path:
    """Refuse to overwrite an existing recording; return the empty target dir."""
    llm_dir = fixture.llm_cassette_dir()
    if llm_dir.exists() and any(llm_dir.glob("*.json")):
        raise SystemExit(
            f"{llm_dir} already holds a recording — bump {fixture.name}'s "
            "current_recording in tests/golden/map_fixtures.py and re-run. "
            "Overwriting destroys the historical comparison a characterization "
            "golden exists for."
        )
    llm_dir.mkdir(parents=True, exist_ok=True)
    return llm_dir


def record_llm_only(fixture: MapFixture) -> None:
    """Re-record ONLY the Pro LLM cassette, holding every other input constant.

    Serves the five non-LLM surfaces from the already-frozen fixtures via
    :func:`frozen_surfaces`, so exactly one variable moves: the request the
    mapper sends. Makes ONE live LLM call.

    Note what holding the surfaces constant implies for the result. The frozen
    market-cap map bounds the reachable ticker universe — a newly proposed
    ticker that is not in ``mcap.json`` gets ``None`` and is dropped by the
    bracket filter — and the Polygon cassette was trimmed to the previous
    capture's tickers. The re-baseline therefore characterizes the new prompt
    ON THE OLD FIXTURE, which is the point: attribution. Widening the universe
    is a separate, deliberate full re-capture.
    """
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit("OPENROUTER_API_KEY must be set for the live LLM capture")
    llm_dir = _guard_recording_dir(fixture)
    rec_pro = RecordingOpenRouter(
        OpenRouterClient(api_key=os.environ["OPENROUTER_API_KEY"]), llm_dir
    )
    with tempfile.TemporaryDirectory(prefix="map_llm_record_") as out_str:
        out_dir = Path(out_str)
        with frozen_surfaces(fixture, pro_client=rec_pro):
            df = orchestrator.map_themes(
                themes=[fixture.theme],
                asof=fixture.asof,
                api_key=os.environ["OPENROUTER_API_KEY"],
                polygon_api_key="frozen",  # forces the patched PolygonClient branch
                output_dir=out_dir,
                market_cap_range=orchestrator.DEFAULT_MCAP_RANGE,
            )
        _write_golden(fixture, df, out_dir)
    print(
        f"re-recorded LLM cassette {fixture.name}/{fixture.current_recording}: "
        f"{len(df)} mapped rows; tickers={sorted(df['ticker']) if len(df) else []}; "
        f"{len(list(llm_dir.glob('*.json')))} cassette(s) -> {llm_dir}"
    )


def record_full(fixture: MapFixture) -> None:
    """Re-record ALL SIX surfaces from the live vendors and ``~/.alphalens``."""
    # Only the two cassette-backed vendors need a key. The 10-K surface is the
    # on-disk text cache, and the canonical SEC client falls back to
    # ``ALPHALENS_DEFAULT_USER_AGENT`` if a cache miss does force a live fetch,
    # so ``SEC_EDGAR_USER_AGENT`` is not a precondition for a capture.
    for env in ("OPENROUTER_API_KEY", "POLYGON_API_KEY"):
        if not os.environ.get(env):
            raise SystemExit(f"{env} must be set for the live capture")

    llm_dir = _guard_recording_dir(fixture)
    vendor_dir = fixture.vendor_cassette_dir
    vendor_dir.mkdir(parents=True, exist_ok=True)

    _freeze_window(fixture, "thematic_events", fixture.events_dir)
    _freeze_window(fixture, "thematic_news", fixture.news_dir)

    # Capture the genuine functions BEFORE patching so the partials wrap the
    # real logic (dir / cache_dir redirected), not the patched stand-in.
    real_find = catalyst_resolver.find_trigger_event
    real_fwu = recent_press.fetch_window_universe
    real_htirp = recent_press.has_theme_in_recent_press
    real_mcap = mcap_filter.fetch_mcap
    real_tenk = tenk_grep.has_theme_keywords_in_10k

    mcap_capture: dict[str, float | None] = {}
    tenk_tickers: set[str] = set()

    def _teed_mcap(ticker: str, *, asof: dt.date | None = None):
        value = real_mcap(ticker, asof=asof)
        mcap_capture[ticker.upper()] = value
        return value

    def _teed_tenk(*, ticker: str, **kwargs):
        # Records the gate's INPUT SET so the 10-K freeze covers every ticker
        # the replay will ask for, not only the ones that survived to a row.
        tenk_tickers.add(ticker.upper())
        return real_tenk(ticker=ticker, **kwargs)

    rec_pro = RecordingOpenRouter(
        OpenRouterClient(api_key=os.environ["OPENROUTER_API_KEY"]), llm_dir
    )
    rec_poly = RecordingVendor(PolygonClient(os.environ["POLYGON_API_KEY"]), vendor_dir)

    # Fresh empty press cache so the Polygon firehose actually fires (gets
    # recorded); TemporaryDirectory cleans it on exit (no /tmp leak).
    with (
        tempfile.TemporaryDirectory(prefix="press_record_") as press_tmp_str,
        tempfile.TemporaryDirectory(prefix="map_full_record_") as out_str,
    ):
        press_tmp = Path(press_tmp_str)
        out_dir = Path(out_str)
        with (
            mock.patch.object(orchestrator, "_init_pro_client", lambda api_key: rec_pro),
            mock.patch.object(orchestrator, "PolygonClient", lambda *a, **k: rec_poly),
            mock.patch.object(orchestrator, "get_default_polygon_client", lambda: rec_poly),
            mock.patch.object(
                catalyst_resolver,
                "find_trigger_event",
                functools.partial(
                    real_find, events_dir=fixture.events_dir, news_dir=fixture.news_dir
                ),
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
            mock.patch.object(tenk_grep, "has_theme_keywords_in_10k", _teed_tenk),
            mock.patch.object(mcap_filter, "fetch_mcap", _teed_mcap),
        ):
            df = orchestrator.map_themes(
                themes=[fixture.theme],
                asof=fixture.asof,
                api_key=os.environ["OPENROUTER_API_KEY"],
                polygon_api_key="dummy",  # forces the `if polygon_api_key:` branch
                output_dir=out_dir,
                market_cap_range=orchestrator.DEFAULT_MCAP_RANGE,
            )
        _write_golden(fixture, df, out_dir)

    # Form-4 fixture: trim to every ticker an mcap lookup touched (= every
    # proposed candidate that survived to the verify stage and beyond).
    _build_form4_fixture(fixture, set(mcap_capture.keys()))

    # tenk fixture: the gate reads its 10-K text from the on-disk cache
    # (~/.alphalens/thematic_tenk). Freeze the selected text for every ticker
    # the gate consulted so the replay greps frozen text, offline.
    _freeze_tenk_cache(fixture, tenk_tickers)

    # Shrink the 30-day Polygon firehose cassette (~15MB) to candidate rows.
    _trim_polygon_cassettes(fixture, set(df["ticker"]) if len(df) else set())

    fixture.mcap_path.write_text(
        json.dumps(dict(sorted(mcap_capture.items())), indent=2, sort_keys=True)
    )
    n_llm = len(list(llm_dir.glob("*.json")))
    n_vendor = len(list(vendor_dir.glob("*.json")))
    print(
        f"captured {len(df)} mapped rows; tickers={sorted(df['ticker']) if len(df) else []}; "
        f"{n_llm} LLM + {n_vendor} vendor cassettes; mcap for {len(mcap_capture)} tickers "
        f"-> {fixture.root}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        required=True,
        choices=[f.name for f in MAP_FIXTURES],
        help="which recorded map-themes case to capture",
    )
    parser.add_argument(
        "--llm-only",
        action="store_true",
        help=(
            "re-record ONLY the Pro LLM cassette against the already-frozen "
            "vendor / event / 10-K / Form-4 / market-cap fixtures (one live call)"
        ),
    )
    args = parser.parse_args()
    fixture = fixture_by_name(args.fixture)
    if args.llm_only:
        record_llm_only(fixture)
    else:
        record_full(fixture)


if __name__ == "__main__":
    main()
