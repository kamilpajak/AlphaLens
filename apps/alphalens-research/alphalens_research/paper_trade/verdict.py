"""Decision-rule evaluator for the v9D paper-trade prospective track.

Two-stage gate per pre-reg ``v9d_long_only_paper_trade_2026_05_04``:

  - 26-week checkpoint: cumulative αt ≥ 1.96 AND Sharpe net ≥ 0.30
  - 52-week checkpoint: cumulative αt ≥ 1.96 AND Sharpe net ≥ 0.30 AND
    no single 13-week sub-period αt < +0.5

This module is read-only with respect to the ledger; it loads the parquet,
computes cumulative stats (Carhart-4F regression on net returns vs MDY),
and emits a structured ``DecisionRuleResult``. The CLI command ``alphalens_research
paper-trade verdict`` is the human-facing wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pandas as pd
from alphalens_pipeline.data.factors import load_carhart_daily

from alphalens_research.attribution.factor_analysis import run_regression
from alphalens_research.backtest.metrics import max_drawdown, sharpe
from alphalens_research.paper_trade.registry import default_paper_trade_dir, get_strategy

CHECKPOINT_26W_N_OBS = 26
CHECKPOINT_52W_N_OBS = 52
ALPHA_T_THRESHOLD = 1.96
SHARPE_THRESHOLD = 0.30
SUB_PERIOD_FLOOR_ALPHA_T = 0.5
SUB_PERIOD_LENGTH_WEEKS = 13


@dataclass
class DecisionRuleResult:
    n_obs: int
    checkpoint: str  # "pre-26w" | "26w" | "between" | "52w" | "post-52w"
    cumulative_alpha_t: float
    cumulative_alpha_annualized: float
    cumulative_sharpe_net: float
    cumulative_max_drawdown: float
    sub_period_alpha_ts: list[float]  # only populated at 52w
    verdict: str  # "PENDING" | "PASS_26W" | "FAIL_26W" | "PASS_52W" | "FAIL_52W"
    rationale: str


def _classify_checkpoint(n_obs: int) -> str:
    if n_obs < CHECKPOINT_26W_N_OBS:
        return "pre-26w"
    if n_obs == CHECKPOINT_26W_N_OBS:
        return "26w"
    if n_obs < CHECKPOINT_52W_N_OBS:
        return "between"
    if n_obs == CHECKPOINT_52W_N_OBS:
        return "52w"
    return "post-52w"


def compute_running_stats(
    ledger: pd.DataFrame,
    *,
    return_col: str = "realized_return_long_net",
    periods_per_year: int = 52,
) -> dict:
    """Compute cumulative stats from a non-empty ledger.

    Empty ledger returns zeros / nans (caller can short-circuit). Both
    portfolio and benchmark return series are aligned by ``asof`` and
    converted to ``DatetimeIndex`` for the regression.
    """
    if ledger.empty:
        return {
            "n_obs": 0,
            "alpha_t": float("nan"),
            "alpha_annualized": float("nan"),
            "sharpe_net": float("nan"),
            "max_drawdown": float("nan"),
            "asof_first": None,
            "asof_last": None,
        }

    df = ledger.sort_values("asof").reset_index(drop=True)
    asofs = pd.to_datetime(df["asof"])
    rets = pd.Series(df[return_col].to_numpy(), index=asofs, name="port", dtype=float)

    asof_first = asofs.min().date()
    asof_last = asofs.max().date()

    # Carhart factors (HAC OLS) — load with 30d buffer.
    carhart = load_carhart_daily(
        start=asof_first - timedelta(days=30),
        end=asof_last + timedelta(days=30),
    )

    n = len(rets)
    sh = float(sharpe(rets.tolist(), periods_per_year=periods_per_year))
    cum = (1.0 + rets.fillna(0)).cumprod()
    mdd = float(max_drawdown(cum.tolist()))

    if n < 20:
        return {
            "n_obs": n,
            "alpha_t": float("nan"),
            "alpha_annualized": float("nan"),
            "sharpe_net": sh,
            "max_drawdown": mdd,
            "asof_first": asof_first,
            "asof_last": asof_last,
            "note": "fewer than 20 obs — αt unreliable",
        }

    res = run_regression(
        rets,
        carhart[["Mkt-RF", "SMB", "HML", "Mom", "RF"]],
        ["Mkt-RF", "SMB", "HML", "Mom"],
        periods_per_year=periods_per_year,
    )
    return {
        "n_obs": n,
        "alpha_t": float(res.alpha_tstat),
        "alpha_annualized": float(res.alpha_annualized),
        "sharpe_net": sh,
        "max_drawdown": mdd,
        "asof_first": asof_first,
        "asof_last": asof_last,
    }


def _per_sub_period_alpha_ts(
    ledger: pd.DataFrame,
    *,
    length_weeks: int = SUB_PERIOD_LENGTH_WEEKS,
    periods_per_year: int = 52,
) -> list[float]:
    """Split ledger into ``length_weeks``-sized chunks and compute αt per chunk."""
    if ledger.empty or len(ledger) < length_weeks:
        return []

    df = ledger.sort_values("asof").reset_index(drop=True)
    asofs = pd.to_datetime(df["asof"])
    rets = pd.Series(df["realized_return_long_net"].to_numpy(), index=asofs, dtype=float)

    asof_first = asofs.min().date()
    asof_last = asofs.max().date()
    carhart = load_carhart_daily(
        start=asof_first - timedelta(days=30),
        end=asof_last + timedelta(days=30),
    )

    out: list[float] = []
    n = len(rets)
    for start in range(0, n - length_weeks + 1, length_weeks):
        chunk = rets.iloc[start : start + length_weeks]
        if len(chunk) < 20:
            continue
        try:
            res = run_regression(
                chunk,
                carhart[["Mkt-RF", "SMB", "HML", "Mom", "RF"]],
                ["Mkt-RF", "SMB", "HML", "Mom"],
                periods_per_year=periods_per_year,
            )
            out.append(float(res.alpha_tstat))
        except (ValueError, RuntimeError):
            continue
    return out


def evaluate_decision_rule(ledger: pd.DataFrame) -> DecisionRuleResult:
    """Apply the two-stage gate sequence to the current ledger state.

    Returns ``PENDING`` for any state before the first checkpoint or
    between checkpoints. PASS/FAIL is only emitted at exact checkpoints
    (n=26 or n=52) — partial-week ledgers stay PENDING.
    """
    stats = compute_running_stats(ledger)
    n = int(stats["n_obs"])
    checkpoint = _classify_checkpoint(n)

    sub_period_alpha_ts: list[float] = []
    if checkpoint == "52w":
        sub_period_alpha_ts = _per_sub_period_alpha_ts(ledger)

    verdict, rationale = _verdict_for_checkpoint(stats, checkpoint, sub_period_alpha_ts)

    return DecisionRuleResult(
        n_obs=n,
        checkpoint=checkpoint,
        cumulative_alpha_t=float(stats.get("alpha_t", float("nan"))),
        cumulative_alpha_annualized=float(stats.get("alpha_annualized", float("nan"))),
        cumulative_sharpe_net=float(stats.get("sharpe_net", float("nan"))),
        cumulative_max_drawdown=float(stats.get("max_drawdown", float("nan"))),
        sub_period_alpha_ts=sub_period_alpha_ts,
        verdict=verdict,
        rationale=rationale,
    )


def _verdict_for_checkpoint(
    stats: dict, checkpoint: str, sub_period_alpha_ts: list[float]
) -> tuple[str, str]:
    if checkpoint in {"pre-26w", "between", "post-52w"}:
        return "PENDING", (
            f"n={stats['n_obs']} not at checkpoint boundary; "
            f"cumulative αt={stats.get('alpha_t', float('nan')):+.2f}, "
            f"Sharpe net={stats.get('sharpe_net', float('nan')):.2f}"
        )

    alpha_t = stats.get("alpha_t", float("nan"))
    sharpe_net = stats.get("sharpe_net", float("nan"))

    base_pass = alpha_t >= ALPHA_T_THRESHOLD and sharpe_net >= SHARPE_THRESHOLD

    if checkpoint == "26w":
        if base_pass:
            return "PASS_26W", (
                f"αt={alpha_t:+.2f}≥{ALPHA_T_THRESHOLD} AND Sharpe={sharpe_net:.2f}"
                f"≥{SHARPE_THRESHOLD} — continue to 52w"
            )
        return "FAIL_26W", (
            f"αt={alpha_t:+.2f} or Sharpe={sharpe_net:.2f} below threshold "
            f"({ALPHA_T_THRESHOLD} / {SHARPE_THRESHOLD}) — archive class, postmortem"
        )

    # 52w
    sub_period_floor_ok = len(sub_period_alpha_ts) > 0 and all(
        t >= SUB_PERIOD_FLOOR_ALPHA_T for t in sub_period_alpha_ts
    )
    if base_pass and sub_period_floor_ok:
        return "PASS_52W", (
            f"αt={alpha_t:+.2f}≥{ALPHA_T_THRESHOLD} AND Sharpe={sharpe_net:.2f}"
            f"≥{SHARPE_THRESHOLD} AND all 13w sub-periods αt ≥ {SUB_PERIOD_FLOOR_ALPHA_T} "
            f"({sub_period_alpha_ts}) — ELIGIBLE for capital-deploy review"
        )
    if not base_pass:
        return "FAIL_52W", (
            f"αt={alpha_t:+.2f} or Sharpe={sharpe_net:.2f} below threshold — "
            "archive class, options-implied class fully closed"
        )
    return "FAIL_52W", (
        f"αt={alpha_t:+.2f} and Sharpe={sharpe_net:.2f} pass, but sub-period floor "
        f"violated ({sub_period_alpha_ts}, floor {SUB_PERIOD_FLOOR_ALPHA_T}) — "
        "archive class, regime-instability flagged"
    )


def default_verdict_path(strategy_id: str) -> Path:
    """Strategy-aware verdict markdown path."""
    return default_paper_trade_dir() / get_strategy(strategy_id).verdict_filename
