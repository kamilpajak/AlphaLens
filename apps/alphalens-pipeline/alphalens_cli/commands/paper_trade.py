"""`alphalens paper-trade` — H+ prospective replication tracker.

Strategy-parameterized via :mod:`alphalens_research.paper_trade.registry`. The
caller selects a registered strategy by id (e.g. ``--strategy v9d``);
ledger / state / verdict paths and the scorer / universe / refresh
callables are resolved from the matching :class:`Strategy` entry.

Lazy imports across command bodies — same convention as
``alphalens_cli/commands/research.py``: heavy modules
(``alphalens_research.paper_trade.scorer_v9d``, ``alphalens_research.attribution.*``,
``alphalens_pipeline.data.factors``) load Carhart factors + statsmodels which
adds ~1s startup. The Layer 1 watchdog cron must not pay this cost.
"""

from __future__ import annotations

from datetime import date as date_t
from pathlib import Path

import typer

paper_trade_app = typer.Typer(
    name="paper-trade",
    help="Prospective paper-trade tracker (H+ infrastructure, strategy-parameterized).",
    no_args_is_help=True,
)

_STRATEGY_OPTION_HELP = (
    "Registered strategy id (see alphalens_research.paper_trade.registry.REGISTRY)."
)


@paper_trade_app.callback()
def _paper_trade_callback() -> None:
    """Force multi-command behaviour even when only one command is registered."""


@paper_trade_app.command(name="refresh-data")
def refresh_data(
    strategy: str = typer.Option(
        ...,
        "--strategy",
        help=_STRATEGY_OPTION_HELP,
    ),
    days: int = typer.Option(
        7,
        "--days",
        help="Trailing window in calendar days to refresh from iVolatility.",
    ),
    universe_size_cap: int = typer.Option(
        0,
        "--universe-cap",
        help="Optional cap on universe size for testing (0 = no cap).",
    ),
) -> None:
    """Pull latest data for the strategy's PIT universe.

    Writes incrementally to the strategy's cache dir. Designed to be
    called by the Sunday 17:00 weekly cron, but safe to run manually.
    """
    import logging
    import os

    import ivolatility as ivol
    from alphalens_research.paper_trade.registry import get_strategy, resolve_callable
    from alphalens_research.paper_trade.scorer_v9d import DEFAULT_SMD_CACHE_DIR

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    cfg = get_strategy(strategy)
    pit_union = resolve_callable(cfg.universe_callable_path)
    incremental_refresh_smd = resolve_callable(cfg.refresh_callable_path)

    api_key = os.environ.get("IVOLATILITY_API_KEY", "")
    if not api_key:
        typer.echo("ERROR: IVOLATILITY_API_KEY not set in environment.", err=True)
        raise typer.Exit(code=1)
    ivol.setLoginParams(apiKey=api_key)
    ivol.setDelayBetweenRequests(0.2)

    universe = pit_union()
    if universe_size_cap > 0:
        universe = universe[:universe_size_cap]
    today = date_t.today()

    typer.echo(
        f"[{strategy}] Incremental refresh: {len(universe)} tickers up to {today} "
        f"(--days={days} flag is informational; refresh appends only "
        f"unseen rows beyond each parquet's max tradeDate)"
    )
    counts = incremental_refresh_smd(
        universe,
        target_end=today,
        cache_dir=DEFAULT_SMD_CACHE_DIR,
    )
    typer.echo(
        f"Refresh result: refreshed={counts['refreshed']} "
        f"up_to_date={counts['skipped_uptodate']} "
        f"missing_no_parquet={counts['skipped_missing']} "
        f"errors={counts['errors']}"
    )


