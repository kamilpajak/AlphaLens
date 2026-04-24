"""`alphalens insider` — Layer 2d Form 4 cluster-buy scan.

Requires local data files seeded from ``ticker_cik_refresher`` (P3) and
``iwm_refresher`` (P4). If absent, prints a helpful bootstrap hint.

Universe is IWM current constituents (Phase 3 live-mode; Phase 3b
backtest uses PIT reconstruction instead).
"""

from __future__ import annotations

import os
from datetime import date as _date
from pathlib import Path

import typer

insider_app = typer.Typer(
    name="insider",
    help="Layer 2d: Form 4 cluster-buy scan (Russell 2000 / IWM universe).",
    no_args_is_help=True,
)


_DEFAULT_IWM_PATH = Path("alphalens/alt_data/data/iwm_current.yaml")
_DEFAULT_CIK_MAP_PATH = Path("alphalens/alt_data/data/ticker_cik_map.yaml")
_DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "insider_form4"
_DEFAULT_REPORT_DIR = Path.home() / ".alphalens" / "insider"


def _require_user_agent() -> str:
    ua = os.environ.get("SEC_EDGAR_USER_AGENT")
    if not ua:
        raise typer.BadParameter(
            "SEC_EDGAR_USER_AGENT env var required. "
            "Example: export SEC_EDGAR_USER_AGENT='Your Name your@email.com'"
        )
    return ua


def _check_data_files(iwm_path: Path, cik_path: Path) -> None:
    missing = []
    if not iwm_path.exists():
        missing.append(str(iwm_path))
    if not cik_path.exists():
        missing.append(str(cik_path))
    if missing:
        hint = (
            "Missing data file(s): " + ", ".join(missing) + "\n\n"
            "Bootstrap with:\n"
            "  .venv/bin/python -c 'from pathlib import Path; "
            "from alphalens.alt_data.sec_edgar_client import SecEdgarClient; "
            "from alphalens.alt_data.ticker_cik_refresher import refresh_ticker_cik_map; "
            "refresh_ticker_cik_map(SecEdgarClient(user_agent=\"YOUR UA\"), "
            f"Path(\"{_DEFAULT_CIK_MAP_PATH}\"))'\n"
            "  .venv/bin/python -c 'from pathlib import Path; "
            "from alphalens.alt_data.iwm_refresher import refresh_iwm_current; "
            f"refresh_iwm_current(Path(\"{_DEFAULT_IWM_PATH}\"))'\n"
        )
        raise typer.BadParameter(hint)


@insider_app.command(name="screen")
def screen(
    top_n: int = typer.Option(10, help="Top-N cluster events to report"),
    dry_run: bool = typer.Option(
        False, help="Print report to stdout instead of sending to Telegram"
    ),
    analyze: bool = typer.Option(
        False, help="Submit top-N to the candidate queue for Layer 3 deep analysis"
    ),
    report: Path = typer.Option(
        None, help="Write markdown report to this path (in addition to stdout/telegram)"
    ),
    universe_file: Path = typer.Option(
        _DEFAULT_IWM_PATH, help="IWM current snapshot yaml"
    ),
    cik_map_file: Path = typer.Option(
        _DEFAULT_CIK_MAP_PATH, help="Ticker↔CIK map yaml"
    ),
) -> None:
    """Scan IWM universe for Form 4 cluster buys; report top-N.

    Signal spec (per design doc §6 locked by Perplexity R5):
    ≥3 distinct officers+directors within 30 days, code P (open-market),
    exclude 10b5-1 plans adopted ≥90 days before transaction.
    """
    import pandas as pd

    from alphalens.alt_data.russell_universe import load_iwm_current
    from alphalens.alt_data.sec_edgar_client import SecEdgarClient
    from alphalens.alt_data.ticker_cik_map import TickerCikMap
    from alphalens.screeners.insider.pipeline import InsiderPipeline
    from alphalens.screeners.insider.scorer import InsiderScorer

    _check_data_files(universe_file, cik_map_file)
    user_agent = _require_user_agent()

    cik_map = TickerCikMap.load(cik_map_file)
    edgar = SecEdgarClient(user_agent=user_agent)
    _DEFAULT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    scorer = InsiderScorer(
        edgar_client=edgar, ticker_cik_map=cik_map, cache_dir=_DEFAULT_CACHE_DIR
    )
    pipeline = InsiderPipeline(
        scorer=scorer,
        universe_loader=lambda: load_iwm_current(universe_file),
    )

    curr_date = _date.today()
    result = pipeline.run(curr_date=curr_date, top_n=top_n)

    text = _format_report(result, curr_date)

    if report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text)
        typer.echo(f"wrote report → {report}")

    if analyze and not result.empty:
        from alphalens.queue import CandidateQueue, default_queue_path

        try:
            with CandidateQueue(default_queue_path()) as queue:
                submitted = queue.submit(pipeline.to_candidates(result))
            typer.echo(f"queued {submitted} insider candidate(s) for Layer 3")
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"queue submit failed: {exc}", err=True)

    if dry_run or report is not None:
        typer.echo(text)
        return

    try:
        from alphalens.watchdog.dispatch.handlers.telegram import TelegramHandler

        bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = os.environ["TELEGRAM_CHAT_ID"]
        TelegramHandler(bot_token=bot_token, chat_id=chat_id).send_message(text)
        typer.echo(f"sent {len(result)} insider candidates to Telegram")
    except KeyError as exc:
        typer.echo(
            f"Telegram env var missing ({exc}); re-run with --dry-run or set TELEGRAM_*",
            err=True,
        )


@insider_app.command(name="version")
def version() -> None:
    """Print Layer 2d insider screener version info."""
    typer.echo("Layer 2d insider screener — Phase 3a (live scan)")
    typer.echo("Signal spec (design doc §6, Perplexity R5):")
    typer.echo("  ≥3 distinct officers+directors, 30d window")
    typer.echo("  Code P (open-market), exclude 10b5-1 plans ≥90d old")


def _format_report(df, curr_date: _date) -> str:
    lines = [f"# Insider cluster scan — {curr_date.isoformat()}", ""]
    if df.empty:
        lines.append("No clusters detected in IWM universe today.")
        lines.append("")
        lines.append("Cluster spec: ≥3 distinct officers+directors in 30 days, "
                     "code P (open-market), exclude 10b5-1 plans ≥90 days old.")
        return "\n".join(lines)
    lines.append(f"**{len(df)} cluster event(s)** in IWM universe:")
    lines.append("")
    lines.append("| # | Ticker | Insiders | Aggregate $ | As-of |")
    lines.append("|---:|---|---:|---:|---|")
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        lines.append(
            f"| {i} | {row['ticker']} | {int(row['insider_count'])} | "
            f"${float(row['aggregate_dollar']):,.0f} | {row['asof']} |"
        )
    return "\n".join(lines)
