"""Golden master characterization tests for insider_pc_compound audit pipeline.

Locks the numerical output of the full audit pipeline (Form-4 + P/C compound
scoring + Carhart 4F regression) on a small fixture so that performance
refactors can be verified byte-equivalent. Pre-reg LOCKED formula (memo
`docs/research/insider_pc_compound_design_2026_05_10.md`); ANY drift between
baseline and refactored output invalidates the audit.

Fixture window (fixed; do NOT regenerate without explicit pre-reg amendment):
    --is-start 2019-01-01 --is-end 2019-06-30
    --universe-size-cap 300  (cap=100 over 3mo gave 0 strict-intersection
                              tickers empirically — first 100 R2000 names
                              alphabetically have spotty iVolatility SMD
                              coverage even post-2018 cliff. Cap=300 over
                              6mo matches the pod smoke that succeeded
                              with 6/6 rebalances and ~85-107 scored
                              tickers per asof.)
    --phase-offset 0 --rebalance-stride 21 --skip-precheck

Three golden master artefacts (`tests/fixtures/audit_equivalence/`):
    compound_scores_per_asof.parquet  — long-format [asof, ticker, score]
    daily_returns.parquet              — pd.Series indexed by date
    assess_metrics.json                — scalar metrics from assess()

Regenerate (only after a pre-reg amendment):
    .venv/bin/python -m tests.test_compound_audit_equivalence --regenerate-fixtures
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "audit_equivalence"
_FIXTURE_CONFIG = {
    "is_start": "2019-01-01",
    "is_end": "2019-06-30",
    "universe_size_cap": 300,
    "phase_offset": 0,
    "rebalance_stride": 21,
    "holding": 21,
    "top_n": 200,
    "benchmark": "IWM",
    "cost_bps": 5.0,
}

_SCALAR_METRIC_KEYS = (
    "n",
    "mean_top_n",
    "turnover_per_rebal",
    "sharpe_gross",
    "sharpe_net",
    "alpha_gross_4f",
    "t_4f",
    "beta_smb",
    "beta_hml",
    "beta_mom",
    "cost_drag_ann",
    "alpha_net_4f",
    "excess_vs_bench_ann",
    "excess_vs_bench_net",
)


class _RecordingScorer:
    """Wraps a scorer; records every per-asof compound output for later inspection.

    The BacktestEngine only retains top-N picks per snapshot — not the full
    scored frame. To capture full per-ticker compound scores for the golden
    master, we wrap the scorer and stash each call's output keyed by asof.
    """

    def __init__(self, inner):
        self._inner = inner
        self.outputs: dict[date, pd.DataFrame] = {}

    def __call__(self, histories, config=None):
        result = self._inner(histories, config)
        cfg = dict(config or {})
        asof = cfg.get("asof")
        if asof is None:
            # Mirror the inner adapter's fallback for asof inference.
            common = None
            for df in histories.values():
                if df is None or len(df) == 0:
                    continue
                if common is None or df.index[-1] > common:
                    common = df.index[-1]
            asof = common
        if asof is not None:
            asof_d = asof.date() if hasattr(asof, "date") else asof
            self.outputs[asof_d] = result.copy()
        return result


def _has_required_data() -> bool:
    """Skip-guard: the fixture pipeline needs Form-4 + iVol SMD + PIT + factors."""
    home = Path.home()
    required = [
        home / ".alphalens" / "form4_parquet",
        home / ".alphalens" / "ivolatility_smd",
        home / ".alphalens" / "pit_universe",
        home / ".alphalens" / "prices",
        home / ".alphalens" / "factors",
    ]
    return all(p.exists() and any(p.iterdir()) for p in required)


def _run_baseline_pipeline() -> tuple[pd.DataFrame, pd.Series, dict[str, float]]:
    """Reproduce the experiment script's main() pipeline on the fixture window.

    Returns:
        scores_long: long-format DataFrame [asof, ticker, score] of compound
                     scorer outputs across all rebalance asofs.
        daily_returns: portfolio daily continuous-holding returns Series.
        scalar_metrics: subset of assess() return dict (only floats; rets_daily
                        Series stripped to keep JSON-serializable).
    """
    import scripts.experiment_insider_pc_compound as exp
    from alphalens_research.attribution.cost_model import RealisticCostModel  # noqa: F401
    from alphalens_research.backtest.engine import BacktestEngine
    from alphalens_research.data.alt_data.pit_universe_loader import load_universe_union
    from alphalens_research.data.alt_data.ticker_cik_map import TickerCikMap
    from alphalens_research.data.alt_data.yfinance_cache import load_cached_histories
    from alphalens_research.data.factors import load_carhart_daily
    from alphalens_research.data.store.form4_pit import Form4PITStore
    from alphalens_research.data.store.history import HistoryStore
    from alphalens_research.screeners.distress_credit.features import make_production_stores

    cfg = _FIXTURE_CONFIG
    is_start = date.fromisoformat(cfg["is_start"])
    is_end = date.fromisoformat(cfg["is_end"])

    universe = load_universe_union(is_start, is_end)
    if not universe:
        raise RuntimeError("Empty PIT universe — cannot generate fixture")
    universe = universe[: cfg["universe_size_cap"]]

    histories = load_cached_histories([*universe, cfg["benchmark"]], exp._PRICES_DIR)
    history_store = HistoryStore(histories)

    _liab_store, share_store = make_production_stores()
    tcm_path = (
        REPO_ROOT / "alphalens_research" / "data" / "alt_data" / "data" / "ticker_cik_map.yaml"
    )
    cik_resolver = TickerCikMap.load(tcm_path)

    form4_store = Form4PITStore(
        parquet_root=Path.home() / ".alphalens" / "form4_parquet",
        ticker_cik_resolver=cik_resolver,
        delisting_events=None,
    )
    classifier_cache = exp.ClassifierCache(form4_store)

    inner_scorer = exp._CompoundInsiderPcScorer(
        form4_store=form4_store,
        classifier_cache=classifier_cache,
        shares_store=share_store,
        smd_loader=exp._smd_loader,
    )

    # Exercise the production path: pre-build P/C panel matching engine's
    # exact rebalance calendar (engine.py:230-236). Numerical equivalence
    # requires the lookup path produce identical (asof, ticker, score)
    # triples as the per-rebalance fallback.
    # Set ALPHALENS_TEST_DISABLE_PREBUILD=1 to bypass for re-blessing the
    # baseline (per-rebalance fallback) golden master only.
    import os as _os

    if not _os.environ.get("ALPHALENS_TEST_DISABLE_PREBUILD"):
        trading_calendar = HistoryStore.benchmark_calendar(
            history_store, cfg["benchmark"], is_start, is_end
        )
        sliced_calendar = trading_calendar[cfg["phase_offset"] :: cfg["rebalance_stride"]]
        rebalance_dates = [ts.date() if hasattr(ts, "date") else ts for ts in sliced_calendar]
        inner_scorer.prebuild_pc_panel(
            universe=universe, asof_dates=rebalance_dates, history_store=history_store
        )

    recording_scorer = _RecordingScorer(inner_scorer)

    engine = BacktestEngine(
        history_store=history_store,
        scorer=recording_scorer,
        scorer_config={},
        holding_period=cfg["holding"],
        top_n=cfg["top_n"],
        benchmark=cfg["benchmark"],
        screener_tickers=universe,
        weighting="equal",
        rebalance_stride=cfg["rebalance_stride"],
        phase_offset=cfg["phase_offset"],
    )
    report = engine.run(is_start, is_end)

    carhart = load_carhart_daily(start=is_start, end=is_end)
    bench_rets = exp.benchmark_returns(history_store, cfg["benchmark"], is_start, is_end)
    metrics = exp.assess(
        report,
        carhart,
        cfg["rebalance_stride"],
        cfg["cost_bps"],
        bench_rets,
        history_store=history_store,
        benchmark=cfg["benchmark"],
        end_date=is_end,
    )

    # Long-format compound scores — sorted deterministically for byte-equivalence.
    rows = []
    for asof_d, df in recording_scorer.outputs.items():
        if df.empty:
            continue
        sub = df.copy()
        sub["asof"] = asof_d
        rows.append(sub[["asof", "ticker", "score"]])
    if not rows:
        scores_long = pd.DataFrame(columns=["asof", "ticker", "score"])
    else:
        scores_long = (
            pd.concat(rows, ignore_index=True)
            .sort_values(["asof", "ticker"], kind="mergesort")
            .reset_index(drop=True)
        )

    daily_returns = metrics.get("rets_daily")
    if daily_returns is None or not isinstance(daily_returns, pd.Series):
        daily_returns = pd.Series(dtype=float, name="portfolio_daily")
    daily_returns = daily_returns.rename("portfolio_daily")

    scalar_metrics = {k: float(metrics[k]) for k in _SCALAR_METRIC_KEYS if k in metrics}

    return scores_long, daily_returns, scalar_metrics


def _regenerate_fixtures() -> None:
    """Bless the current pipeline output as the new golden master.

    The golden master MUST be blessed from the per-rebalance fallback path,
    not the optimised prebuild path — otherwise the test becomes refactor-
    vs-refactor and silently approves any drift the refactor introduces.
    We enforce the flag here so a maintainer cannot forget to set the env
    var when calling --regenerate-fixtures.
    """
    import os as _os

    _os.environ["ALPHALENS_TEST_DISABLE_PREBUILD"] = "1"

    if not _has_required_data():
        sys.stderr.write("ERROR: required data missing under ~/.alphalens/. Cannot regenerate.\n")
        sys.exit(2)
    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    print(
        "Running baseline pipeline with ALPHALENS_TEST_DISABLE_PREBUILD=1 "
        "(per-rebalance fallback path; ~2 min)...",
        flush=True,
    )
    scores, returns, metrics = _run_baseline_pipeline()

    scores.to_parquet(_FIXTURE_DIR / "compound_scores_per_asof.parquet")
    returns.to_frame().to_parquet(_FIXTURE_DIR / "daily_returns.parquet")
    (_FIXTURE_DIR / "assess_metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n"
    )
    (_FIXTURE_DIR / "fixture_config.json").write_text(
        json.dumps(_FIXTURE_CONFIG, indent=2, sort_keys=True) + "\n"
    )
    print(
        f"Wrote 4 fixture files to {_FIXTURE_DIR.relative_to(REPO_ROOT)}:\n"
        f"  - compound_scores_per_asof.parquet ({len(scores)} rows)\n"
        f"  - daily_returns.parquet ({len(returns)} rows)\n"
        f"  - assess_metrics.json ({len(metrics)} scalars)\n"
        f"  - fixture_config.json"
    )


def _fixtures_present() -> bool:
    return all(
        (_FIXTURE_DIR / fname).exists()
        for fname in (
            "compound_scores_per_asof.parquet",
            "daily_returns.parquet",
            "assess_metrics.json",
            "fixture_config.json",
        )
    )


@unittest.skipUnless(_has_required_data(), "Required ~/.alphalens data not available")
@unittest.skipUnless(_fixtures_present(), "Golden master fixtures not yet blessed")
class TestCompoundAuditEquivalence(unittest.TestCase):
    _scores: pd.DataFrame
    _returns: pd.Series
    _metrics: dict[str, float]
    _golden_scores: pd.DataFrame
    _golden_returns: pd.Series
    _golden_metrics: dict[str, Any]

    @classmethod
    def setUpClass(cls):
        cls._scores, cls._returns, cls._metrics = _run_baseline_pipeline()
        cls._golden_scores = pd.read_parquet(_FIXTURE_DIR / "compound_scores_per_asof.parquet")
        cls._golden_returns = pd.read_parquet(_FIXTURE_DIR / "daily_returns.parquet")[
            "portfolio_daily"
        ]
        cls._golden_metrics = json.loads((_FIXTURE_DIR / "assess_metrics.json").read_text())

    def test_compound_scores_match_golden(self):
        cur = self._scores.sort_values(["asof", "ticker"], kind="mergesort").reset_index(drop=True)
        gold = self._golden_scores.sort_values(["asof", "ticker"], kind="mergesort").reset_index(
            drop=True
        )
        self.assertEqual(len(cur), len(gold), "row count mismatch")
        self.assertEqual(list(cur["asof"]), list(gold["asof"]), "asof column mismatch")
        self.assertEqual(list(cur["ticker"]), list(gold["ticker"]), "ticker column mismatch")
        np.testing.assert_array_equal(
            cur["score"].to_numpy(),
            gold["score"].to_numpy(),
            err_msg="compound score column drift from golden master",
        )

    def test_daily_returns_match_golden(self):
        cur = self._returns
        gold = self._golden_returns
        self.assertEqual(len(cur), len(gold), "daily-returns length mismatch")
        self.assertTrue((cur.index == gold.index).all(), "daily-returns index mismatch")
        np.testing.assert_array_equal(
            cur.to_numpy(),
            gold.to_numpy(),
            err_msg="daily portfolio returns drift from golden master",
        )

    def test_assess_metrics_match_golden(self):
        cur = self._metrics
        gold = self._golden_metrics
        self.assertEqual(set(cur), set(gold), "scalar metric keys mismatch")
        for k in cur:
            self.assertAlmostEqual(
                cur[k],
                gold[k],
                places=12,
                msg=f"metric {k!r} drift: cur={cur[k]} gold={gold[k]}",
            )


if __name__ == "__main__":
    if "--regenerate-fixtures" in sys.argv:
        sys.argv.remove("--regenerate-fixtures")
        _regenerate_fixtures()
    else:
        unittest.main()
