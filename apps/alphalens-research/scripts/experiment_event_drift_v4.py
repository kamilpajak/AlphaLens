"""event_drift v4 PEAD x quality experiment runner — S&P 1500 universe.

In-class extension of v3 (event_drift_search_2026_05_03). Sole change vs
``experiment_event_drift_v3.py``: universe loader swapped from
``_pit_universe_union`` (R2000-PIT yamls in ``~/.alphalens/pit_universe/``)
to ``alphalens_research.data.universes.sp1500_pit.load_sp1500_pit_for_date`` (S&P
500 + 400 + 600 fallback snapshots in ``data/{sp500,sp400,sp600}_pit/``).

Modes:

  - ``smoke``: fast in-sample sanity on a small universe (n=50-100) over a
    short period (2018-2019). Prints sample portfolio composition.
  - ``breadth-audit``: per-asof daily portfolio cardinality on the burnt
    holdout 2024-04-30..2026-04-30. Asserts mean >= 10, p10 >= 5 per
    pre-reg ``min_daily_portfolio_breadth``. GO/NO-GO before holdout run.
  - ``holdout``: actual holdout backtest (Phase 3 — runs after breadth
    audit passes).

Pre-reg: docs/research/preregistration/ledger.json
  ``event_drift_v4_pead_quality_sp1500``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
from alphalens_research.data.alt_data.ticker_cik_map import TickerCikMap
from alphalens_research.data.alt_data.yfinance_cache import load_cached_histories
from alphalens_research.data.fundamentals.companyfacts_parquet import CompanyfactsParquetReader
from alphalens_research.data.fundamentals.sue import FosterSUEStore
from alphalens_research.data.store.history import HistoryStore
from alphalens_research.data.universes.sp1500_pit import load_sp1500_pit_for_date
from alphalens_research.screeners.event_drift.accruals import SloanAccrualsStore
from alphalens_research.screeners.event_drift.announcement_dates import (
    AnnouncementDateProvider,
    EarningsAnnouncement,
)
from alphalens_research.screeners.event_drift.day1_filter import day1_return
from alphalens_research.screeners.event_drift.score_pead_quality import score_pead_quality
from alphalens_research.screeners.event_drift.sector_filter import SectorFilter

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()
COMPANYFACTS_DIR = (
    HOME / ".alphalens" / "companyfacts"
)  # legacy JSON tree, kept read-only post-migration
COMPANYFACTS_PARQUET_DIR = HOME / ".alphalens" / "companyfacts_parquet"
PRICES_DIR = HOME / ".alphalens" / "prices"
CIK_MAP_PATH = (
    REPO_ROOT / "alphalens_research" / "data" / "alt_data" / "data" / "ticker_cik_map.yaml"
)

ETFS = ("SPY", "QQQ", "IWM", "MDY")


def _sp1500_universe_for_window(start: date, end: date) -> list[str]:
    """Return union of S&P 1500 PIT membership across the [start, end] window.

    Polls one snapshot per year (Jan 1 of each year touched). Mirrors the
    v3 ``_pit_universe_union`` pattern but reads from the S&P 1500 fallback
    yaml convention rather than R2000-PIT.
    """
    union: set[str] = set()
    cur_year = start.year
    while cur_year <= end.year:
        try:
            tickers = load_sp1500_pit_for_date(pd.Timestamp(date(cur_year, 1, 1)))
        except Exception as exc:
            logger.warning("S&P 1500 load for %d-01-01 failed: %s", cur_year, exc)
            cur_year += 1
            continue
        union.update(tickers)
        cur_year += 1
    return sorted(union)


class _HistoryCalendar:
    """TradingCalendar bound to SPY history (NYSE schedule, 1998-present)."""

    def __init__(self, spy_index: pd.DatetimeIndex) -> None:
        self._dates = sorted({d.date() for d in spy_index})
        self._date_set = set(self._dates)

    def snap_to_trading_day(self, d: date) -> date:
        if d in self._date_set:
            return d
        idx = self._search_next(d)
        if idx is None:
            return d
        return self._dates[idx]

    def next_trading_day(self, d: date) -> date:
        snapped = self.snap_to_trading_day(d)
        if snapped > d:
            return snapped
        idx = self._dates.index(snapped)
        if idx + 1 >= len(self._dates):
            return snapped
        return self._dates[idx + 1]

    def add_trading_days(self, d: date, n: int) -> date:
        snapped = self.snap_to_trading_day(d)
        try:
            idx = self._dates.index(snapped)
        except ValueError:
            return snapped
        target = idx + n
        if target < 0:
            return self._dates[0]
        if target >= len(self._dates):
            return self._dates[-1]
        return self._dates[target]

    def _search_next(self, d: date) -> int | None:
        lo, hi = 0, len(self._dates)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._dates[mid] < d:
                lo = mid + 1
            else:
                hi = mid
        return lo if lo < len(self._dates) else None


def _build_announcement_index(
    provider: AnnouncementDateProvider, tickers: list[str]
) -> dict[str, list[EarningsAnnouncement]]:
    index: dict[str, list[EarningsAnnouncement]] = {}
    for t in tickers:
        anns = provider.announcements(t)
        if anns:
            index[t] = anns
    return index


def _make_announcement_lookup(raw_index: Mapping[str, list[EarningsAnnouncement]], asof: date):
    def lookup(ticker: str) -> list[EarningsAnnouncement]:
        anns = raw_index.get(ticker, [])
        return [a for a in anns if a.filed_date <= asof]

    return lookup


def _make_day1_lookup(history: HistoryStore):
    def lookup(ticker: str, market_day: date) -> float | None:
        df = history.full(ticker) if ticker in {*history.tickers()} else None
        if df is None or df.empty:
            return None
        return day1_return(prices=df["close"], market_day=market_day)

    return lookup


def _adv_filter(
    history: HistoryStore, tickers: list[str], asof: date, *, adv_min_usd: float, window_days: int
) -> list[str]:
    keep: list[str] = []
    asof_ts = pd.Timestamp(asof)
    for t in tickers:
        try:
            df = history.full(t)
        except KeyError:
            continue
        recent = df.loc[df.index <= asof_ts].tail(window_days)
        if len(recent) < window_days // 2:
            continue
        dollar_vol = (recent["close"] * recent["volume"]).mean()
        if pd.notna(dollar_vol) and dollar_vol >= adv_min_usd:
            keep.append(t)
    return keep


def _setup_stores(universe: list[str]) -> tuple:
    cik_map = TickerCikMap.load(CIK_MAP_PATH)
    histories_dict = load_cached_histories(universe + list(ETFS), PRICES_DIR)
    history = HistoryStore(histories_dict)

    cf_reader = CompanyfactsParquetReader(COMPANYFACTS_PARQUET_DIR)
    sue_store = FosterSUEStore(cf_reader, cik_map)
    accruals_store = SloanAccrualsStore(cf_reader, cik_map)
    announce_provider = AnnouncementDateProvider(cf_reader, cik_map)

    if "SPY" not in histories_dict:
        raise RuntimeError(
            f"SPY not in cached histories ({PRICES_DIR}). Cannot build trading calendar."
        )
    calendar = _HistoryCalendar(histories_dict["SPY"].index)

    sector_filter = SectorFilter(sic_map={}, unknown_policy="include")

    return cik_map, history, sue_store, accruals_store, announce_provider, calendar, sector_filter


def _run_breadth_audit(args: argparse.Namespace) -> int:
    """Fast breadth audit on S&P 1500 universe — same algorithm as v3."""
    from alphalens_research.screeners.event_drift.day1_filter import day1_sign_confirmed
    from alphalens_research.screeners.event_drift.event_window import (
        EventWindow,
        apply_single_active_window,
        windows_active_on,
    )
    from alphalens_research.screeners.event_drift.t0_timing import (
        drift_entry_day,
        drift_exit_day,
        market_announcement_day,
    )

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    universe = _sp1500_universe_for_window(start, end)
    universe_with_history = [t for t in universe if (PRICES_DIR / f"{t}.parquet").exists()]
    print(
        f"S&P 1500 union: {len(universe)}; with cached OHLCV: {len(universe_with_history)}",
        flush=True,
    )

    if args.max_tickers and args.max_tickers < len(universe_with_history):
        universe_with_history = universe_with_history[: args.max_tickers]
        print(f"Truncated to first {args.max_tickers} tickers for fast audit", flush=True)

    t0 = time.time()
    (cik_map, history, sue_store, accruals_store, announce_provider, calendar, sector_filter) = (
        _setup_stores(universe_with_history)
    )
    print(f"Stores loaded in {time.time() - t0:.1f}s", flush=True)

    t1 = time.time()
    announcement_index = _build_announcement_index(announce_provider, universe_with_history)
    n_total_announce = sum(len(v) for v in announcement_index.values())
    print(
        f"Announcement index: {len(announcement_index)} tickers, "
        f"{n_total_announce} total announcements ({time.time() - t1:.1f}s)",
        flush=True,
    )

    t1b = time.time()
    all_windows: list[EventWindow] = []
    skipped_no_sue = 0
    skipped_no_accruals = 0
    skipped_excluded_sector = 0
    skipped_no_history = 0
    history_tickers = set(history.tickers())
    for i, ticker in enumerate(universe_with_history):
        if ticker not in history_tickers:
            skipped_no_history += 1
            continue
        if sector_filter.is_excluded(ticker):
            skipped_excluded_sector += 1
            continue
        anns = announcement_index.get(ticker, [])
        if not anns:
            continue
        for ann in anns:
            if ann.filed_date < start - timedelta(days=120):
                continue
            if ann.filed_date > end:
                continue
            sue = sue_store.sue(ticker, ann.filed_date)
            if sue is None:
                skipped_no_sue += 1
                continue
            accruals = accruals_store.accruals_ratio(ticker, ann.filed_date)
            if accruals is None:
                skipped_no_accruals += 1
                continue
            market_day = market_announcement_day(
                ann.filed_date, ann.accepted_hour_et, calendar=calendar
            )
            entry = drift_entry_day(
                ann.filed_date, ann.accepted_hour_et, calendar=calendar, skip_days=2
            )
            exit_d = drift_exit_day(
                ann.filed_date, ann.accepted_hour_et, calendar=calendar, exit_days=60
            )
            all_windows.append(
                EventWindow(
                    ticker=ticker,
                    market_day=market_day,
                    entry_day=entry,
                    exit_day=exit_d,
                    sue=float(sue),
                    accruals_ratio=float(accruals),
                )
            )
        if (i + 1) % 200 == 0:
            print(
                f"  built windows for {i + 1}/{len(universe_with_history)} tickers; "
                f"running total = {len(all_windows)} windows",
                flush=True,
            )
    elapsed_build = time.time() - t1b
    print(
        f"Built {len(all_windows)} event windows in {elapsed_build:.1f}s "
        f"(skipped: {skipped_no_sue} no-SUE, {skipped_no_accruals} no-accruals, "
        f"{skipped_excluded_sector} excluded-sector, {skipped_no_history} no-history)",
        flush=True,
    )

    t1c = time.time()
    deduped = apply_single_active_window(all_windows)
    print(
        f"After single-active-window invariant: {len(deduped)} windows kept, "
        f"{len(all_windows) - len(deduped)} dropped ({time.time() - t1c:.1f}s)",
        flush=True,
    )

    t1d = time.time()
    day1_cache: dict[tuple[str, date], float | None] = {}
    for w in deduped:
        key = (w.ticker, w.market_day)
        if key in day1_cache:
            continue
        try:
            close_series = history.full(w.ticker)["close"]
        except KeyError:
            day1_cache[key] = None
            continue
        from alphalens_research.screeners.event_drift.day1_filter import day1_return

        day1_cache[key] = day1_return(prices=close_series, market_day=w.market_day)
    print(
        f"Day-1 returns cached for {len(day1_cache)} (ticker, market_day) pairs ({time.time() - t1d:.1f}s)",
        flush=True,
    )

    if getattr(args, "no_day1", False):
        survivors = list(deduped)
        print(
            f"Day-1 sign confirmation DISABLED via --no-day1 (retry override). "
            f"All {len(survivors)} windows pass through.",
            flush=True,
        )
    else:
        survivors = [
            w
            for w in deduped
            if day1_sign_confirmed(sue=w.sue, day1_return=day1_cache.get((w.ticker, w.market_day)))
        ]
        print(
            f"After Day-1 sign confirmation: {len(survivors)} windows kept "
            f"({len(deduped) - len(survivors)} dropped)",
            flush=True,
        )

    asof_strides = pd.bdate_range(start=start, end=end, freq="W-FRI")
    asofs = [ts.date() for ts in asof_strides]
    print(f"Sampling {len(asofs)} asof Fridays in [{start}, {end}]", flush=True)

    breadths: list[int] = []
    sample_compositions: list[tuple[date, list[str]]] = []
    t2 = time.time()
    for i, asof in enumerate(asofs):
        active = windows_active_on(survivors, asof)
        if not active:
            breadths.append(0)
            continue

        cohort_lookback = asof - timedelta(days=90)
        cohort = [w for w in survivors if cohort_lookback <= w.market_day <= asof]
        if len(cohort) < 2:
            sue_threshold = -float("inf")
            accruals_threshold = float("inf")
        else:
            sue_values = np.asarray([w.sue for w in cohort], dtype=float)
            acc_values = np.asarray([w.accruals_ratio for w in cohort], dtype=float)
            sue_threshold = float(np.percentile(sue_values, 100.0 - args.sue_top_pct))
            accruals_threshold = float(np.percentile(acc_values, args.accrual_bottom_pct))

        portfolio = [
            w for w in active if w.sue >= sue_threshold and w.accruals_ratio <= accruals_threshold
        ]

        keep_after_adv: list[EventWindow] = []
        asof_ts = pd.Timestamp(asof)
        for w in portfolio:
            try:
                df_t = history.full(w.ticker)
            except KeyError:
                continue
            recent = df_t.loc[df_t.index <= asof_ts].tail(60)
            if len(recent) < 30:
                continue
            dollar_vol = (recent["close"] * recent["volume"]).mean()
            if pd.notna(dollar_vol) and dollar_vol >= 5_000_000.0:
                keep_after_adv.append(w)

        breadths.append(len(keep_after_adv))
        if i < 3 or i % 10 == 0:
            sample_compositions.append(
                (asof, [w.ticker for w in sorted(keep_after_adv, key=lambda x: -x.sue)[:15]])
            )

    elapsed = time.time() - t2
    print(
        f"\nBreadth audit complete in {elapsed:.1f}s ({elapsed / max(1, len(asofs)):.2f}s/asof)\n",
        flush=True,
    )

    arr = np.asarray(breadths)
    summary = {
        "n_asofs": len(arr),
        "mean_daily_breadth": float(arr.mean()),
        "median": float(np.percentile(arr, 50)),
        "p10": float(np.percentile(arr, 10)),
        "p25": float(np.percentile(arr, 25)),
        "p75": float(np.percentile(arr, 75)),
        "p90": float(np.percentile(arr, 90)),
        "min": int(arr.min()),
        "max": int(arr.max()),
        "n_zero_days": int((arr == 0).sum()),
    }
    print("Daily portfolio cardinality (Friday-stride samples):")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    print("\nSample compositions (date -> top 15 tickers by SUE):")
    for asof, tickers in sample_compositions[:8]:
        print(f"  {asof}: {tickers}")

    pre_reg_min_breadth = 10
    pre_reg_min_p10 = 5
    pass_mean = summary["mean_daily_breadth"] >= pre_reg_min_breadth
    pass_p10 = summary["p10"] >= pre_reg_min_p10
    verdict = "PASS" if pass_mean and pass_p10 else "FAIL"
    print(
        f"\nBreadth-audit verdict: {verdict} "
        f"(mean>={pre_reg_min_breadth}: {pass_mean}; p10>={pre_reg_min_p10}: {pass_p10})"
    )

    if args.output:
        Path(args.output).write_text(
            json.dumps(
                {
                    "summary": summary,
                    "params": {
                        "start": str(start),
                        "end": str(end),
                        "n_universe": len(universe_with_history),
                        "n_with_announcements": len(announcement_index),
                        "max_tickers_truncation": args.max_tickers,
                        "universe_source": "S&P 1500 PIT (sp500_pit + sp400_pit + sp600_pit FALLBACK)",
                    },
                    "verdict": verdict,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"Wrote summary to {args.output}")

    return 0 if verdict == "PASS" else 2


def _run_smoke(args: argparse.Namespace) -> int:
    print("Smoke mode — single-asof sanity check.\n")
    asof = date.fromisoformat(args.asof) if args.asof else date(2019, 6, 28)
    print(f"asof = {asof}")

    universe = _sp1500_universe_for_window(asof - timedelta(days=730), asof + timedelta(days=365))
    universe_with_history = [t for t in universe if (PRICES_DIR / f"{t}.parquet").exists()]
    if args.max_tickers:
        universe_with_history = universe_with_history[: args.max_tickers]

    (cik_map, history, sue_store, accruals_store, announce_provider, calendar, sector_filter) = (
        _setup_stores(universe_with_history)
    )

    announcement_index = _build_announcement_index(announce_provider, universe_with_history)
    ann_lookup = _make_announcement_lookup(announcement_index, asof)
    day1_lookup = _make_day1_lookup(history)

    candidate_universe = [t for t in universe_with_history if ann_lookup(t)]
    candidate_universe = _adv_filter(
        history, candidate_universe, asof, adv_min_usd=5_000_000.0, window_days=60
    )
    print(
        f"Universe at {asof}: {len(candidate_universe)} tickers with active announcements + ADV>=5M"
    )

    df = score_pead_quality(
        asof=asof,
        universe=candidate_universe,
        sue_lookup=lambda t, pe: sue_store.sue(t, pe),
        accruals_lookup=lambda t, pe: accruals_store.accruals_ratio(t, pe),
        announcement_lookup=ann_lookup,
        day1_return_lookup=day1_lookup,
        sector_filter=sector_filter,
        calendar=calendar,
        sue_quantile_top_pct=20.0,
        accrual_quantile_bottom_pct=50.0,
    )
    print(f"\nPortfolio composition at {asof}:")
    print(df.to_string())
    print(f"\nDaily portfolio size: {len(df)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("smoke", "breadth-audit", "holdout"), required=True)
    parser.add_argument("--start", default="2024-04-30")
    parser.add_argument("--end", default="2026-04-30")
    parser.add_argument("--asof", default=None, help="single asof (smoke mode only)")
    parser.add_argument("--max-tickers", type=int, default=None)
    parser.add_argument("--output", default=None, help="JSON output path")
    parser.add_argument("--sue-top-pct", type=float, default=20.0)
    parser.add_argument("--accrual-bottom-pct", type=float, default=50.0)
    parser.add_argument(
        "--no-day1",
        action="store_true",
        help="Drop Day-1 sign confirmation filter (pre-reg retry option)",
    )
    args = parser.parse_args(argv)

    if args.mode == "smoke":
        return _run_smoke(args)
    if args.mode == "breadth-audit":
        return _run_breadth_audit(args)
    print("Holdout mode not yet implemented in this script — Phase 3 task.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
