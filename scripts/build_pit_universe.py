"""One-shot full PIT universe build for Phase 2.5.

Fetches XBRL shares outstanding for every ticker in ticker_cik_map.yaml,
bulk-downloads daily OHLCV histories from yfinance, and writes monthly
PIT universe snapshots to ``~/.alphalens/pit_universe/{YYYY-MM}.yaml``.

Each stage is resumable: companyfacts JSON cached per CIK, OHLCV parquet
per ticker, universe yaml per month. Rerun after any crash and only
missing work is redone.

Estimated wall clock for full 2009-2026 build:
  - Companyfacts: ~10k CIKs × 1 call × 8 rps  = ~20 min
  - yfinance:     ~10k tickers × ~1s/ticker   = ~3 hours
  - Snapshots:    ~200 month-ends × build     = ~30 min
  Total: ~6-7 hours, one-time.

Usage:
    SEC_EDGAR_USER_AGENT="AlphaLens your@email.com" \\
        .venv/bin/python scripts/build_pit_universe.py
    # Or run stages individually:
    #   --stage companyfacts   (shares only)
    #   --stage prices         (yfinance only)
    #   --stage snapshots      (assemble only)
    #   --limit 50             (first 50 tickers, for smoke testing)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from alphalens.alt_data.pit_universe import UniverseConfig, build_pit_universe  # noqa: E402
from alphalens.alt_data.russell_universe import load_iwm_current  # noqa: E402
from alphalens.alt_data.sec_edgar_client import SecEdgarClient, SecEdgarError  # noqa: E402
from alphalens.alt_data.shares_outstanding import SharesFact, parse_company_facts  # noqa: E402
from alphalens.alt_data.ticker_cik_map import TickerCikMap  # noqa: E402
from alphalens.alt_data.yfinance_cache import (  # noqa: E402
    download_and_cache,
    load_cached_histories,
)

logger = logging.getLogger(__name__)


_ROOT = Path.home() / ".alphalens"
_COMPANYFACTS_DIR = _ROOT / "companyfacts"
_PRICES_DIR = _ROOT / "prices"
_SNAPSHOTS_DIR = _ROOT / "pit_universe"

_DEFAULT_TICKER_CIK = Path("alphalens/alt_data/data/ticker_cik_map.yaml")
_DEFAULT_IWM = Path("alphalens/alt_data/data/iwm_current.yaml")


def _load_cached_facts(cik: str) -> list[SharesFact] | None:
    path = _COMPANYFACTS_DIR / f"{cik}.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    return parse_company_facts(payload, cik=cik)


def stage_companyfacts(
    client: SecEdgarClient,
    tickers: list[str],
    cik_map: TickerCikMap,
) -> dict[str, list[SharesFact]]:
    """Fetch + cache XBRL companyfacts for each ticker's CIK."""
    _COMPANYFACTS_DIR.mkdir(parents=True, exist_ok=True)
    out: dict[str, list[SharesFact]] = {}
    for i, ticker in enumerate(tickers, start=1):
        cik = cik_map.lookup(ticker)
        if cik is None:
            continue
        cached = _load_cached_facts(cik)
        if cached is not None:
            out[cik] = cached
        else:
            try:
                payload = client.fetch_company_facts(cik)
            except SecEdgarError as exc:
                logger.warning("companyfacts %s (%s): %s", ticker, cik, exc)
                continue
            (_COMPANYFACTS_DIR / f"{cik}.json").write_text(json.dumps(payload))
            out[cik] = parse_company_facts(payload, cik=cik)
        if i % 100 == 0:
            logger.info("companyfacts %d/%d (%.0f%%)", i, len(tickers), i / len(tickers) * 100)
    return out


def stage_prices(
    tickers: list[str],
    start: date,
    end: date,
) -> None:
    """Bulk download OHLCV via yfinance to per-ticker parquet."""
    logger.info("yfinance download %d tickers %s → %s", len(tickers), start, end)
    new = download_and_cache(
        tickers=tickers,
        start=start,
        end=end,
        cache_dir=_PRICES_DIR,
        sleep_between=1.0,
    )
    logger.info("yfinance cache: %d new entries", new)


def stage_snapshots(
    tickers: list[str],
    cik_map: TickerCikMap,
    shares_by_cik: dict[str, list[SharesFact]],
    start: date,
    end: date,
    config: UniverseConfig,
) -> int:
    """Build monthly PIT snapshots; return count written."""
    _SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    histories = load_cached_histories(tickers, _PRICES_DIR)
    logger.info("loaded %d histories from cache", len(histories))

    month_ends = list(pd.date_range(start=start, end=end, freq="ME"))
    written = 0
    for ts in month_ends:
        asof = ts.date()
        snapshot_path = _SNAPSHOTS_DIR / f"{asof.strftime('%Y-%m')}.yaml"
        if snapshot_path.exists():
            continue
        universe = build_pit_universe(
            asof=asof,
            shares_by_cik=shares_by_cik,
            histories=histories,
            cik_map=cik_map,
            config=config,
        )
        snapshot_path.write_text(
            yaml.safe_dump({"asof": asof.isoformat(), "tickers": universe})
        )
        written += 1
    logger.info("snapshots: %d written (of %d month-ends)", written, len(month_ends))
    return written


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--stage",
        choices=["all", "companyfacts", "prices", "snapshots"],
        default="all",
    )
    ap.add_argument("--start", type=date.fromisoformat, default=date(2009, 1, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date.today())
    ap.add_argument("--limit", type=int, default=0, help="Only first N tickers (smoke)")
    ap.add_argument("--tickers", type=str, default="",
                    help="Comma-separated ticker override (bypasses IWM)")
    ap.add_argument("--cap-min", type=float, default=300_000_000.0)
    ap.add_argument("--cap-max", type=float, default=3_000_000_000.0)
    return ap.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args()

    ua = os.environ.get("SEC_EDGAR_USER_AGENT")
    if not ua and args.stage in ("all", "companyfacts"):
        print("ERROR: SEC_EDGAR_USER_AGENT env var required", file=sys.stderr)
        return 2

    cik_map = TickerCikMap.load(_DEFAULT_TICKER_CIK)

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = load_iwm_current(_DEFAULT_IWM)
    if args.limit:
        tickers = tickers[: args.limit]
    logger.info("universe: %d tickers", len(tickers))

    shares_by_cik: dict[str, list[SharesFact]] = {}
    t0 = time.time()

    if args.stage in ("all", "companyfacts"):
        client = SecEdgarClient(user_agent=ua)
        shares_by_cik = stage_companyfacts(client, tickers, cik_map)
        logger.info("companyfacts stage done: %.1fs", time.time() - t0)
    else:
        for ticker in tickers:
            cik = cik_map.lookup(ticker)
            if cik is None:
                continue
            cached = _load_cached_facts(cik)
            if cached is not None:
                shares_by_cik[cik] = cached

    if args.stage in ("all", "prices"):
        stage_prices(tickers, args.start, args.end)

    if args.stage in ("all", "snapshots"):
        config = UniverseConfig(cap_min_usd=args.cap_min, cap_max_usd=args.cap_max)
        stage_snapshots(tickers, cik_map, shares_by_cik, args.start, args.end, config)

    logger.info("TOTAL: %.1fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
