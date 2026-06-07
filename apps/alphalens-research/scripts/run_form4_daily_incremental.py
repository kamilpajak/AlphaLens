"""Daily Form-4 incremental ingest — VPS systemd-user oneshot.

Keeps the hive-partitioned Form-4 parquet store at ``~/.alphalens/form4_parquet/``
fresh after the one-time historical bulk backfill (the seed) froze. Each run
fetches a fixed lookback window ``[asof - lookback_days, asof]`` (UTC) via the SEC
daily form index; overlap between consecutive daily runs collapses on the unique
``accession_number`` under the compactor's full-row drop_duplicates, so there is
NO state file to corrupt.

Steady-state cadence is ``--lookback-days 3`` (covers the SEC 2-business-day
filing deadline + a weekend). The FIRST deploy needs ONE manual catch-up run with
a larger window (``--lookback-days 35``) to fill the seed -> today gap; the daily
timer then runs steady-state from the next day. See ``deploy/systemd/README.md``.

Exits 0 on a clean run OR on transient SEC degradation (403 starvation) so the
``last_success`` staleness clock refreshes nightly and the staleness rule cleanly
catches "the job stopped running" rather than firing on a single rate-limited
night. The next overlapping run re-covers any date skipped on a transient error.

Deployment: ``deploy/systemd/alphalens-form4-incremental.{service,timer}``. All
SEC HTTP goes through the canonical :class:`SecEdgarClient` (User-Agent + 10 req/s
throttle + 429/5xx retry + cross-process rate coordinator).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from alphalens_pipeline.data.alt_data.form4_incremental import (
    fetch_form4_records_for_window,
    latest_filed_date_in_store,
    load_cik_universe,
    today_utc,
)
from alphalens_pipeline.data.alt_data.sec_edgar_client import (
    SecEdgarClient,
    get_default_sec_client,
)
from alphalens_pipeline.observability.textfile import emit_domain_metrics

logger = logging.getLogger(__name__)

_DEFAULT_LOOKBACK_DAYS = 3
_DEFAULT_OVERLAP_DAYS = 2
_DEFAULT_MAX_CATCHUP_DAYS = 400
_DEFAULT_PARQUET_ROOT = Path.home() / ".alphalens" / "form4_parquet"
_DEFAULT_CIK_UNIVERSE = Path.home() / ".alphalens" / "form4_cik_universe.txt"
_JOB_NAME = "form4-incremental"


def _resolve_window_start(
    *,
    asof_date: date,
    lookback_days: int,
    overlap_days: int,
    max_catchup_days: int,
    latest_in_store: date | None,
) -> date:
    """Window start = the earlier of the steady-state lookback and a reach-back
    to the store's newest filing (minus overlap), capped at ``max_catchup_days``.

    Steady state (store fresh): the lookback term dominates. After a late first
    deploy or a missed run (store stale): the reach-back term dominates so the
    gap is closed. The cap bounds an empty/misread store so the window can never
    run away.
    """
    default_start = asof_date - timedelta(days=lookback_days - 1)
    floor_start = asof_date - timedelta(days=max_catchup_days)
    if latest_in_store is None:
        start = default_start
    else:
        reach_back = latest_in_store - timedelta(days=overlap_days)
        start = min(default_start, reach_back)
    return max(start, floor_start)


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument(
        "--lookback-days",
        type=int,
        default=_DEFAULT_LOOKBACK_DAYS,
        help=(
            "Minimum inclusive lookback window in days (default 3). The window "
            "auto-extends further back when the store's newest filing is older "
            "than this (late first deploy or a missed run self-heals); no manual "
            "catch-up sizing needed."
        ),
    )
    p.add_argument(
        "--overlap-days",
        type=int,
        default=_DEFAULT_OVERLAP_DAYS,
        help=(
            "When auto-extending to the store's newest filing, start this many "
            "days before it so a same-day partial filing set is re-pulled "
            "(dedup-safe). Default 2."
        ),
    )
    p.add_argument(
        "--max-catchup-days",
        type=int,
        default=_DEFAULT_MAX_CATCHUP_DAYS,
        help=(
            "Hard cap on how far back the auto-extended window may reach "
            "(default 400). Bounds the first run / a misread store so it can "
            "never run away fetching years of daily indexes."
        ),
    )
    p.add_argument(
        "--asof-date",
        type=_parse_date,
        default=today_utc(),
        help="End of the window (UTC), YYYY-MM-DD. Defaults to today (UTC).",
    )
    p.add_argument(
        "--parquet-root",
        type=Path,
        default=_DEFAULT_PARQUET_ROOT,
        help="Root of the hive-partitioned Form-4 parquet store.",
    )
    p.add_argument(
        "--user-agent",
        default=None,
        help=(
            "SEC User-Agent (email or URL contact). Omit to use the canonical "
            "default client (reads SEC_EDGAR_USER_AGENT from the environment)."
        ),
    )
    p.add_argument(
        "--cik-universe",
        type=Path,
        default=_DEFAULT_CIK_UNIVERSE,
        help=(
            "CIK-per-line universe file. The daily index is filtered to these "
            "issuers before any .txt fetch (matches the seed's scope; ~10x fewer "
            "requests). Fails loud if missing — use --market-wide to opt out."
        ),
    )
    p.add_argument(
        "--market-wide",
        action="store_true",
        help="Ingest every Form-4 in the daily index (no universe filter). Slow.",
    )
    return p.parse_args(argv)


def _build_client(user_agent: str | None) -> SecEdgarClient:
    if user_agent:
        return SecEdgarClient(user_agent=user_agent)
    return get_default_sec_client()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = parse_args(argv)

    # Universe-scope by default (fail loud if the file is missing rather than
    # silently degrading to the slow market-wide path); --market-wide opts out.
    try:
        cik_universe = None if args.market_wide else load_cik_universe(args.cik_universe)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("form4-incremental: cannot load CIK universe: %s", exc)
        return 1

    latest_in_store = latest_filed_date_in_store(args.parquet_root)
    start_date = _resolve_window_start(
        asof_date=args.asof_date,
        lookback_days=args.lookback_days,
        overlap_days=args.overlap_days,
        max_catchup_days=args.max_catchup_days,
        latest_in_store=latest_in_store,
    )
    logger.info(
        "form4-incremental: window=[%s, %s] (%dd) latest_in_store=%s root=%s",
        start_date,
        args.asof_date,
        (args.asof_date - start_date).days + 1,
        latest_in_store,
        args.parquet_root,
    )

    client = _build_client(args.user_agent)
    result = fetch_form4_records_for_window(
        client,
        start_date=start_date,
        end_date=args.asof_date,
        parquet_root=args.parquet_root,
        cik_universe=cik_universe,
    )

    logger.info(
        "form4-incremental complete: rows_written=%d distinct_accessions=%d "
        "latest_filing_date=%s transient_errors=%d other_errors=%d",
        result.rows_written,
        result.distinct_accessions,
        result.latest_filing_date,
        result.transient_errors,
        result.other_errors,
    )

    # Prometheus textfile gauges for the cron-observability dashboard. Wrap in
    # try/except: the parquet write already succeeded, so a metrics-dir failure
    # must not turn this run into a unit failure (zen pre-merge rule, PR #311).
    # latest_filing_date is emitted as a Unix timestamp (seconds) so the gauge
    # can be compared against time() to surface a coverage hole (the gauge sits
    # ~lookback days behind today on a healthy run; ~30 days behind if the
    # first-run catch-up was skipped).
    try:
        metrics: dict[str, float | int] = {
            "alphalens_form4_rows_written": result.rows_written,
            "alphalens_form4_distinct_accessions": result.distinct_accessions,
            "alphalens_form4_transient_errors": result.transient_errors,
        }
        if result.latest_filing_date is not None:
            from datetime import datetime

            ts = datetime(
                result.latest_filing_date.year,
                result.latest_filing_date.month,
                result.latest_filing_date.day,
                tzinfo=UTC,
            ).timestamp()
            metrics["alphalens_form4_latest_filing_date"] = int(ts)
        emit_domain_metrics(job=_JOB_NAME, metrics=metrics)
    except Exception:
        logger.exception("emit_domain_metrics failed; form4-incremental run succeeded")

    return 0


if __name__ == "__main__":
    sys.exit(main())
