"""Versioned fixture layout + frozen-surface harness for the map-themes golden.

This is the ONE place that defines (a) where the map-day fixtures live, (b)
which recording is CURRENT, and (c) how the five NON-LLM external surfaces are
frozen. Both consumers import from here so they cannot drift:

* ``tests/golden/test_golden_map_replay.py`` — replays all six surfaces offline.
* ``scripts/record_golden_map.py --llm-only`` — re-records ONLY the Pro LLM
  cassette, serving those same five surfaces from these same frozen fixtures so
  the prompt is the single variable that moved.

RECORDING VERSIONS
------------------
The LLM cassette and the golden projection are versioned side by side under
``cassettes_llm/<version>/`` and ``golden/<version>/``. Everything else — the
Polygon vendor cassette, the events/news window, the 10-K text cache, the
Form-4 parquet slice and the market-cap map — is SHARED across versions and
stays byte-frozen.

A re-baseline BUMPS :data:`CURRENT_RECORDING` and adds a new directory. It
never edits or deletes an older one: a characterization golden is only worth
keeping if the new execution can be diffed against the approved one, and
overwriting destroys exactly that comparison. The current version is read
explicitly from :data:`CURRENT_RECORDING` — the fixture tree is never globbed
for "whatever version happens to be on disk".

===========  ===========  =====================================================
version      recorded     mapper request
===========  ===========  =====================================================
v1           2026-06-01   bare theme slug (``mapper-freeze-v1``)
v2           2026-08-03   resolved catalyst event (``mapper-freeze-v2``)
===========  ===========  =====================================================

See ``docs/research/golden_map_rebaseline_2026_08_03.md`` for the v1→v2
request/projection diff and the provenance record.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import functools
import json
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.thematic.mapping import catalyst_resolver, orchestrator
from alphalens_pipeline.thematic.verification import insider, mcap_filter, recent_press, tenk_grep

from tests.golden.vendor_cassette import VendorCassette

THEME = "quantum_computing"
ASOF = dt.date(2026, 5, 24)
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "map_day"

CURRENT_RECORDING = "v2"

# Genuine module functions captured at import, BEFORE any patch — the partials
# below wrap the REAL logic with a redirected dir/cache, not a patched stand-in.
_REAL_FIND = catalyst_resolver.find_trigger_event
_REAL_FWU = recent_press.fetch_window_universe
_REAL_HTIRP = recent_press.has_theme_in_recent_press
_REAL_TENK = tenk_grep.has_theme_keywords_in_10k
_REAL_INSIDER = insider.has_opportunistic_buy


def llm_cassette_dir(version: str = CURRENT_RECORDING) -> Path:
    """Directory holding the Pro LLM cassette for one recording version."""
    return FIXTURES / "cassettes_llm" / version


def golden_dir(version: str = CURRENT_RECORDING) -> Path:
    """Directory holding the golden projection + candidates parquet for a version."""
    return FIXTURES / "golden" / version


def golden_projection_path(version: str = CURRENT_RECORDING) -> Path:
    return golden_dir(version) / "projection.json"


@contextlib.contextmanager
def frozen_surfaces(*, pro_client) -> Iterator[None]:
    """Serve the five NON-LLM map-themes surfaces from the frozen fixtures.

    ``pro_client`` is the only live-capable seam: pass ``ReplayOpenRouter`` to
    replay a cassette, ``RecordingOpenRouter`` to record a new one. Everything
    else inside the block is offline and deterministic:

    * Polygon press → ``VendorCassette`` over the recorded ``get_news_range``
      payload, injected by patching ``orchestrator.PolygonClient``
    * SEC 10-K → frozen on-disk text cache read by the real grep gate (the
      cache hit precedes CIK resolution, so no SEC client call fires)
    * yfinance mcap → frozen ``{ticker: mcap}`` map (no client to cassette).
      Note this also BOUNDS the reachable ticker universe: a ticker absent from
      the map gets ``None`` and is dropped by the bracket filter.
    * Form-4 → trimmed hive parquet, real Cohen-Malloy classifier runs over it
    * catalyst → frozen events/news window, real resolver runs over it

    The press cache is a fresh ``TemporaryDirectory`` so the Polygon call
    actually fires (and is served by the cassette) and no write lands in
    ``~/.alphalens``.
    """
    vendor = VendorCassette(FIXTURES / "cassettes_vendor")
    mcap_map = {k.upper(): v for k, v in json.loads((FIXTURES / "mcap.json").read_text()).items()}
    with TemporaryDirectory(prefix="press_frozen_") as press_tmp_str:
        press_tmp = Path(press_tmp_str)
        with (
            mock.patch.object(orchestrator, "_init_pro_client", lambda api_key: pro_client),
            mock.patch.object(orchestrator, "PolygonClient", lambda *a, **k: vendor),
            mock.patch.object(orchestrator, "get_default_polygon_client", lambda: vendor),
            mock.patch.object(
                catalyst_resolver,
                "find_trigger_event",
                functools.partial(
                    _REAL_FIND, events_dir=FIXTURES / "events", news_dir=FIXTURES / "news"
                ),
            ),
            mock.patch.object(
                recent_press,
                "fetch_window_universe",
                functools.partial(_REAL_FWU, cache_dir=press_tmp),
            ),
            mock.patch.object(
                recent_press,
                "has_theme_in_recent_press",
                functools.partial(_REAL_HTIRP, cache_dir=press_tmp),
            ),
            mock.patch.object(
                tenk_grep,
                "has_theme_keywords_in_10k",
                functools.partial(_REAL_TENK, cache_dir=FIXTURES / "tenk_cache"),
            ),
            mock.patch.object(
                insider,
                "has_opportunistic_buy",
                functools.partial(_REAL_INSIDER, form4_root=FIXTURES / "form4_parquet"),
            ),
            mock.patch.object(
                mcap_filter, "fetch_mcap", lambda ticker, *, asof=None: mcap_map.get(ticker.upper())
            ),
        ):
            yield


__all__ = [
    "ASOF",
    "CURRENT_RECORDING",
    "FIXTURES",
    "THEME",
    "frozen_surfaces",
    "golden_dir",
    "golden_projection_path",
    "llm_cassette_dir",
]
