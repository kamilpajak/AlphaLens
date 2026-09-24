"""CLI: ``alphalens cache`` subcommands for server-side data caches.

v2 PR-2 ships ``refresh-vix`` only — pulls VIXCLS from FRED and writes the
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

logger = logging.getLogger(__name__)

cache_app = typer.Typer(
    name="cache",
    help="Server-side data cache refresh tools (VIX regime, ...).",
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
