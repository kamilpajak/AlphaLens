"""Build optionable-universe filter from Polygon chain references.

For each delisted ticker in delisted_2021_2026.parquet, queries Polygon
/v3/reference/options/contracts at delisted_date - 30d. Marks ticker as
optionable if any contracts returned. Persists result to parquet for
reuse by probe v3.

Per zen CR (2026-05-01 PM): probe must sample from objective optionable
universe (random subset of tickers that actually had options markets),
not from full 2970 delisted parquet which contains 60%+ no-options
small-caps and SPACs that depress retention metrics meaninglessly.

Run:
    .venv/bin/python scripts/build_optionable_universe.py
    .venv/bin/python scripts/build_optionable_universe.py --resume

Output:
    ~/.alphalens/survivorship/optionable_delisted_2018_2024.parquet
        Columns: ticker, delisted_date, name, reason, n_contracts, optionable
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd
from alphalens_pipeline.data.alt_data.polygon_client import (
    PolygonError,
    PolygonRateLimitError,
    get_default_polygon_client,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SURVIVORSHIP_PARQUET = Path.home() / ".alphalens" / "survivorship" / "delisted_2021_2026.parquet"
OUTPUT_PARQUET = (
    Path.home() / ".alphalens" / "survivorship" / "optionable_delisted_2018_2024.parquet"
)

WINDOW_START = pd.Timestamp("2018-04-30")
WINDOW_END = pd.Timestamp("2024-04-30")

DELAY_BETWEEN_REQUESTS = 0.05  # 20 req/s — Polygon Starter unlimited but be polite


def _query_chain_count(client, ticker: str, as_of: str) -> tuple[int, str | None]:
    """Returns (n_contracts, error). n_contracts=-1 on error.

    HTTP, 429/Retry-After handling, and Bearer auth are owned by the canonical
    PolygonClient. We only do a single paginated call here (``max_pages=1``)
    because the script is interested in "are there contracts at all?", not the
    full chain.
    """
    try:
        results = client.get_options_contracts(
            underlying_ticker=ticker,
            as_of=as_of,
            limit=1000,
            max_pages=1,
        )
        return len(results), None
    except PolygonRateLimitError as e:
        return -1, f"rate-limit: {e}"
    except PolygonError as e:
        return -1, str(e)[:120]
    except Exception as e:
        return -1, str(e)[:120]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--resume", action="store_true", help="skip tickers already in output parquet"
    )
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    client = get_default_polygon_client()
    df = pd.read_parquet(SURVIVORSHIP_PARQUET)
    mask = (df["delisted_date"] >= WINDOW_START) & (df["delisted_date"] <= WINDOW_END)
    pool = df.loc[mask].copy().reset_index(drop=True)

    if args.resume and OUTPUT_PARQUET.exists():
        existing = pd.read_parquet(OUTPUT_PARQUET)
        already_done = set(existing["ticker"])
        pool = pool[~pool["ticker"].isin(already_done)].reset_index(drop=True)
        results = existing.to_dict("records")
        logger.info("Resuming: %d already done, %d remaining", len(already_done), len(pool))
    else:
        results = []

    if args.limit:
        pool = pool.head(args.limit)

    logger.info("Probing %d tickers via Polygon chain refs", len(pool))
    start = time.time()
    for i, row in pool.iterrows():
        ticker = row["ticker"]
        delisted = row["delisted_date"]
        as_of = (delisted - timedelta(days=30)).strftime("%Y-%m-%d")

        n, err = _query_chain_count(client, ticker, as_of)
        results.append(
            {
                "ticker": ticker,
                "delisted_date": delisted,
                "name": row.get("name"),
                "reason": row.get("reason"),
                "n_contracts": n,
                "optionable": n > 0,
                "polygon_error": err,
            }
        )

        if (i + 1) % 100 == 0:
            elapsed = time.time() - start
            rate = (i + 1) / elapsed
            eta = (len(pool) - i - 1) / rate
            n_optionable = sum(1 for r in results if r.get("optionable"))
            logger.info(
                "progress: %d/%d (%.1f req/s, ETA %.0fs, optionable so far: %d)",
                i + 1,
                len(pool),
                rate,
                eta,
                n_optionable,
            )

        time.sleep(DELAY_BETWEEN_REQUESTS)

        # Incremental persistence every 200 records (resume safety)
        if (i + 1) % 200 == 0:
            pd.DataFrame(results).to_parquet(OUTPUT_PARQUET, index=False)

    pd.DataFrame(results).to_parquet(OUTPUT_PARQUET, index=False)

    df_out = pd.DataFrame(results)
    n_total = len(df_out)
    n_optionable = int(df_out["optionable"].sum())
    n_errors = int((df_out["n_contracts"] == -1).sum())

    logger.info("Done in %.1fs", time.time() - start)
    logger.info(
        "Total: %d, Optionable: %d (%.1f%%), Errors: %d",
        n_total,
        n_optionable,
        100 * n_optionable / max(n_total, 1),
        n_errors,
    )
    logger.info("Output: %s", OUTPUT_PARQUET)
    print(f"\nOptionable / total: {n_optionable} / {n_total}")
    print("By reason:")
    for reason, sub in df_out.groupby("reason"):
        opt = int(sub["optionable"].sum())
        print(f"  {reason}: {opt}/{len(sub)} optionable ({100 * opt / len(sub):.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
