"""CLI: ``alphalens cache`` subcommands for server-side data caches.

Two commands today, and they do opposite things. ``refresh-vix`` WRITES a
cache; ``companyfacts-census`` only READS one and reports how much of it is
still usable, as Prometheus gauges plus a line of text. Both run as
best-effort steps inside the daily thematic build, so neither may fail a build
whose brief is already written.

``refresh-vix`` pulls VIXCLS from FRED and writes the
tiny JSON VIX regime cache that the Django feedback POST path reads on the
hot path (see ``alphalens_feedback.regime.get_cached_vix``). Wired
into the daily thematic build (``deploy/docker/run_thematic_day.sh``) so a
fresh VIX value lands ~6x/day; the reader degrades to "unknown" if this
refresh dies and the cache ages past 96h.

Lazy imports inside the command body keep the ``alphalens`` CLI startup cost
low — the FRED client (pandas + requests) is only imported when this command
actually runs, never on the Layer-1 ``edgar-detect`` cron hot path.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from alphalens_feedback.regime import default_vix_cache_path
from alphalens_pipeline.observability.textfile import emit_domain_metrics

if TYPE_CHECKING:
    import pandas as pd
    from alphalens_pipeline.data.store.companyfacts_census import StoreCensus

logger = logging.getLogger(__name__)

cache_app = typer.Typer(
    name="cache",
    help="Server-side data cache tools: refresh a cache, or report on one.",
    no_args_is_help=True,
)

_VIX_SERIES = "VIXCLS"
# Freshness gauge consumed by the AlphalensVixCache{Stale,MetricMissing}
# Prometheus rules. Domain job name (not a cron systemd unit — the refresh
# runs inline in run_thematic_day.sh), so it has no ExecStopPost emit hook
# and must stay out of the cron job enumerations.
_VIX_METRIC = "alphalens_vix_cache_fetched_at_timestamp_seconds"
#: Age of the DATA, not of the request (#1524). The gauge above says when we last
#: ASKED FRED; it stayed green through three months of a VIXCLS parquet frozen at
#: a 2026-07-01 print, because the refresher kept running and kept fetching into a
#: throwaway directory. Only the observation date can catch a publisher — or a
#: cache — that has stopped moving. Consumed by
#: AlphalensFredSeries{Stale,ObservationMetricMissing}.
_OBSERVATION_METRIC = "alphalens_fred_series_last_observation_timestamp_seconds"
_VIX_JOB = "vix-cache-refresh"

#: Store-health gauges for the companyfacts parquet cache (#1335). Domain job
#: name for the same reason as ``_VIX_JOB``: the census runs inline in
#: run_thematic_day.sh, not as a cron systemd unit, so it has no ExecStopPost
#: emit hook and must stay out of the cron job enumerations. The gauge NAMES
#: live beside the census in
#: ``alphalens_pipeline.data.store.companyfacts_census.census_metrics``, so a
#: band cannot be added to the measurement without its series coming with it.
_CENSUS_JOB = "companyfacts-store"


def _fetch_vixcls() -> pd.Series:
    """Pull VIXCLS through the canonical client, into the SHARED cache.

    This used to fetch into a throwaway ``TemporaryDirectory`` because the client
    had no cache expiry and "returned an existing parquet forever" — the defect
    fixed in #1524. With ``through`` honoured, the workaround is not merely
    unnecessary, it is harmful: refreshing the shared parquet is what lets one
    fetch per run serve this JSON cache AND ``market_state``, which was reading
    the same file and getting a 2026-07-01 print for three months.

    ``through`` is today's CALENDAR date rather than a session, because this
    command's job is "get me the newest print there is". The client converts it
    with session arithmetic, so a weekend or holiday invocation is not treated as
    missing data. It therefore refetches on every invocation by design — the
    command runs once per build, FRED allows 120 req/min, and the fetch is what
    leaves the shared parquet current for `market_state` a few lines later.
    """
    from alphalens_pipeline.data.macro.fred_client import FREDClient

    return FREDClient.from_env().fetch_series(_VIX_SERIES, through=dt.date.today())


def refresh_vix_cache(
    cache_path: str | Path | None = None,
    *,
    fred_fetch: Callable[[], pd.Series] | None = None,
    now: dt.datetime | None = None,
) -> dict:
    """Fetch the latest VIXCLS close and write the JSON regime cache atomically.

    Takes the last non-null observation (FRED sentinel "." rows are already
    dropped by FREDClient, but a trailing NaN is guarded here too) and writes
    ``{observation_date, vix, fetched_at, series}`` via tmp-file + os.replace
    so a concurrent reader never sees a half-written file. Returns the written
    payload. ``fred_fetch`` is injectable for tests (no network).
    """
    fetch = fred_fetch or _fetch_vixcls
    now = now or dt.datetime.now(dt.UTC)
    path = Path(cache_path) if cache_path is not None else default_vix_cache_path()

    # ``dropna()`` removes FRED's missing-observation rows; the empty-check
    # then guarantees the series has at least one real value, so the last
    # element is a non-NaN float by construction (no extra NaN guard needed).
    series = fetch().dropna()
    if series.empty:
        raise ValueError(f"FRED returned no usable {_VIX_SERIES} observations")
    observation_date = series.index[-1].date().isoformat()
    vix = float(series.iloc[-1])

    payload = {
        "observation_date": observation_date,
        "vix": vix,
        "fetched_at": now.isoformat(),
        "series": _VIX_SERIES,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise

    # Emit a freshness gauge AFTER the cache is durably written. A dead
    # refresher silently ages the cache past the 96h reader ceiling, degrading
    # every new decision's ``market_regime_at_entry`` to "unknown" with no
    # other signal — this gauge is what the staleness alert watches. The write
    # above is the real work; any emit failure is pure observability debt and
    # must not raise, so the best-effort ``|| echo WARN`` step in
    # run_thematic_day.sh keeps the fresh VIX value. Catch broad ``Exception``
    # (not just OSError) to match the other emit callsites — a malformed
    # metrics dict must never abort a successful refresh either.
    try:
        emit_domain_metrics(
            job=_VIX_JOB,
            metrics={
                f'{_VIX_METRIC}{{series="{_VIX_SERIES}"}}': int(now.timestamp()),
                f'{_OBSERVATION_METRIC}{{series="{_VIX_SERIES}"}}': int(
                    dt.datetime.fromisoformat(observation_date).replace(tzinfo=dt.UTC).timestamp()
                ),
            },
        )
    except Exception:
        logger.exception("emit_domain_metrics failed; vix-cache-refresh run succeeded")
    return payload


def companyfacts_census(store_dir: Path | None = None) -> StoreCensus:
    """Census the companyfacts parquet store and emit its health gauges.

    The gauges are the product here, not a side effect: nothing else watches
    the store ageing out of the TTM staleness gate, which is the half of #1335
    that PR #1636 deferred. Returned as well as emitted so the command can
    print it and a caller can assert on it.

    One ``emit_domain_metrics`` call for all nine series. The emitter rewrites
    the whole ``alphalens_domain_<job>.prom`` per call, so a second call under
    this job would clobber the first one's series.

    Any emit failure is swallowed, broad ``Exception`` like the other call
    sites: this runs as a best-effort step after the brief is written, and a
    malformed metrics dict must not fail a build that has already published.
    """
    from alphalens_pipeline.data.store.companyfacts_census import (
        census_companyfacts_store,
        census_metrics,
    )
    from alphalens_pipeline.data.store.edgar_fundamentals import DEFAULT_PARQUET_DIR

    census = census_companyfacts_store(store_dir or DEFAULT_PARQUET_DIR)
    try:
        emit_domain_metrics(job=_CENSUS_JOB, metrics=census_metrics(census))
    except Exception:
        logger.exception("emit_domain_metrics failed; companyfacts-census run succeeded")
    return census


@cache_app.command(name="companyfacts-census")
def companyfacts_census_command(
    store_dir: Path = typer.Option(
        None,
        "--store-dir",
        help="Override the companyfacts store (default: ~/.alphalens/companyfacts_parquet).",
    ),
    output_format: str = typer.Option(
        "human",
        "--format",
        help="human|json",
    ),
) -> None:
    """Report how much of the companyfacts store the FCFF path can still read.

    A MEASUREMENT, not a check: it exits 0 whenever the census completed,
    however bad the reading, the same contract as the systemd drift check. The
    numbers reach Prometheus as gauges; stdout is for a human or a script
    asking the same question by hand.
    """
    if output_format not in ("human", "json"):
        typer.echo(f"--format must be human or json, got {output_format!r}", err=True)
        raise typer.Exit(code=2)

    from alphalens_pipeline.data.store.companyfacts_census import CENSUS_SCHEMA

    census = companyfacts_census(store_dir)
    if output_format == "json":
        typer.echo(
            json.dumps(
                {
                    "schema": CENSUS_SCHEMA,
                    "asof": census.asof.isoformat(),
                    "total_files": census.total_files,
                    "readable": census.readable,
                    "empty": census.empty,
                    "unreadable": census.unreadable,
                    "fresh": census.fresh,
                    "refetching": census.refetching,
                    "past_gate": census.past_gate,
                    "chain_absent": census.chain_absent,
                    "crossing_gate_within_horizon": census.crossing_gate_within_horizon,
                    "refetched_recently": census.refetched_recently,
                },
                separators=(",", ":"),
            )
        )
        return

    typer.echo(
        f"companyfacts-census {census.asof}: {census.total_files} files "
        f"({census.readable} readable, {census.empty} empty, {census.unreadable} unreadable)"
    )
    typer.echo(
        f"  fcff age: {census.fresh} fresh, {census.refetching} refetching, "
        f"{census.past_gate} past gate, {census.chain_absent} no chain"
    )
    typer.echo(
        f"  {census.crossing_gate_within_horizon} crossing the gate soon, "
        f"{census.refetched_recently} written recently"
    )


@cache_app.command(name="refresh-vix")
def refresh_vix_command(
    cache_path: Path = typer.Option(
        None,
        "--cache-path",
        help="Override the VIX cache JSON location (default: ~/.alphalens/macro/vix_regime_cache.json).",
    ),
) -> None:
    """Pull the latest VIXCLS close from FRED and refresh the VIX regime cache.

    Best-effort by design: run from the daily thematic build so the Django
    feedback POST path can stamp a real low/mid/high market regime instead of
    "unknown". Requires FRED_API_KEY in the environment.
    """
    payload = refresh_vix_cache(cache_path)
    typer.echo(
        f"refresh-vix: {payload['series']} = {payload['vix']} "
        f"(observation {payload['observation_date']}) -> "
        f"{cache_path or default_vix_cache_path()}"
    )
