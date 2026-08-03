"""Fixture SET + frozen-surface harness for the map-themes golden replay.

This is the ONE place that defines (a) which recorded map-themes cases exist,
(b) where each one's fixtures live, (c) which recording of each is CURRENT, and
(d) how the five NON-LLM external surfaces are frozen. Both consumers import
from here so they cannot drift:

* ``tests/golden/test_golden_map_replay.py`` — replays all six surfaces offline,
  once per fixture in :data:`MAP_FIXTURES`.
* ``scripts/record_golden_map.py`` — records a fixture: all six surfaces
  (``--fixture NAME``) or only the Pro LLM cassette (``--llm-only``), serving
  the other five from these same frozen files so the prompt is the single
  variable that moved.

THE FIXTURE SET
---------------
Each fixture is one recorded ``(theme, asof)`` case with its own self-contained
tree under ``fixtures/<dir>/``. Fixtures are ADDED, never swapped: a second case
answers a different question from the first, and replacing one with the other
would destroy the comparison the first was recorded for.

=========================  ==================  ============  ===================
fixture                    theme               asof          why it exists
=========================  ==================  ============  ===================
``quantum_2026_05_24``     quantum_computing   2026-05-24    the original case;
                                                             re-recorded across
                                                             the prompt change,
                                                             so v1 vs v2 isolates
                                                             that one variable
``nvda_ising_2026_04_14``  quantum_computing   2026-04-14    NVIDIA announces
                                                             open models for
                                                             quantum error
                                                             correction and names
                                                             no small-cap; the
                                                             case the event
                                                             conditioning is
                                                             about
=========================  ==================  ============  ===================

RECORDING VERSIONS
------------------
Within a fixture, the LLM cassette and the golden projection are versioned side
by side under ``cassettes_llm/<version>/`` and ``golden/<version>/``. Everything
else — the Polygon vendor cassette, the events/news window, the 10-K text cache,
the Form-4 parquet slice and the market-cap map — is SHARED across that
fixture's versions and stays byte-frozen.

A re-baseline BUMPS the fixture's ``current_recording`` and adds a new
directory. It never edits or deletes an older one: a characterization golden is
only worth keeping if the new execution can be diffed against the approved one,
and overwriting destroys exactly that comparison. The current version is read
explicitly from the descriptor — the fixture tree is never globbed for
"whatever version happens to be on disk".

=========================  =======  ===========  ==============================
fixture                    version  recorded     mapper request
=========================  =======  ===========  ==============================
``quantum_2026_05_24``     v1       2026-06-01   bare theme slug
                                                 (``mapper-freeze-v1``)
``quantum_2026_05_24``     v2       2026-08-03   resolved catalyst event
                                                 (``mapper-freeze-v2``)
``nvda_ising_2026_04_14``  v1       2026-08-03   resolved catalyst event
                                                 (``mapper-freeze-v2``)
=========================  =======  ===========  ==============================

Provenance: ``docs/research/golden_map_rebaseline_2026_08_03.md`` (the
``quantum_2026_05_24`` v1→v2 diff) and
``docs/research/golden_map_ising_fixture_2026_08_03.md`` (the
``nvda_ising_2026_04_14`` capture).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import functools
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.thematic.mapping import catalyst_resolver, orchestrator
from alphalens_pipeline.thematic.verification import insider, mcap_filter, recent_press, tenk_grep

from tests.golden.vendor_cassette import VendorCassette

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Genuine module functions captured at import, BEFORE any patch — the partials
# below wrap the REAL logic with a redirected dir/cache, not a patched stand-in.
_REAL_FIND = catalyst_resolver.find_trigger_event
_REAL_FWU = recent_press.fetch_window_universe
_REAL_HTIRP = recent_press.has_theme_in_recent_press
_REAL_TENK = tenk_grep.has_theme_keywords_in_10k
_REAL_INSIDER = insider.has_opportunistic_buy


@dataclass(frozen=True)
class MapFixture:
    """One recorded ``(theme, asof)`` map-themes case and where its files live.

    ``window_dates`` are the dates whose ``thematic_events`` / ``thematic_news``
    parquets exist on disk inside the resolver's 30-day lookback from ``asof``.
    They are listed explicitly rather than globbed so a capture freezes a known
    window and a later re-capture cannot silently widen it.
    """

    name: str
    theme: str
    asof: dt.date
    window_dates: tuple[str, ...]
    current_recording: str
    dirname: str

    @property
    def root(self) -> Path:
        return FIXTURES / self.dirname

    @property
    def events_dir(self) -> Path:
        return self.root / "events"

    @property
    def news_dir(self) -> Path:
        return self.root / "news"

    @property
    def tenk_cache_dir(self) -> Path:
        return self.root / "tenk_cache"

    @property
    def form4_root(self) -> Path:
        return self.root / "form4_parquet"

    @property
    def mcap_path(self) -> Path:
        return self.root / "mcap.json"

    @property
    def vendor_cassette_dir(self) -> Path:
        return self.root / "cassettes_vendor"

    def llm_cassette_dir(self, version: str | None = None) -> Path:
        """Directory holding the Pro LLM cassette for one recording version."""
        return self.root / "cassettes_llm" / (version or self.current_recording)

    def golden_dir(self, version: str | None = None) -> Path:
        """Directory holding the golden projection + candidates parquet."""
        return self.root / "golden" / (version or self.current_recording)

    def golden_projection_path(self, version: str | None = None) -> Path:
        return self.golden_dir(version) / "projection.json"


QUANTUM_2026_05_24 = MapFixture(
    name="quantum_2026_05_24",
    theme="quantum_computing",
    asof=dt.date(2026, 5, 24),
    window_dates=("2026-05-15", "2026-05-18", "2026-05-24"),
    current_recording="v2",
    dirname="map_day",
)

NVDA_ISING_2026_04_14 = MapFixture(
    name="nvda_ising_2026_04_14",
    theme="quantum_computing",
    asof=dt.date(2026, 4, 14),
    # The Ising press release is the ONLY theme-tagged event on disk inside the
    # 30-day lookback from this asof, so the resolver has exactly one candidate
    # trigger and the fixture is unambiguous about which event it characterizes.
    window_dates=("2026-04-14",),
    current_recording="v1",
    dirname="map_day_nvda_ising",
)

MAP_FIXTURES: tuple[MapFixture, ...] = (QUANTUM_2026_05_24, NVDA_ISING_2026_04_14)


def fixture_by_name(name: str) -> MapFixture:
    for fixture in MAP_FIXTURES:
        if fixture.name == name:
            return fixture
    known = ", ".join(f.name for f in MAP_FIXTURES)
    raise KeyError(f"unknown map fixture {name!r} — known fixtures: {known}")


@contextlib.contextmanager
def frozen_surfaces(fixture: MapFixture, *, pro_client) -> Iterator[None]:
    """Serve the five NON-LLM map-themes surfaces from ``fixture``'s frozen files.

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
    vendor = VendorCassette(fixture.vendor_cassette_dir)
    mcap_map = {k.upper(): v for k, v in json.loads(fixture.mcap_path.read_text()).items()}
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
                    _REAL_FIND, events_dir=fixture.events_dir, news_dir=fixture.news_dir
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
                functools.partial(_REAL_TENK, cache_dir=fixture.tenk_cache_dir),
            ),
            mock.patch.object(
                insider,
                "has_opportunistic_buy",
                functools.partial(_REAL_INSIDER, form4_root=fixture.form4_root),
            ),
            mock.patch.object(
                mcap_filter, "fetch_mcap", lambda ticker, *, asof=None: mcap_map.get(ticker.upper())
            ),
        ):
            yield


__all__ = [
    "FIXTURES",
    "MAP_FIXTURES",
    "NVDA_ISING_2026_04_14",
    "QUANTUM_2026_05_24",
    "MapFixture",
    "fixture_by_name",
    "frozen_surfaces",
]
