"""AlphaLens CLI entry point.

Top-level commands:
    status              — global state (queue + digest + dedup)
    backtest            — screener-agnostic backtest harness
    audit               — multi-phase audit + Bonferroni accountability

Groups:
    watchdog/           — Layer 1 SEC EDGAR event detection (LIVE)
    literature/         — Perplexity scan via launchd (LIVE)
    paper-trade/        — prospective replication tracker (LIVE)
    preregister/        — pre-registration ledger (multiple-testing accountability)
    research/           — ad-hoc validators (LLM filter, walk-forward, ...)
    archive/            — ARCHIVED layer replay (themed/insider/rotation per ADR 0005)
"""

from __future__ import annotations

import logging

import typer
from dotenv import load_dotenv

from alphalens_cli.commands.api import api_app
from alphalens_cli.commands.archive import archive_app
from alphalens_cli.commands.audit import audit_command
from alphalens_cli.commands.backtest import backtest
from alphalens_cli.commands.literature import literature_app
from alphalens_cli.commands.paper_trade import paper_trade_app
from alphalens_cli.commands.preaudit import preaudit_command
from alphalens_cli.commands.preregister import preregister_app
from alphalens_cli.commands.research import research_app
from alphalens_cli.commands.status import status
from alphalens_cli.commands.thematic import thematic_app
from alphalens_cli.commands.watchdog import watchdog_app

load_dotenv()

app = typer.Typer(
    name="alphalens",
    help="AlphaLens stock analysis pipeline CLI.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.callback()
def _root_callback() -> None:
    """Configure logging once before routing to any subcommand.

    basicConfig is a no-op if the root logger is already configured (e.g. by a
    parent Python process); in that case we inherit the parent's setup.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


app.add_typer(watchdog_app, name="watchdog")
app.add_typer(literature_app, name="literature")
app.add_typer(paper_trade_app, name="paper-trade")
app.add_typer(preregister_app, name="preregister")
app.add_typer(research_app, name="research")
app.add_typer(archive_app, name="archive")
app.add_typer(thematic_app, name="thematic")
app.add_typer(api_app, name="api")
app.command(name="status")(status)
app.command(name="backtest")(backtest)
app.command(
    name="audit",
    help="Multi-phase audit — pre-reg + Bonferroni accountability.",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)(audit_command)
app.command(
    name="preaudit",
    help="Fail-fast env check (coverage + smoke) before a long audit.",
)(preaudit_command)


if __name__ == "__main__":
    app()
