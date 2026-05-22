"""insider_pc_compound_2026_05_10 — 2-way compound (insider_form4 x P/C abnormal).

Pre-reg context:
- Class: insider_pc_compound_2026_05_10 (LOCKED 2026-05-10, PR #89)
- Design memo: docs/research/insider_pc_compound_design_2026_05_10.md
- Bonferroni effective n=34 (alpha-class +1 + zen Z5 +6 selection penalty for
  implicit C(4,2) over 4 candidate signals); critical |t| >= 2.974.
- Components frozen at design time:
    * insider_form4_opportunistic_2026_05_08_v2 (PASS_MARGINAL on both windows)
    * pc_abnormal_volume_retrospective_pre_2018_2026_05_05 (INCONCLUSIVE/MID)
- Compound formula (memo Section 3.1): per-asof, cross-sectional z-score each
  component (ddof=1, no clipping), strict-intersection equal-weight average.
- Universe: R2000 PIT; rebalance stride 21d (P/C pinned 5d->21d for parity);
  benchmark IWM; HAC maxlags=126.
- Verdict matrix (memo Section 5.1): joint-PASS rule on both
  OOS 2018-2023 + final-lock 2024-2026. Capital deploy off-table.

Per zen review 2026-05-10, the Form-4 adapter classes (ClassifierCache,
_StaticTickerCikResolver, _OpportunisticForm4Scorer) are duplicated verbatim
from experiment_insider_form4_opportunistic.py rather than imported. Both
experiments are LOCKED; cross-importing across `scripts/` couples two locked
artifacts and risks silent breakage if either is later refactored.

Pre-screen reproducibility guard: by default, re-runs the IS-window 2014-2017
signal-independence check before the audit window. The IS dates are HARDCODED
in this script (NOT inherited from --is-start / --is-end) to prevent the
guard from accidentally consuming the audit holdout.

The guard auto-skips when --phase-offset != 0 because the multi-phase audit
orchestrator runs one subprocess per phase, and the IS panel inputs are
identical across phases — running the ~30min check 20+ times adds no
information. Phase 0 still fires the guard. Pass --skip-precheck to suppress
even on phase 0 (use on runpod when the guard has already cleared locally).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from alphalens_research.attribution.cost_model import RealisticCostModel  # noqa: E402
from alphalens_research.attribution.factor_analysis import run_regression  # noqa: E402
from alphalens_research.backtest.daily_continuous_returns import (  # noqa: E402
    daily_continuous_returns,
)
from alphalens_research.backtest.engine import BacktestEngine  # noqa: E402
from alphalens_research.backtest.metrics import sharpe, turnover_pct  # noqa: E402
from alphalens_research.data.alt_data.pit_universe_loader import (  # noqa: E402
    load_universe_union,
)
from alphalens_research.data.alt_data.ticker_cik_map import TickerCikMap  # noqa: E402
from alphalens_research.data.alt_data.yfinance_cache import load_cached_histories  # noqa: E402
from alphalens_research.data.factors import load_carhart_daily  # noqa: E402
from alphalens_research.data.store.form4_pit import (  # noqa: E402
    PARTITION_KEY,
    Form4PITStore,
)
from alphalens_research.data.store.history import HistoryStore  # noqa: E402
from alphalens_research.screeners.compound_insider_pc import (  # noqa: E402
    compound_score_from_components,
)
from alphalens_research.screeners.distress_credit.features import (  # noqa: E402
    make_production_stores,
)
from alphalens_research.screeners.insider_activity.cohen_malloy_classifier import (  # noqa: E402
    CohenMalloyLabel,
    classify_from_transaction_dates,
)
from alphalens_research.screeners.insider_activity.opportunistic_form4 import (  # noqa: E402
    aggregate_opportunistic_signal,
    score_opportunistic_form4,
)
from alphalens_research.screeners.options_implied.features import (  # noqa: E402
    _compute_equity_controls,
)
from alphalens_research.screeners.options_volume.features import build_feature_frame  # noqa: E402
from alphalens_research.screeners.options_volume.pc_abnormal_volume import (  # noqa: E402
    score_pc_abnormal_residual,
)

logger = logging.getLogger(__name__)

_PRICES_DIR = Path.home() / ".alphalens" / "prices"
_FORM4_PARQUET_DEFAULT = Path.home() / ".alphalens" / "form4_parquet"
_SMD_DIR = Path.home() / ".alphalens" / "ivolatility_smd"
_CARHART_FACTORS = ["Mkt-RF", "SMB", "HML", "Mom"]

# Pre-reg locks (memo Section 3.1 + 5.1)
_HAC_MAXLAGS_LOCK = 126
_REBALANCE_STRIDE_LOCK = 21
_BONFERRONI_THRESHOLD = 2.974

# Pre-screen reproducibility guard window (memo Section 3.5).
# Hardcoded; never inherits the audit window args.
_PRECHECK_IS_START = date(2014, 1, 1)
_PRECHECK_IS_END = date(2017, 12, 31)

# Pre-reg memo §7 risk #4 mitigation: SHA256-lock the two component scorer
# modules that mathematically define the compound's per-asof score. If
# either drifts during the audit (~30h pod compute), the result is no
# longer the pre-registered test and must not be used as a verdict. The
# guard fires at every run-time invocation (cheap; ~ms per file). Update
# these hashes ONLY in coordination with a design-memo amendment + a new
# pre-reg class registration in the ledger.
_COMPONENT_LOCKED_HASHES: dict[str, str] = {
    "alphalens_research/screeners/insider_activity/opportunistic_form4.py": (
        "59ee0cd59f51f5d842b510a1e5533c36f6237abd4618948f4b3a384e03b3d932"
    ),
    "alphalens_research/screeners/options_volume/pc_abnormal_volume.py": (
        "d53ab6af4c3842208ea17a291f16de60efece43c89afeb952001864793c0e7d1"
    ),
}


def _verify_component_hashes() -> None:
    """Raise RuntimeError if either locked component module drifted.

    Pre-reg memo `insider_pc_compound_design_2026_05_10.md` §7 risk #4:
    *"Phase 0 hash check on both component scorer modules at compound
    run time."* Drift = silent verdict invalidation.
    """
    import hashlib

    for rel_path, expected_hash in _COMPONENT_LOCKED_HASHES.items():
        file_path = REPO_ROOT / rel_path
        if not file_path.is_file():
            raise RuntimeError(f"PRE-REG GUARD: locked component module missing at {file_path}")
        actual_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"PRE-REG VIOLATION: {rel_path} drifted from its locked SHA256.\n"
                f"  expected: {expected_hash}\n"
                f"  actual:   {actual_hash}\n"
                "The compound audit verdict is mathematically defined by these "
                "two scorer modules. Restore them to locked state OR amend the "
                "design memo + increment program-level Bonferroni n in the "
                "ledger BEFORE proceeding."
            )


# -------------------------------------------------------------------------
# Form-4 adapter classes — DUPLICATED verbatim from
# scripts/experiment_insider_form4_opportunistic.py to isolate this locked
# compound experiment from any later refactor of the Form-4 driver script.
# Per zen review 2026-05-10. If a third compound consumes the same classes,
# extract to alphalens_research/screeners/insider_activity/scorer_adapter.py.
# -------------------------------------------------------------------------


class ClassifierCache:
    """Caches Cohen-Malloy labels per (person_cik, year_y)."""

    def __init__(self, store: Form4PITStore):
        self._store = store
        self._cache: dict[tuple[str, int], CohenMalloyLabel] = {}

    def get(self, person_cik: str, classification_year: int) -> CohenMalloyLabel:
        key = (person_cik, classification_year)
        if key not in self._cache:
            history = self._store.records_for_person(person_cik, classification_year)
            dates = history["transaction_date"].tolist() if not history.empty else []
            self._cache[key] = classify_from_transaction_dates(
                dates, classification_year=classification_year
            )
        return self._cache[key]


class _StaticTickerCikResolver:
    """Wrap the offline ticker_cik_map.yaml as a CIK resolver for Form4PITStore."""

    def __init__(self, mapping: dict[str, str]):
        self._mapping = {k.upper(): v for k, v in mapping.items()}

    def lookup(self, ticker: str) -> str | None:
        return self._mapping.get(ticker.upper())


def _check_backfill_exists(parquet_root: Path) -> None:
    if not parquet_root.is_dir():
        sys.stderr.write(
            f"ERROR: Form-4 parquet backfill missing at {parquet_root}.\n"
            "Run the SEC EDGAR backfill on runpod first; see "
            "docs/research/insider_form4_opportunistic_runpod_handoff.md\n"
        )
        sys.exit(2)
    if not list(parquet_root.glob(f"{PARTITION_KEY}=*")):
        sys.stderr.write(f"ERROR: Form-4 parquet root {parquet_root} has no partitions.\n")
        sys.exit(2)


def benchmark_returns(
    history_store: HistoryStore, benchmark: str, start: date, end: date
) -> pd.Series:
    df = history_store.full(benchmark)
    df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    return df["close"].pct_change().dropna()


def _close_at(history: pd.DataFrame, asof: pd.Timestamp) -> float | None:
    sliced = history[history.index <= asof]
    if sliced.empty:
        return None
    return float(sliced["close"].iloc[-1])


# -------------------------------------------------------------------------
# Process-pool plumbing for the Form-4 scorer (Refactor B from the perf
# optimization plan). Each worker process runs `_init_worker` ONCE on
# startup to build its own Form4PITStore + ClassifierCache, then
# `_score_one_ticker` reuses those globals for every dispatched ticker.
# - OMP_NUM_THREADS=1 + pyarrow.set_cpu_count(1) prevents BLAS / PyArrow
#   thread-pool oversubscription (8 workers × 8 implicit threads = 64).
# - ClassifierCache survives across all rebalances within a worker,
#   since the executor is persistent across engine.run.
# Per zen review 2026-05-10.
# -------------------------------------------------------------------------

_worker_store: Form4PITStore | None = None
_worker_cache: ClassifierCache | None = None


def _init_worker(parquet_root: Path, ticker_cik_resolver) -> None:
    """One-time worker-process setup: thread caps + store + cache."""
    global _worker_store, _worker_cache  # noqa: PLW0603
    os.environ["OMP_NUM_THREADS"] = "1"
    # PyArrow is a hard dep (Form4PITStore.records_as_of uses pyarrow.dataset),
    # so import unconditionally; an ImportError here means the environment
    # is broken and the worker would fail anyway on first records_as_of call.
    import pyarrow as pa

    pa.set_cpu_count(1)
    _worker_store = Form4PITStore(
        parquet_root=parquet_root,
        ticker_cik_resolver=ticker_cik_resolver,
        delisting_events=None,
    )
    _worker_cache = ClassifierCache(_worker_store)


def _score_one_ticker(args_tuple: tuple) -> dict:
    """Worker payload — globals already initialized via _init_worker."""
    ticker, asof_date, mcap, controls = args_tuple
    records = _worker_store.records_as_of(ticker, asof=asof_date, lookback_days=180)
    net_oppor_usd = aggregate_opportunistic_signal(
        records, asof=asof_date, classifier_cache=_worker_cache
    )
    signal_raw = float(net_oppor_usd) / float(mcap)
    return {
        "asof": asof_date,
        "ticker": ticker,
        "signal_raw": signal_raw,
        **controls,
    }


class _OpportunisticForm4Scorer:
    """BacktestEngine adapter — for each rebalance asof, build features and score.

    Heavy parts (Form-4 records_as_of + classifier cache lookups) are dispatched
    to a persistent ProcessPoolExecutor (lazily created on first __call__,
    torn down via shutdown()). Parent-side work — equity controls, close,
    shares-store-backed PIT mcap — stays in the main process to keep
    shares_store off the pickle path AND preserve PIT correctness over the
    audit window. Set ALPHALENS_WORKERS=1 for serial execution (no pool).
    """

    def __init__(
        self,
        *,
        form4_store: Form4PITStore,
        classifier_cache: ClassifierCache,
        mcap_lookup: dict[str, float] | None = None,
        shares_store=None,
    ):
        self._store = form4_store
        self._cache = classifier_cache
        self._shares = shares_store
        self._mcap_lookup = mcap_lookup or {}
        self._executor: ProcessPoolExecutor | None = None
        # Pickle-friendly snapshots for worker-pool initargs:
        self._parquet_root = form4_store.parquet_root
        self._cik_resolver = form4_store.ticker_cik_resolver

    def _ensure_executor(self) -> None:
        """Lazy persistent pool. Survives across all rebalances within a phase."""
        if self._executor is not None:
            return
        n_workers = int(os.environ.get("ALPHALENS_WORKERS", "8"))
        if n_workers <= 1:
            return  # serial mode — no pool created
        self._executor = ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_init_worker,
            initargs=(self._parquet_root, self._cik_resolver),
        )

    def shutdown(self) -> None:
        """Release worker processes. Call after engine.run completes."""
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    def __call__(self, histories, config=None) -> pd.DataFrame:
        cfg = dict(config or {})
        asof = cfg.get("asof")
        if asof is None:
            common = None
            for df in histories.values():
                if df is None or len(df) == 0:
                    continue
                if common is None or df.index[-1] > common:
                    common = df.index[-1]
            asof = common
        if asof is None:
            return pd.DataFrame(columns=["ticker", "score"])

        asof_date = asof.date() if hasattr(asof, "date") else asof
        asof_ts = pd.Timestamp(asof_date)

        # Parent-side prep — PIT-correct mcap (zen review 2026-05-10:
        # static main()-precomputed mcap dict would violate PIT correctness
        # because mcaps drift over the 6yr audit window).
        args_list: list[tuple] = []
        for ticker, history in histories.items():
            if history is None or history.empty:
                continue
            sliced = history[history.index <= asof_ts]
            if sliced.empty:
                continue
            controls = _compute_equity_controls(sliced)
            if controls is None:
                continue
            close = _close_at(sliced, asof_ts)
            if close is None or close <= 0:
                continue
            shares = self._shares.get(ticker, asof_ts) if self._shares is not None else None
            mcap = (close * shares) if shares else self._mcap_lookup.get(ticker)
            if not mcap or mcap <= 0:
                continue
            args_list.append((ticker, asof_date, float(mcap), controls))

        if not args_list:
            return pd.DataFrame(columns=["ticker", "score"])

        # Dispatch heavy work — pool if configured, otherwise serial via the
        # SAME _score_one_ticker function (per zen review 2026-05-10: serial
        # path duplicating worker logic risks silent drift if either is
        # changed independently). For the serial branch we populate the
        # module-level worker globals from this scorer's parent state so
        # _score_one_ticker behaves identically.
        self._ensure_executor()
        if self._executor is not None:
            rows = list(self._executor.map(_score_one_ticker, args_list, chunksize=50))
        else:
            global _worker_store, _worker_cache  # noqa: PLW0603
            _worker_store = self._store
            _worker_cache = self._cache
            rows = [_score_one_ticker(args) for args in args_list]

        if not rows:
            return pd.DataFrame(columns=["ticker", "score"])

        features = pd.DataFrame(rows)
        features["score"] = score_opportunistic_form4(features)
        return features[["ticker", "score"]].dropna(subset=["score"])


# -------------------------------------------------------------------------
# Compound-specific code below this line.
# -------------------------------------------------------------------------


def _smd_loader(ticker: str) -> pd.DataFrame | None:
    """Load iVolatility smd parquet for a ticker; mirrors precheck script."""
    p = _SMD_DIR / f"{ticker.upper()}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if "tradeDate" in df.columns:
        df["date"] = pd.to_datetime(df["tradeDate"])
    return df


def _infer_asof_from_histories(histories) -> pd.Timestamp | None:
    """Mirror Form-4 inner adapter fallback when config omits asof."""
    common = None
    for df in histories.values():
        if df is None or len(df) == 0:
            continue
        if common is None or df.index[-1] > common:
            common = df.index[-1]
    return common


class _CompoundInsiderPcScorer:
    """Per-asof equal-weight z-score average of Form-4 + P/C abnormal scores.

    Strict intersection of tickers (memo Section 3.1): a ticker enters the
    compound only if BOTH components produce a finite score for that asof.
    """

    # Class-level default; covers __new__-bypass paths in unit tests.
    _pc_panel: pd.Series | None = None

    def __init__(
        self,
        *,
        form4_store: Form4PITStore,
        classifier_cache: ClassifierCache,
        shares_store,
        smd_loader,
        mcap_lookup: dict[str, float] | None = None,
    ):
        # Composition over inheritance — Form-4 logic lives in the duplicated
        # class above (isolated from upstream Form-4 driver).
        self._form4_inner = _OpportunisticForm4Scorer(
            form4_store=form4_store,
            classifier_cache=classifier_cache,
            mcap_lookup=mcap_lookup,
            shares_store=shares_store,
        )
        self._smd_loader = smd_loader
        # Pre-built P/C panel populated by prebuild_pc_panel(); when present,
        # __call__ does an O(1) lookup instead of rebuilding per rebalance.
        self._pc_panel: pd.Series | None = None

    def prebuild_pc_panel(
        self,
        universe: list[str],
        asof_dates: list[date],
        history_store: HistoryStore,
    ) -> None:
        """Pre-build P/C feature frame + scores for all rebalance asofs.

        `build_feature_frame` already supports multi-asof input efficiently
        (`_prepare_ticker_history` is called ONCE per ticker, computing the
        rolling abnormal_pcr series; the inner loop only slices per asof).
        Per-rebalance invocation discards this amortization — recomputing
        the rolling series ~85 times per ticker in a 6yr window. Pre-building
        once cuts that to 1x.

        Numerical equivalence requires post-filtering rows to match the
        engine's per-asof universe: BacktestEngine._build_histories
        (engine.py:295-301) drops tickers where
        `len(truncate_to(ticker, day)) < MIN_BARS_REQUIRED` (default 220).
        Without that filter, the prebuild includes "phantom" rows for
        recent-IPO tickers (when their iVolatility SMD covers the asof but
        yfinance OHLCV history is too short). Even a single phantom in the
        per-asof OLS shifts coefficients → residuals shift for ALL scored
        tickers at that asof.

        MUST be called BEFORE BacktestEngine.run with `asof_dates` matching
        the engine's rebalance schedule (see HistoryStore.benchmark_calendar
        + [phase_offset::stride] slicing in alphalens_research/backtest/engine.py:230).
        """
        from alphalens_research.backtest.engine import BacktestEngine as _Engine

        asof_strs = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in asof_dates]
        pc_features = build_feature_frame(
            smd_loader=self._smd_loader,
            universe=universe,
            asof_dates=asof_strs,
        )
        if pc_features.empty:
            self._pc_panel = pd.Series(dtype=float, name="score")
            return

        # Mirror engine's per-asof history filter so OLS input matches the
        # per-rebalance fallback exactly (pre-reg-blessed fixtures stay
        # byte-equivalent). Default min_bars is engine.MIN_BARS_REQUIRED=220
        # unless the scorer declares its own.
        min_bars = getattr(self, "MIN_BARS_REQUIRED", _Engine.MIN_BARS_REQUIRED)
        allowed_pairs: set[tuple[str, str]] = set()
        for asof_d, asof_str in zip(asof_dates, asof_strs, strict=True):
            for ticker in universe:
                if len(history_store.truncate_to(ticker, asof_d)) >= min_bars:
                    allowed_pairs.add((asof_str, ticker.upper()))
        keep_mask = pd.Series(
            [
                (row.asof, row.ticker) in allowed_pairs
                for row in pc_features.itertuples(index=False)
            ],
            index=pc_features.index,
        )
        pc_features = pc_features.loc[keep_mask]
        if pc_features.empty:
            self._pc_panel = pd.Series(dtype=float, name="score")
            return

        pc_features = pc_features.assign(score=score_pc_abnormal_residual(pc_features)).dropna(
            subset=["score"]
        )
        # Normalize asof to date for stable lookup keys.
        pc_features["asof"] = pd.to_datetime(pc_features["asof"]).dt.date
        self._pc_panel = pc_features.set_index(["asof", "ticker"])["score"].sort_index()

    def __call__(self, histories, config=None) -> pd.DataFrame:  # noqa: PLR0911
        # Eight defensive early-returns: missing asof, empty form4, panel
        # miss, empty pc_features, empty pc_df, empty pc_series, empty
        # compound, normal path. Refactoring to a single return obscures
        # which branch handled which empty case; left as-is.
        cfg = dict(config or {})
        asof = cfg.get("asof")
        if asof is None:
            asof = _infer_asof_from_histories(histories)
            if asof is not None:
                # Inject so the inner Form-4 adapter does not repeat the same
                # O(N-tickers) fallback loop independently.
                cfg["asof"] = asof
        if asof is None:
            return pd.DataFrame(columns=["ticker", "score"])
        asof_date = asof.date() if hasattr(asof, "date") else asof

        # 1) Form-4 scores via the duplicated adapter.
        form4_df = self._form4_inner(histories, cfg)
        if form4_df.empty:
            return pd.DataFrame(columns=["ticker", "score"])

        # 2) P/C scores: lookup pre-built panel if present, else fall back to
        #    per-asof build for ad-hoc invocations (e.g. precheck script).
        if self._pc_panel is not None:
            try:
                pc_series = self._pc_panel.loc[asof_date].astype(float)
            except KeyError:
                return pd.DataFrame(columns=["ticker", "score"])
        else:
            universe = list(histories.keys())
            pc_features = build_feature_frame(
                smd_loader=self._smd_loader,
                universe=universe,
                asof_dates=[asof_date.strftime("%Y-%m-%d")],
            )
            if pc_features.empty:
                return pd.DataFrame(columns=["ticker", "score"])
            pc_features = pc_features.assign(score=score_pc_abnormal_residual(pc_features))
            pc_df = pc_features[["ticker", "score"]].dropna(subset=["score"])
            if pc_df.empty:
                return pd.DataFrame(columns=["ticker", "score"])
            pc_series = pc_df.set_index("ticker")["score"].astype(float)

        if pc_series.empty:
            return pd.DataFrame(columns=["ticker", "score"])

        # 3) Strict-intersection equal-weight z-score average.
        f4_series = form4_df.set_index("ticker")["score"].astype(float)
        compound = compound_score_from_components(f4_series, pc_series)
        if compound.empty:
            return pd.DataFrame(columns=["ticker", "score"])
        # compound's index inherits name 'ticker' from set_index above, so
        # reset_index emits ['ticker', 'score'] directly.
        return compound.reset_index()

    def shutdown(self) -> None:
        """Release Form-4 worker pool. Call after engine.run completes."""
        self._form4_inner.shutdown()


def _monthly_asofs(start: date, end: date, *, day_of_month: int = 21) -> list[pd.Timestamp]:
    """Monthly rebalance calendar; mirrors precheck script."""
    asofs: list[pd.Timestamp] = []
    cur = pd.Timestamp(start.year, start.month, day_of_month)
    while cur.date() <= end:
        while cur.weekday() > 4:
            cur += pd.Timedelta(days=1)
        if cur.date() <= end:
            asofs.append(cur)
        if cur.month == 12:
            cur = pd.Timestamp(cur.year + 1, 1, day_of_month)
        else:
            cur = pd.Timestamp(cur.year, cur.month + 1, day_of_month)
    return asofs


def _run_precheck(
    *,
    histories: dict[str, pd.DataFrame],
    form4_store: Form4PITStore,
    classifier_cache: ClassifierCache,
    share_store,
) -> bool:
    """Reproducibility guard: re-confirm IS-window signal independence.

    Hardcoded IS window 2014-2017 (NEVER reads --is-start / --is-end). On
    failure logs the verdict and returns False so caller can abort before
    burning audit compute.
    """
    from alphalens_research.attribution.signal_independence import (
        classify_independence,
        pairwise_rank_ic_correlation,
    )

    asofs = _monthly_asofs(_PRECHECK_IS_START, _PRECHECK_IS_END)
    logger.info(
        "Pre-screen: IS %s..%s, %d monthly asofs",
        _PRECHECK_IS_START,
        _PRECHECK_IS_END,
        len(asofs),
    )

    # P/C panel — single batched build_feature_frame call.
    pc_features = build_feature_frame(
        smd_loader=_smd_loader,
        universe=list(histories.keys()),
        asof_dates=[a.strftime("%Y-%m-%d") for a in asofs],
    )
    if pc_features.empty:
        logger.error("Pre-screen aborted: empty P/C feature frame on IS")
        return False
    pc_features = pc_features.assign(score=score_pc_abnormal_residual(pc_features)).dropna(
        subset=["score"]
    )
    pc_features["asof"] = pd.to_datetime(pc_features["asof"]).dt.normalize()
    pc_panel = pc_features[["asof", "ticker", "score"]].copy()

    # Form-4 panel — per-asof loop via the duplicated adapter.
    insider_scorer = _OpportunisticForm4Scorer(
        form4_store=form4_store,
        classifier_cache=classifier_cache,
        shares_store=share_store,
    )
    insider_rows: list[pd.DataFrame] = []
    for asof_ts in asofs:
        df = insider_scorer(histories, config={"asof": asof_ts})
        if df.empty:
            continue
        df = df.assign(asof=asof_ts.normalize())
        insider_rows.append(df[["asof", "ticker", "score"]])
    if not insider_rows:
        logger.error("Pre-screen aborted: no insider scores produced on IS")
        return False
    insider_panel = pd.concat(insider_rows, ignore_index=True)

    try:
        result = pairwise_rank_ic_correlation(insider_panel, pc_panel)
    except ValueError as e:
        logger.error("Pre-screen failed to compute Spearman ρ: %s", e)
        return False

    verdict = classify_independence(result)
    logger.info(
        "Pre-screen result: mean ρ=%+.4f t=%+.3f n_asofs=%d/%d -> %s",
        result.mean_rho,
        result.t_stat,
        result.n_asofs_with_valid_rho,
        result.n_asofs_total,
        verdict.classification,
    )
    if not verdict.proceed:
        logger.error("Pre-screen verdict: %s -- %s", verdict.classification, verdict.rationale)
        return False
    logger.info("Pre-screen PROCEED: %s", verdict.rationale)
    return True


def assess(
    report,
    factors,
    rebalance_stride,
    cost_bps,
    bench_rets_daily,
    *,
    history_store,
    benchmark,
    end_date,
) -> dict:
    """Daily-cadence Carhart attribution; mirrors Form-4 driver lock v2."""
    rets_daily = daily_continuous_returns(
        report.rebalance_results,
        history_store,
        calendar_ticker=benchmark,
        end_date=end_date,
    )
    if rets_daily.empty:
        return {"n": 0}

    avg_turnover = turnover_pct(r.top_n_tickers for r in report.rebalance_results)
    rebalances_per_year = 252 / max(1, rebalance_stride)
    cost_model = RealisticCostModel(adverse_selection_bps=5.0)
    drag_per_rebal_bps = cost_model.primary_period_drag_bps(cost_bps, avg_turnover)
    drag_ann = drag_per_rebal_bps * rebalances_per_year / 10_000.0
    drag_per_day = drag_ann / 252.0
    rets_net_daily = rets_daily - drag_per_day

    sharpe_gross = sharpe(rets_daily.tolist(), periods_per_year=252)
    sharpe_net = sharpe(rets_net_daily.tolist(), periods_per_year=252)

    res4 = run_regression(
        rets_daily,
        factors[[*_CARHART_FACTORS, "RF"]],
        _CARHART_FACTORS,
        hac_maxlags=_HAC_MAXLAGS_LOCK,
        periods_per_year=252,
    )

    bench_aligned = bench_rets_daily.reindex(rets_daily.index).dropna()
    excess_per_day = (rets_daily.reindex(bench_aligned.index) - bench_aligned).mean()
    excess_ann = float(excess_per_day * 252) if not np.isnan(excess_per_day) else float("nan")

    mean_top_n = float(
        sum(len(r.top_n_tickers) for r in report.rebalance_results)
        / max(1, len(report.rebalance_results))
    )
    return {
        "n": len(rets_daily),
        "mean_top_n": mean_top_n,
        "turnover_per_rebal": avg_turnover,
        "sharpe_gross": sharpe_gross,
        "sharpe_net": sharpe_net,
        "alpha_gross_4f": float(res4.alpha_annualized),
        "t_4f": float(res4.alpha_tstat),
        "beta_smb": float(res4.betas.get("SMB", 0.0)),
        "beta_hml": float(res4.betas.get("HML", 0.0)),
        "beta_mom": float(res4.betas.get("Mom", 0.0)),
        "cost_drag_ann": drag_ann,
        "alpha_net_4f": float(res4.alpha_annualized) - drag_ann,
        "excess_vs_bench_ann": excess_ann,
        "excess_vs_bench_net": excess_ann - drag_ann,
        "rets_daily": rets_daily,
    }


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--top-n", type=int, default=200)
    ap.add_argument("--holding", type=int, default=21)
    ap.add_argument("--rebalance-stride", type=int, default=_REBALANCE_STRIDE_LOCK)
    ap.add_argument("--benchmark", default="IWM")
    ap.add_argument("--cost-half-spreads", nargs="+", type=float, default=[5.0])
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("docs/research/insider_pc_compound_audit_run.md"),
    )
    ap.add_argument("--is-start", type=date.fromisoformat, default=date(2018, 1, 1))
    ap.add_argument("--is-end", type=date.fromisoformat, default=date(2023, 12, 31))
    ap.add_argument("--phase-offset", type=int, default=0)
    ap.add_argument("--parquet-root", type=Path, default=_FORM4_PARQUET_DEFAULT)
    ap.add_argument(
        "--universe-mode",
        choices=["R2000", "R3000"],
        default="R2000",
        help="R2000 PIT primary (per pre-reg).",
    )
    ap.add_argument(
        "--universe-size-cap",
        type=int,
        default=None,
        help="Optional ticker cap for smoke runs. MUST exceed P/C MIN_ASOF_TICKERS=50 "
        "to avoid silent empty-asof FALSE-GREEN; use >=100 for sanity tests.",
    )
    ap.add_argument(
        "--dump-returns",
        type=Path,
        default=None,
        help="Optional parquet path; writes daily portfolio returns for downstream "
        "block-bootstrap and cyclicality diagnostics (memo Section 7 A4).",
    )
    ap.add_argument(
        "--skip-precheck",
        action="store_true",
        help="Skip the IS 2014-2017 signal-independence reproducibility guard. "
        "Use on runpod when the guard has already cleared locally.",
    )
    return ap


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Pre-reg memo §3.1 + §4 lock rebalance stride at 21d (monthly). The
    # CLI accepts --rebalance-stride for testing /smoke pathways, but the
    # locked value must not be overridden at audit time. The generic
    # `alphalens audit` driver passed `--rebalance-stride 5` (intended as
    # "5 phases") which the experiment script silently accepted as 5-day
    # cadence, deviating from memo before this guard. Discovery cost: ~27
    # min wasted pod compute (2026-05-11 audit re-launch postmortem).
    if args.rebalance_stride != _REBALANCE_STRIDE_LOCK:
        sys.stderr.write(
            f"PRE-REG VIOLATION: --rebalance-stride={args.rebalance_stride} "
            f"overrides locked memo §3.1 value {_REBALANCE_STRIDE_LOCK}. "
            "Use scripts/run_insider_pc_compound_audit.py for multi-phase "
            "audits (N_PHASES separate from rebalance_stride) or remove "
            "the override to use the locked 21d monthly cadence.\n"
        )
        return 9

    _check_backfill_exists(args.parquet_root)
    _verify_component_hashes()

    if args.universe_mode == "R3000":
        sys.stderr.write(
            "ERROR: R3000 mode requires separately-built PIT yaml snapshots. "
            "Build R3000 yamls via scripts/build_pit_universe.py with custom "
            "--cap-min/--cap-max before running R3000 diagnostic.\n"
        )
        return 4

    logger.info(
        "experiment insider_pc_compound | universe=%s | %s..%s | phase_offset=%d",
        args.universe_mode,
        args.is_start,
        args.is_end,
        args.phase_offset,
    )

    universe = load_universe_union(args.is_start, args.is_end)
    if not universe:
        sys.stderr.write(f"ERROR: empty PIT universe for {args.is_start}..{args.is_end}.\n")
        return 5
    if args.universe_size_cap is not None:
        universe = universe[: args.universe_size_cap]
        logger.info("Universe capped to %d tickers (smoke mode)", len(universe))
    logger.info("PIT universe union: %d unique tickers across window", len(universe))

    histories = load_cached_histories([*universe, args.benchmark], _PRICES_DIR)
    if args.benchmark not in histories or histories[args.benchmark].empty:
        sys.stderr.write(f"ERROR: benchmark {args.benchmark} OHLCV missing from {_PRICES_DIR}.\n")
        return 6
    history_store = HistoryStore(histories)

    _liab_store, share_store = make_production_stores()
    tcm_path = (
        REPO_ROOT / "alphalens_research" / "data" / "alt_data" / "data" / "ticker_cik_map.yaml"
    )
    cik_resolver = TickerCikMap.load(tcm_path)

    form4_store = Form4PITStore(
        parquet_root=args.parquet_root,
        ticker_cik_resolver=cik_resolver,
        delisting_events=None,
    )
    classifier_cache = ClassifierCache(form4_store)

    # Phase 0 in a multi-phase audit runs the precheck once; phases 1+ skip
    # it because phase_robust_backtesting spawns one subprocess per phase
    # and re-running the ~30min IS panel build 20+ times burns ~10h with no
    # additional information (data inputs are identical across phases).
    # Standalone invocations default to phase_offset=0, so the guard still
    # fires for ad-hoc runs unless --skip-precheck is set.
    skip_precheck_effective = args.skip_precheck or args.phase_offset != 0
    if not skip_precheck_effective:
        # Reproducibility guard runs on the FULL universe over the hardcoded
        # IS window 2014-2017 — independent of --is-start / --is-end.
        precheck_universe = load_universe_union(_PRECHECK_IS_START, _PRECHECK_IS_END)
        if not precheck_universe:
            sys.stderr.write(
                "ERROR: empty PIT universe for pre-screen IS window "
                f"{_PRECHECK_IS_START}..{_PRECHECK_IS_END}.\n"
            )
            return 7
        precheck_histories = load_cached_histories(precheck_universe, _PRICES_DIR)
        precheck_histories = {
            t: h for t, h in precheck_histories.items() if h is not None and not h.empty
        }
        ok = _run_precheck(
            histories=precheck_histories,
            form4_store=form4_store,
            classifier_cache=classifier_cache,
            share_store=share_store,
        )
        if not ok:
            sys.stderr.write(
                "Pre-screen FAIL: signal-independence reproducibility guard tripped. "
                "Re-run scripts/precheck_insider_pc_compound_independence.py to "
                "investigate before consuming the audit holdout.\n"
            )
            return 8
    elif args.phase_offset != 0:
        logger.info(
            "phase_offset=%d != 0; skipping precheck (already ran on phase 0)",
            args.phase_offset,
        )
    else:
        logger.info("--skip-precheck set; not re-running IS reproducibility guard")

    scorer = _CompoundInsiderPcScorer(
        form4_store=form4_store,
        classifier_cache=classifier_cache,
        shares_store=share_store,
        smd_loader=_smd_loader,
    )

    # Pre-build P/C feature panel for all rebalance asofs. Mirror engine's
    # exact rebalance calendar (engine.py:230-236) so panel keys align with
    # what the engine will actually request during run().
    trading_calendar = HistoryStore.benchmark_calendar(
        history_store, args.benchmark, args.is_start, args.is_end
    )
    sliced_calendar = trading_calendar[args.phase_offset :: args.rebalance_stride]
    rebalance_dates = [ts.date() if hasattr(ts, "date") else ts for ts in sliced_calendar]
    logger.info("Pre-building P/C panel for %d rebalance asofs", len(rebalance_dates))
    scorer.prebuild_pc_panel(
        universe=universe, asof_dates=rebalance_dates, history_store=history_store
    )

    engine = BacktestEngine(
        history_store=history_store,
        scorer=scorer,
        scorer_config={},
        holding_period=args.holding,
        top_n=args.top_n,
        benchmark=args.benchmark,
        screener_tickers=universe,
        weighting="equal",
        rebalance_stride=args.rebalance_stride,
        phase_offset=args.phase_offset,
    )
    logger.info(
        "Engine: stride=%d holding=%d top_n=%d phase_offset=%d benchmark=%s",
        args.rebalance_stride,
        args.holding,
        args.top_n,
        args.phase_offset,
        args.benchmark,
    )
    try:
        report = engine.run(args.is_start, args.is_end)
    finally:
        # Always release Form-4 worker pool — even on engine.run() exception
        # — so worker processes don't leak as zombies (zen review 2026-05-10).
        scorer.shutdown()

    carhart = load_carhart_daily(start=args.is_start, end=args.is_end)
    bench_rets = benchmark_returns(history_store, args.benchmark, args.is_start, args.is_end)

    period_label = f"compound {args.is_start.year}-{args.is_end.year}"
    sections: list[str] = [
        f"# insider_pc_compound_2026_05_10 — {period_label}",
        "",
        "Long-only top-decile equal-weighted compound (insider_form4 x P/C abnormal),",
        "per-asof equal-weight z-score average on strict-intersection tickers.",
        f"Universe: {args.universe_mode} PIT, {len(universe)} ticker union over window.",
        f"Rebalance stride: {args.rebalance_stride}d, holding: {args.holding}d.",
        f"Phase offset: {args.phase_offset}",
        f"Bonferroni threshold: |t| >= {_BONFERRONI_THRESHOLD} (n=34).",
        "",
    ]

    all_rows: list[dict] = []
    for cost_bps in args.cost_half_spreads:
        stats = assess(
            report,
            carhart,
            args.rebalance_stride,
            cost_bps,
            bench_rets,
            history_store=history_store,
            benchmark=args.benchmark,
            end_date=args.is_end,
        )
        stats["period"] = period_label
        stats["cost_bps"] = cost_bps
        all_rows.append(stats)
        if stats.get("n", 0) > 0:
            logger.info(
                "%s | cost=%.0fbps | n=%d topN=%.1f turn=%.1f%% | "
                "Sh gross=%.2f net=%.2f | excess gross=%.1f%% net=%.1f%% | "
                "α 4F=%.1f%% t=%.2f",
                period_label,
                cost_bps,
                stats["n"],
                stats["mean_top_n"],
                stats["turnover_per_rebal"] * 100,
                stats["sharpe_gross"],
                stats["sharpe_net"],
                stats["excess_vs_bench_ann"] * 100,
                stats["excess_vs_bench_net"] * 100,
                stats["alpha_gross_4f"] * 100,
                stats["t_4f"],
            )

    sections.append("## Results")
    sections.append("")
    sections.append(
        "| Period | cost | n | mean topN | turn | Sharpe gross | Sharpe net | "
        "excess gross | excess net | α 4F | t (4F) | β_SMB | β_HML | β_MOM |"
    )
    sections.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in all_rows:
        if r.get("n", 0) == 0:
            continue
        sections.append(
            "| {p} | {cb:.0f}bp | {n} | {tn:.1f} | {tr:.1f}% | "
            "{sg:.2f} | {sn:.2f} | {eg:+.1f}% | {en:+.1f}% | {a4:+.1f}% | "
            "{t4:+.2f} | {bsmb:+.2f} | {bhml:+.2f} | {bmom:+.2f} |".format(
                p=r["period"],
                cb=r["cost_bps"],
                n=r["n"],
                tn=r["mean_top_n"],
                tr=r["turnover_per_rebal"] * 100,
                sg=r["sharpe_gross"],
                sn=r["sharpe_net"],
                eg=r["excess_vs_bench_ann"] * 100,
                en=r["excess_vs_bench_net"] * 100,
                a4=r["alpha_gross_4f"] * 100,
                t4=r["t_4f"],
                bsmb=r["beta_smb"],
                bhml=r["beta_hml"],
                bmom=r["beta_mom"],
            )
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(sections) + "\n", encoding="utf-8")
    logger.info("Wrote %s", args.out)

    if args.dump_returns is not None:
        rets_daily = next(
            (r["rets_daily"] for r in all_rows if "rets_daily" in r),
            None,
        )
        if rets_daily is None or rets_daily.empty:
            logger.warning("No daily returns to dump (assess returned n=0)")
        else:
            args.dump_returns.parent.mkdir(parents=True, exist_ok=True)
            rets_daily.rename("portfolio_daily").to_frame().to_parquet(args.dump_returns)
            logger.info(
                "Dumped %d daily portfolio return obs to %s",
                len(rets_daily),
                args.dump_returns,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
