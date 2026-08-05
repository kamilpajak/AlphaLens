"""AlphaLens CLI entry point.

Top-level commands:
    status              — global state (queue + digest + dedup)
    audit               — multi-phase audit + Bonferroni accountability
    preaudit            — fail-fast env check before a long audit

Groups:
    broker/             — broker execution layer reads (SIM-only, ADR 0014)
    buffett/            — Buffett Mode-A observational lens over the brief (ad hoc)
    edgar/              — Layer 1 SEC EDGAR event detection (LIVE)
    literature/         — Perplexity scan via VPS systemd (LIVE)
    preregister/        — pre-registration ledger (multiple-testing accountability)
    thematic/           — Layer 2-5 thematic event pipeline (LIVE)
"""

from __future__ import annotations

import logging

import typer
from dotenv import load_dotenv

from alphalens_cli.commands.audit import audit_command
from alphalens_cli.commands.broker import broker_app
from alphalens_cli.commands.buffett import buffett_app
from alphalens_cli.commands.cache import cache_app
from alphalens_cli.commands.doctrine import audit_verdict_command
from alphalens_cli.commands.edgar import edgar_app
from alphalens_cli.commands.experts import experts_app
from alphalens_cli.commands.feedback import feedback_app
from alphalens_cli.commands.literature import literature_app
from alphalens_cli.commands.preaudit import preaudit_command
from alphalens_cli.commands.preregister import preregister_app
from alphalens_cli.commands.status import status
from alphalens_cli.commands.templates import templates_app
from alphalens_cli.commands.thematic import thematic_app

load_dotenv()

app = typer.Typer(
    name="alphalens",
    help="AlphaLens stock analysis pipeline CLI.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.callback()
def _root_callback() -> None:
    """Configure logging and validate operator env once, before any subcommand.

    basicConfig is a no-op if the root logger is already configured (e.g. by a
    parent Python process); in that case we inherit the parent's setup.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _validate_provider_routing_env()


def _validate_provider_routing_env() -> None:
    """Reject a malformed ``ALPHALENS_OPENROUTER_*`` knob before work starts.

    The knobs are normally read late — deep inside OpenRouter client
    construction, once a stage first needs an LLM. That is far too late to
    notice a typo: several stages catch ``ValueError`` there so a missing
    ``OPENROUTER_API_KEY`` degrades gracefully rather than crashing a whole
    day's run, and a malformed knob is indistinguishable from that at the
    catch site. ``thematic brief`` is the worst case — it would stamp every
    row ``brief_status='unavailable'``, write the parquet, and exit 0, so
    ``run_thematic_day.sh``'s ``set -e`` never trips and ``rebuild-cache``
    publishes a day of prose-less briefs to Postgres behind one warning line.

    Reading the knobs here instead turns that into a startup failure, before
    any stage writes anything, for every command uniformly. Checked at the
    root callback rather than per-command because the same env reaches every
    LLM stage. The import is local to keep the module off the CLI's cold-start
    path in principle, though it is already loaded by the command tree.
    """
    from alphalens_pipeline.data.alt_data.openrouter_client import provider_routing_from_env

    try:
        provider_routing_from_env()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


app.add_typer(broker_app, name="broker")
app.add_typer(buffett_app, name="buffett")
app.add_typer(cache_app, name="cache")
app.add_typer(edgar_app, name="edgar")
app.add_typer(experts_app, name="experts")
app.add_typer(feedback_app, name="feedback")
app.add_typer(literature_app, name="literature")
app.add_typer(preregister_app, name="preregister")
app.add_typer(templates_app, name="templates")
app.add_typer(thematic_app, name="thematic")
app.command(name="status")(status)
app.command(
    name="audit",
    help="Multi-phase audit — pre-reg + Bonferroni accountability.",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)(audit_command)
app.command(
    name="preaudit",
    help="Fail-fast env check (coverage + smoke) before a long audit.",
)(preaudit_command)
app.command(
    name="audit-verdict",
    help="Apply the pre-registered doctrine bars (3.5/2.5/per-phase/15bps/AV-PIT) "
    "to per-window audit JSONs — distinct from audit's offset-stability gate.",
)(audit_verdict_command)


if __name__ == "__main__":
    app()