@paper_trade_app.command(name="score")
def score(
    strategy: str = typer.Option(
        ...,
        "--strategy",
        help=_STRATEGY_OPTION_HELP,
    ),
    asof: str = typer.Option(
        "",
        "--asof",
        help="Scoring date (YYYY-MM-DD); empty = latest valid trading date in cache.",
    ),
    holding_period_days: int = typer.Option(
        5,
        "--holding-days",
        help="Trading-day window for prior-week realized return computation.",
    ),
    cost_bps_rt: float = typer.Option(
        30.0,
        "--cost-bps-rt",
        help="Round-trip cost in basis points (locked at 30 per pre-reg).",
    ),
    decile_pct: float = typer.Option(
        0.10,
        "--decile-pct",
        help="Long-decile fraction (locked at 0.10 per pre-reg).",
    ),
) -> None:
    """Score current PIT universe, append ledger entry, update state.

    Reads previous portfolio from the strategy's state file, computes
    its realized return over the past ``holding_period_days`` from the
    previous asof, then writes a new ledger entry and overwrites state
    with the freshly-computed top decile.
    """
    import logging

    from alphalens_research.paper_trade.ledger import (
        LedgerEntry,
        append_ledger_entry,
        default_ledger_path,
    )
    from alphalens_research.paper_trade.registry import get_strategy, resolve_callable
    from alphalens_research.paper_trade.scorer_v9d import (
        benchmark_return,
        compute_realized_return,
        latest_trading_asof,
        make_smd_loader,
    )
    from alphalens_research.paper_trade.state import PaperTradeState, default_state_path

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    cfg = get_strategy(strategy)
    pit_union = resolve_callable(cfg.universe_callable_path)
    score_top_decile = resolve_callable(cfg.scorer_callable_path)
    ledger_path = default_ledger_path(strategy)
    state_path = default_state_path(strategy)

    smd_loader = make_smd_loader()
    target_asof = (
        date_t.fromisoformat(asof)
        if asof
        else latest_trading_asof(today=date_t.today(), smd_loader=smd_loader)
    )
    if target_asof is None:
        typer.echo("ERROR: could not resolve target asof from cache.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"[{strategy}] Target asof: {target_asof}")

    prior_state = PaperTradeState.load(state_path)
    typer.echo(
        f"Prior state: held={len(prior_state.held)} as_of={prior_state.as_of} "
        f"rebalance_n={prior_state.rebalance_n}"
    )

    universe = pit_union()
    typer.echo(f"PIT universe: {len(universe)} tickers")

    result = score_top_decile(
        target_asof,
        universe=universe,
        smd_loader=smd_loader,
        decile_pct=decile_pct,
    )
    typer.echo(
        f"Scored: {result.n_scored}/{result.universe_size} "
        f"({result.coverage_pct * 100:.1f}% coverage); "
        f"top decile size={result.decile_size}"
    )

    realized_long_gross = float("nan")
    realized_long_net = float("nan")
    bench = float("nan")
    if prior_state.held and prior_state.as_of:
        realized_long_gross, n_realized = compute_realized_return(
            prior_state.held,
            prior_state.as_of,
            holding_period_days=holding_period_days,
            smd_loader=smd_loader,
        )
        bench = benchmark_return(
            prior_state.as_of,
            holding_period_days=holding_period_days,
            smd_loader=smd_loader,
        )
        # v9D drag convention: drag_per_period = annual_drag_bps / 10000 / (252 / stride)
        per_period_drag = cost_bps_rt / 10_000.0 / (252 / holding_period_days)
        realized_long_net = realized_long_gross - per_period_drag
        typer.echo(
            f"Realized prior-week: gross={realized_long_gross:+.4f} "
            f"net={realized_long_net:+.4f} bench={bench:+.4f} "
            f"({n_realized}/{len(prior_state.held)} resolved)"
        )

    if result.top_decile_tickers and prior_state.as_of:
        # Append ledger entry only when we have BOTH prior holdings to mark
        # AND new holdings to record; first-ever score skips the ledger
        # write (no prior week to mark) and only writes the state.
        entry = LedgerEntry(
            asof=target_asof,
            rebalance_n=prior_state.rebalance_n + 1,
            n_held=len(result.top_decile_tickers),
            holdings=result.top_decile_tickers,
            prior_holdings=prior_state.held,
            realized_return_long_gross=float(realized_long_gross),
            realized_return_long_net=float(realized_long_net),
            benchmark_return_mdy=float(bench),
            cost_drag_bps=float(cost_bps_rt),
            universe_size=int(result.universe_size),
        )
        ledger = append_ledger_entry(entry, ledger_path)
        typer.echo(f"Ledger appended; n={len(ledger)}")
    else:
        typer.echo("First-ever score (no prior state) — ledger entry deferred to next week.")

    new_state = PaperTradeState(
        held=result.top_decile_tickers,
        scores=result.top_decile_scores,
        as_of=target_asof,
        rebalance_n=prior_state.rebalance_n + 1,
    )
    new_state.save(state_path)
    typer.echo(
        f"State saved: held={len(new_state.held)} as_of={new_state.as_of} n={new_state.rebalance_n}"
    )


@paper_trade_app.command(name="verdict")
def verdict_cmd(
    strategy: str = typer.Option(
        ...,
        "--strategy",
        help=_STRATEGY_OPTION_HELP,
    ),
    out: Path = typer.Option(
        None,
        "--out",
        help="Output markdown path (default: strategy verdict path).",
    ),
) -> None:
    """Compute running stats + decision-rule verdict from current ledger."""
    from alphalens_research.paper_trade.ledger import default_ledger_path, load_ledger
    from alphalens_research.paper_trade.verdict import default_verdict_path, evaluate_decision_rule

    out_path = out if out is not None else default_verdict_path(strategy)
    ledger = load_ledger(default_ledger_path(strategy))
    if ledger.empty:
        typer.echo(f"[{strategy}] Ledger empty — no verdict to compute yet.")
        return
    result = evaluate_decision_rule(ledger)

    md_lines = [
        f"# {strategy} paper-trade verdict — {result.verdict}",
        "",
        f"- n_obs: **{result.n_obs}** (checkpoint: `{result.checkpoint}`)",
        f"- cumulative αt: **{result.cumulative_alpha_t:+.2f}**",
        f"- cumulative α (annualized): **{result.cumulative_alpha_annualized * 100:+.2f}%**",
        f"- cumulative Sharpe net: **{result.cumulative_sharpe_net:.2f}**",
        f"- cumulative MaxDD: **{result.cumulative_max_drawdown * 100:+.1f}%**",
    ]
    if result.sub_period_alpha_ts:
        md_lines.append(
            f"- sub-period αts (13-week chunks): {[f'{t:+.2f}' for t in result.sub_period_alpha_ts]}"
        )
    md_lines += [
        "",
        f"**Rationale:** {result.rationale}",
        "",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(md_lines) + "\n")
    typer.echo("\n".join(md_lines))
    typer.echo(f"\n→ {out_path}")
