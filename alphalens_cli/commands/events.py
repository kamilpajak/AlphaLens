"""`alphalens events` — event-driven research screeners (Layer 2f candidate).

Subcommands:
  8k-screen — Perplexity-recommended go/no-go test: compute CAR per 8-K Item
              type over a universe & window, print verdict (KILL/GRAY/PROCEED).
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import pandas as pd
import typer

from alphalens.alt_data.sec_edgar_client import SecEdgarClient
from alphalens.archive.events.eightk_screener import (
    KILL_THRESHOLD_BPS,
    PROCEED_THRESHOLD_BPS,
    run_screen,
)

events_app = typer.Typer(
    name="events",
    help="Event-driven research screeners (Layer 2f candidate).",
    no_args_is_help=True,
)


def _yfinance_loader(start: pd.Timestamp, end: pd.Timestamp):
    """Cache-light loader: fetches daily close via yfinance once per ticker/run."""
    import yfinance as yf

    cache: dict[str, pd.Series | None] = {}

    def _load(ticker: str) -> pd.Series:
        if ticker in cache:
            s = cache[ticker]
            if s is None:
                raise RuntimeError(f"yfinance returned no data for {ticker}")
            return s
        df = yf.download(
            ticker,
            start=start - pd.Timedelta(days=10),
            end=end + pd.Timedelta(days=90),
            progress=False,
            auto_adjust=True,
        )
        if df.empty:
            cache[ticker] = None
            raise RuntimeError(f"yfinance returned no data for {ticker}")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        s = df["Close"] if "Close" in df.columns else df.iloc[:, 0]
        s = s.dropna()
        cache[ticker] = s
        return s

    return _load


def _load_sample_universe(n: int, seed: int = 42) -> list[tuple[str, str]]:
    """Random sample from SEC's company_tickers.json (all US public issuers).

    Returns list of (ticker, 10-digit zero-padded CIK).
    """
    client = SecEdgarClient(
        user_agent=os.environ.get("SEC_EDGAR_USER_AGENT", "AlphaLens research@example.com")
    )
    tickers = client.fetch_company_tickers()
    # tickers: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    all_pairs = [(row["ticker"], f"{int(row['cik_str']):010d}") for row in tickers.values()]
    rng = random.Random(seed)
    rng.shuffle(all_pairs)
    return all_pairs[:n]


@events_app.command(name="8k-screen")
def eightk_screen(
    start: str = typer.Option("2022-01-01", help="Start date YYYY-MM-DD"),
    end: str = typer.Option("2024-12-31", help="End date YYYY-MM-DD"),
    sample_size: int = typer.Option(
        100, help="Number of random tickers from SEC company_tickers.json"
    ),
    seed: int = typer.Option(42, help="RNG seed for reproducibility"),
    output: Path = typer.Option(
        Path("reports/events_8k_screen.md"),
        help="Markdown output path",
    ),
    benchmark: str = typer.Option("SPY", help="Benchmark ticker for CAR"),
    min_sample_per_item: int = typer.Option(
        20,
        help="Minimum filings per Item for verdict to count (filters rare-item noise)",
    ),
) -> None:
    """Run 8-K CAR go/no-go screen per Perplexity 4th-attempt plan."""
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)

    typer.echo(f"Loading sample of {sample_size} tickers from SEC EDGAR company_tickers.json...")
    pairs = _load_sample_universe(sample_size, seed=seed)
    typer.echo(f"Sampled {len(pairs)} tickers.")

    sec_client = SecEdgarClient(
        user_agent=os.environ.get("SEC_EDGAR_USER_AGENT", "AlphaLens research@example.com")
    )
    loader = _yfinance_loader(start_ts, end_ts)

    typer.echo(
        f"Fetching 8-K filings + computing CAR for {start} → {end} (benchmark {benchmark})..."
    )
    result = run_screen(
        ticker_cik_pairs=pairs,
        sec_client=sec_client,
        price_loader=loader,
        benchmark=benchmark,
        start=start_ts,
        end=end_ts,
    )
    summary = result["summary"]
    records = result["records"]

    # Filter summary by min_sample to suppress rare-item noise
    summary_filtered = summary[summary["n"] >= min_sample_per_item].copy()
    summary_sorted = summary_filtered.sort_values(
        ["window_days", "mean_car_bps"], ascending=[True, False]
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_markdown(summary_sorted, records, start, end, sample_size))
    typer.echo(f"\nReport written to {output}\n")

    typer.echo(f"=== GO/NO-GO SUMMARY (filtered to n >= {min_sample_per_item}) ===")
    if summary_sorted.empty:
        typer.echo(
            f"No (item, window) groups had ≥{min_sample_per_item} filings. "
            f"Increase sample_size or widen date range."
        )
        raise typer.Exit(code=2)

    # Top 5 candidates at 20d window
    twenty_d = summary_sorted[summary_sorted["window_days"] == 20]
    if twenty_d.empty:
        typer.echo("No 20-day window data; check sample size.")
        raise typer.Exit(code=2)

    # Rank by winsorized mean (robust) not raw mean (outlier-dominated).
    twenty_d_sorted = twenty_d.sort_values("winsorized_mean_bps", ascending=False)

    typer.echo("\n20-day CAR ranking (by winsorized mean, robust):")
    typer.echo("  Item    n     mean    median  winsor. t-stat  std      verdict")
    for _, row in twenty_d_sorted.iterrows():
        typer.echo(
            f"  {row['item']:<6} "
            f"{row['n']:<5} "
            f"{row['mean_car_bps']:+7.1f} "
            f"{row['median_car_bps']:+7.1f} "
            f"{row['winsorized_mean_bps']:+7.1f} "
            f"{row['tstat']:+6.2f}  "
            f"{row['std_car_bps']:7.1f}  "
            f"{row['verdict']}"
        )

    proceed_items = twenty_d_sorted[twenty_d_sorted["verdict"] == "PROCEED"]
    if not proceed_items.empty:
        best = proceed_items.iloc[0]
        typer.echo(
            f"\n✓ PROCEED: Item {best['item']} "
            f"winsorized={best['winsorized_mean_bps']:+.1f} bps, "
            f"t={best['tstat']:.2f}, n={best['n']}. "
            "Event-driven strategy justified — proceed to IS backtest design."
        )
    else:
        any_kill = (twenty_d_sorted["verdict"] == "KILL").any()
        all_kill_or_gray = set(twenty_d_sorted["verdict"]).issubset({"KILL", "GRAY"})
        if all_kill_or_gray and any_kill:
            typer.echo(
                f"\n✗ KILL/GRAY only. No item type meets PROCEED criteria "
                f"(winsorized >= {PROCEED_THRESHOLD_BPS} bps, |t| >= 2.0, std <= {400} bps). "
                "Accept signal: retail-viable event-driven edge not found in this sample."
            )
            raise typer.Exit(code=1)
        typer.echo(
            "\n⚠ GRAY zone only. Consider: larger sample, sub-categorization with LLM, "
            "or alternative research direction."
        )


def _render_markdown(
    summary: pd.DataFrame,
    records: pd.DataFrame,
    start: str,
    end: str,
    sample_size: int,
) -> str:
    lines = [
        "# 8-K go/no-go screener report",
        "",
        f"**Window:** {start} → {end}",
        f"**Sample size:** {sample_size} tickers (random from SEC company_tickers.json)",
        f"**Total filings analyzed:** {len(records):,}",
        f"**Valid CAR records (non-NaN):** "
        f"{int(records['car'].notna().sum()) if not records.empty else 0:,}",
        "",
        f"**Thresholds:** KILL ≤ {KILL_THRESHOLD_BPS} bps, PROCEED ≥ {PROCEED_THRESHOLD_BPS} bps",
        "",
        "## CAR summary by Item × window (robust statistics)",
        "",
        "| Item | window (d) | n | mean (bps) | median (bps) | winsorized (bps) | t-stat | std (bps) | verdict |",
        "|---|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for _, row in summary.iterrows():
        lines.append(
            f"| {row['item']} | {int(row['window_days'])} | {int(row['n'])} | "
            f"{row['mean_car_bps']:+.1f} | {row['median_car_bps']:+.1f} | "
            f"{row['winsorized_mean_bps']:+.1f} | {row['tstat']:+.2f} | "
            f"{row['std_car_bps']:.1f} | {row['verdict']} |"
        )
    return "\n".join(lines) + "\n"
