"""Pull iVolatility coverage calendars before subscription expiry.

Two endpoints in the Lab tier we never cached but should before the
trial ends 2026-05-08:

- ``/equities/eod/history-earnings-calendar`` — historical earnings
  dates with estimate vs reported EPS (event-driven research input).
- ``/equities/trading-calendar`` — market open / close / holiday flags
  per region.

Both share the iVolatility async-file-mode protocol when
``recordsFound > 500``: the first GET returns metadata + a
``urlForDetails`` JSON describing a gzipped CSV; we poll the details
URL until ``COMPLETE`` then download the CSV gz.

Usage::

    .venv/bin/python scripts/pull_ivolatility_calendars.py \\
        --out-dir ~/.alphalens/ivolatility_calendar

The output directory will contain:

- ``history_earnings_<group>_<from>_<to>.parquet``
- ``trading_calendar_<region>_<from>_<to>.parquet``

These are intentionally separate files (no merge) so partial fetches
remain useful if a later range fails.
"""

from __future__ import annotations

import argparse
import gzip
import io
import logging
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://restapi.ivolatility.com"
DEFAULT_OUT_DIR = Path.home() / ".alphalens" / "ivolatility_calendar"
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_POLL_TIMEOUT = 600.0  # 10 min — generous for whole-universe fetches


class CalendarFetchError(RuntimeError):
    """Raised when a calendar endpoint returns a non-200 / parse failure."""


def fetch_calendar_endpoint(
    endpoint: str,
    params: dict,
    api_key: str,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT,
) -> pd.DataFrame:
    """Fetch one iVolatility calendar endpoint and return a DataFrame.

    Handles both inline (recordsFound ≤ 500) and async file mode (> 500)
    transparently. The async path polls the ``urlForDetails`` JSON until
    the job is COMPLETE, then downloads the gzipped CSV.
    """
    full_params = {**params, "apiKey": api_key}
    resp = requests.get(BASE_URL + endpoint, params=full_params, timeout=60)
    if resp.status_code != 200:
        raise CalendarFetchError(f"{endpoint} returned {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    status = payload.get("status", {})
    inline = payload.get("data") or []

    # Inline mode — data is right there.
    if inline:
        return pd.DataFrame(inline)

    # Inline empty + no urlForDetails = no records found.
    details_url = status.get("urlForDetails")
    if not details_url:
        return pd.DataFrame()

    # Async mode — poll details until COMPLETE.
    download_url = _poll_until_ready(
        details_url=details_url,
        api_key=api_key,
        poll_interval=poll_interval,
        poll_timeout=poll_timeout,
    )
    return _download_gzip_csv(download_url, api_key)


def _poll_until_ready(
    *,
    details_url: str,
    api_key: str,
    poll_interval: float,
    poll_timeout: float,
) -> str:
    """Poll ``details_url`` until the job reports COMPLETE; return download URL."""
    deadline = time.time() + poll_timeout
    while True:
        resp = requests.get(details_url, params={"apiKey": api_key}, timeout=60)
        if resp.status_code != 200:
            raise CalendarFetchError(f"details poll returned {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        # Details responses are a JSON array with one job descriptor.
        if not isinstance(body, list) or not body:
            raise CalendarFetchError(f"unexpected details body: {body!r}")
        job = body[0]
        meta = job.get("meta", {})
        files = job.get("data") or []
        if meta.get("status") == "COMPLETE" and files:
            return files[0]["urlForDownload"]
        if time.time() > deadline:
            raise CalendarFetchError(f"poll timeout after {poll_timeout}s on {details_url}")
        time.sleep(poll_interval)


def _download_gzip_csv(url: str, api_key: str) -> pd.DataFrame:
    """Download the gzipped CSV blob and parse to DataFrame."""
    resp = requests.get(url, params={"apiKey": api_key}, timeout=120)
    if resp.status_code != 200:
        raise CalendarFetchError(f"download returned {resp.status_code}: {resp.text[:200]}")
    csv_bytes = gzip.decompress(resp.content)
    return pd.read_csv(io.BytesIO(csv_bytes))


def save_calendar_parquet(df: pd.DataFrame, out_path: Path) -> None:
    """Persist DataFrame to parquet, creating parent dirs as needed."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)


def pull_history_earnings(
    *,
    api_key: str,
    out_dir: Path,
    stock_group: str = "ALL_USA",
    from_date: str = "2010-01-01",
    to_date: str | None = None,
) -> Path:
    """Pull history-earnings-calendar for a whole stockGroup, save to parquet."""
    if to_date is None:
        from datetime import date

        to_date = date.today().isoformat()
    df = fetch_calendar_endpoint(
        endpoint="/equities/eod/history-earnings-calendar",
        params={"stockGroup": stock_group, "from": from_date, "to": to_date},
        api_key=api_key,
    )
    out = out_dir / f"history_earnings_{stock_group}_{from_date}_{to_date}.parquet"
    save_calendar_parquet(df, out)
    logger.info(
        "history-earnings %s [%s..%s]: %d rows → %s", stock_group, from_date, to_date, len(df), out
    )
    return out


def pull_trading_calendar(
    *,
    api_key: str,
    out_dir: Path,
    region: str = "USA",
    from_date: str = "2000-01-01",
    to_date: str | None = None,
) -> Path:
    """Pull trading-calendar for one region, save to parquet."""
    if to_date is None:
        from datetime import date

        to_date = (date.today().replace(year=date.today().year + 2)).isoformat()
    params = {"from": from_date, "to": to_date}
    if region:
        params["region"] = region
    df = fetch_calendar_endpoint(
        endpoint="/equities/trading-calendar",
        params=params,
        api_key=api_key,
    )
    out = out_dir / f"trading_calendar_{region}_{from_date}_{to_date}.parquet"
    save_calendar_parquet(df, out)
    logger.info(
        "trading-calendar %s [%s..%s]: %d rows → %s", region, from_date, to_date, len(df), out
    )
    return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--earnings-from", default="2010-01-01")
    ap.add_argument("--earnings-to", default=None)
    ap.add_argument("--earnings-group", default="ALL_USA")
    ap.add_argument("--calendar-from", default="2000-01-01")
    ap.add_argument("--calendar-to", default=None)
    ap.add_argument("--calendar-region", default="USA")
    ap.add_argument("--skip-earnings", action="store_true")
    ap.add_argument("--skip-calendar", action="store_true")
    args = ap.parse_args(argv)

    api_key = os.environ.get("IVOLATILITY_API_KEY", "")
    if not api_key:
        logger.error("IVOLATILITY_API_KEY not set in environment")
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)

    try:
        if not args.skip_earnings:
            pull_history_earnings(
                api_key=api_key,
                out_dir=args.out_dir,
                stock_group=args.earnings_group,
                from_date=args.earnings_from,
                to_date=args.earnings_to,
            )
        if not args.skip_calendar:
            pull_trading_calendar(
                api_key=api_key,
                out_dir=args.out_dir,
                region=args.calendar_region,
                from_date=args.calendar_from,
                to_date=args.calendar_to,
            )
    except CalendarFetchError as exc:
        logger.error("Fetch failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
