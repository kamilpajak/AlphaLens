"""`alphalens guru` — GuruAgent LLM-researcher pilot (Layer 2f candidate).

Subcommands:
  pilot  — Run multi-year GuruAgent pilot, write report + exit code per verdict
  status — Show prompt fingerprint + git SHA
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from pathlib import Path

import pandas as pd
import typer

from alphalens.guru.pilot_runner import SingleYearResult, run_single_year
from alphalens.guru.prompt import load_guru_prompt
from alphalens.guru.report import PilotReport

logger = logging.getLogger(__name__)

guru_app = typer.Typer(
    name="guru",
    help="Layer 2f: GuruAgent LLM-researcher concentrated value portfolio pilot.",
    no_args_is_help=True,
)


def _build_pilot_years(
    *,
    years: Sequence[int],
    data_dir: Path,
    sample_size: int,
    top_n: int,
    seed: int,
    prompt,
    llm_client,
    cache_dir: Path,
    price_loader,
) -> list[SingleYearResult]:
    """Real-data path — orchestrates loaders + scorer across years.

    Separated from the Typer command so tests can monkeypatch it. Production
    wiring constructs GuruScorer + universe + price_store + context_builder
    here then delegates each year to ``run_single_year``.
    """
    from alphalens.backtest.history_store import HistoryStore
    from alphalens.guru.financial_context import build_context, context_to_prompt
    from alphalens.guru.llm_scorer import GuruScorer
    from alphalens.guru.pilot_runner import sample_tickers
    from alphalens.guru.universe import load_sp500_pit

    scorer = GuruScorer(prompt=prompt, llm=llm_client, cache_dir=cache_dir)
    results: list[SingleYearResult] = []

    for year in years:
        universe = load_sp500_pit(year=year, data_dir=data_dir)
        typer.echo(f"[{year}] loaded {len(universe)} S&P 500 tickers from PIT snapshot")

        # Pre-sample BEFORE downloading prices — no need to fetch 503 tickers
        # when we only score 30. Seed convention matches run_single_year
        # (seed + year).
        sampled = sample_tickers(universe, size=sample_size, seed=seed + year)
        typer.echo(
            f"[{year}] pre-sampled {len(sampled)} tickers; downloading prices for sample + SPY..."
        )

        histories = price_loader(sampled + ["SPY"], year=year)
        store = HistoryStore(histories)

        def _ctx(*, ticker, asof, price_series):
            ctx = build_context(ticker=ticker, asof=asof, price_series=price_series)
            if ctx is None:
                return None
            return context_to_prompt(ctx)

        typer.echo(f"[{year}] scoring {len(sampled)} tickers via Gemini...")
        try:
            year_result = run_single_year(
                year=year,
                universe=sampled,
                sample_size=len(sampled),
                top_n=top_n,
                seed=seed,
                scorer=scorer,
                context_builder=_ctx,
                price_store=store,
                benchmark="SPY",
            )
        except RuntimeError as exc:
            typer.echo(f"[{year}] SKIPPED: {exc}. Continuing to next year.")
            continue
        typer.echo(
            f"[{year}] portfolio: {year_result.portfolio_return:+.2%}, "
            f"SPY: {year_result.benchmark_return:+.2%}, "
            f"outperf: {year_result.outperformance:+.2%}, "
            f"cost: ${year_result.total_cost_usd:.3f}"
        )
        results.append(year_result)

    return results


def _default_price_loader(tickers: list[str], *, year: int) -> dict[str, pd.DataFrame]:
    """Batch yfinance loader — uses multi-ticker download for speed.

    Downloads ~15 months of history spanning the test year (prior Dec through
    following March) for trailing-12m context + forward-1y return computation.
    """
    import yfinance as yf

    start = pd.Timestamp(f"{year - 1}-12-01")
    end = pd.Timestamp(f"{year + 1}-03-01")

    try:
        df = yf.download(
            tickers,
            start=start,
            end=end,
            progress=False,
            auto_adjust=True,
            group_by="ticker",
            threads=True,
        )
    except Exception as exc:
        logger.warning("batch yfinance download failed: %s", exc)
        return {}

    out: dict[str, pd.DataFrame] = {}
    if df is None or df.empty:
        return out

    if isinstance(df.columns, pd.MultiIndex):
        for t in tickers:
            try:
                sub = df[t].rename(columns=str.lower).dropna(how="all")
                if sub.empty:
                    continue
                cols = ["open", "high", "low", "close", "volume"]
                if all(c in sub.columns for c in cols):
                    out[t] = sub[cols]
            except (KeyError, IndexError):
                continue
    else:
        # Single-ticker response — columns are plain
        sub = df.rename(columns=str.lower).dropna(how="all")
        if len(tickers) == 1 and not sub.empty:
            cols = ["open", "high", "low", "close", "volume"]
            if all(c in sub.columns for c in cols):
                out[tickers[0]] = sub[cols]
    return out


@guru_app.command(name="pilot")
def pilot(
    prompt: Path = typer.Option(..., help="Path to guru prompt .txt file"),
    data_dir: Path = typer.Option(
        Path("data/sp500_pit"),
        help="Directory with S&P 500 PIT YAML snapshots",
    ),
    output: Path = typer.Option(
        Path("reports/guru_pilot_v2.md"),
        help="Markdown report output path",
    ),
    years: str = typer.Option("2018,2020,2022,2024", help="Test years (comma-separated)"),
    sample_size: int = typer.Option(30, help="Random tickers per year"),
    top_n: int = typer.Option(10, help="Top-N picks by conviction"),
    seed: int = typer.Option(42, help="RNG seed"),
    allow_dirty: bool = typer.Option(True, help="Allow dirty git when fingerprinting"),
    max_cost_usd: float = typer.Option(30.0, help="Hard cap on total LLM cost"),
    min_year_tolerance: float = typer.Option(
        0.0,
        help="Allowed underperformance in worst year (e.g. -0.05 = -5pp tolerance). "
        "Default 0.0 = strict gate. Per Perplexity 2026-04-25, value-style "
        "strategies merit relaxed gate (Buffett 1999 was -9pp vs S&P).",
    ),
) -> None:
    """Run Layer 2f GuruAgent pilot on 4 test years."""
    prompt_obj = load_guru_prompt(prompt, allow_dirty=allow_dirty)
    year_list = [int(y.strip()) for y in years.split(",") if y.strip()]

    import os

    from tradingagents.llm_clients.google_client import GoogleClient

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise typer.BadParameter("GOOGLE_API_KEY not set in environment")
    llm_client = GoogleClient(
        model="gemini-3-pro-preview",
        api_key=api_key,
        thinking_level="low",  # fast + cheaper for screening
    ).get_llm()

    cache_dir = Path.home() / ".alphalens" / "guru_cache"

    years_results = _build_pilot_years(
        years=year_list,
        data_dir=data_dir,
        sample_size=sample_size,
        top_n=top_n,
        seed=seed,
        prompt=prompt_obj,
        llm_client=llm_client,
        cache_dir=cache_dir,
        price_loader=_default_price_loader,
    )

    report = PilotReport(
        years=years_results,
        prompt_sha=prompt_obj.content_sha256,
        git_sha=prompt_obj.git_sha,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.render_markdown())

    typer.echo(f"\nReport: {output}")
    typer.echo(f"Total cost: ${report.total_cost_usd:.2f} (cap ${max_cost_usd})")

    if report.total_cost_usd > max_cost_usd:
        typer.echo(f"⚠ Cost ${report.total_cost_usd:.2f} exceeded cap ${max_cost_usd}")

    verdict = report.evaluate_kill_thresholds(min_year_tolerance=min_year_tolerance)
    typer.echo(f"\n=== VERDICT: {verdict.label} ===")
    if not math.isclose(min_year_tolerance, 0.0):
        typer.echo(f"(relaxed gate: min-year tolerance {min_year_tolerance:+.2%})")
    typer.echo(f"{verdict.summary}")
    typer.echo(f"Passed: {', '.join(verdict.passed_gates) or '(none)'}")
    typer.echo(f"Failed: {', '.join(verdict.failed_gates) or '(none)'}")

    if verdict.label == "KILL":
        raise typer.Exit(code=1)
    if verdict.label == "GRAY":
        raise typer.Exit(code=2)


@guru_app.command(name="status")
def status(
    prompt: Path = typer.Option(..., help="Path to guru prompt .txt file"),
    allow_dirty: bool = typer.Option(True),
) -> None:
    """Show prompt fingerprint + git SHA for reproducibility."""
    prompt_obj = load_guru_prompt(prompt, allow_dirty=allow_dirty)
    typer.echo(f"Prompt path: {prompt_obj.path}")
    typer.echo(f"Git SHA:     {prompt_obj.git_sha}")
    typer.echo(f"Content SHA: {prompt_obj.content_sha256}")
    typer.echo(f"Prompt length: {len(prompt_obj.text)} chars")
