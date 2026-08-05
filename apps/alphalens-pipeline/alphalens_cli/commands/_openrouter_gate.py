"""Startup gate for the ``ALPHALENS_OPENROUTER_*`` routing knobs.

The knobs are normally read late — deep inside OpenRouter client construction,
once a stage first needs an LLM. That is far too late to notice a typo: several
stages catch ``ValueError`` there so a missing ``OPENROUTER_API_KEY`` degrades
gracefully rather than crashing a whole day's run, and a malformed knob is
indistinguishable from that at the catch site. ``thematic brief`` is the worst
case — it would stamp every row ``brief_status='unavailable'``, write the
parquet, and exit 0, so ``run_thematic_day.sh``'s ``set -e`` never trips and
``rebuild-cache`` publishes a day of prose-less briefs to Postgres behind one
warning line. ``experts enrich`` has the same shape (its qualitative layer
catches bare ``Exception`` around the call).

Reading the knobs at command-group entry turns that into a startup failure,
before any stage writes anything.

Scoped to the command GROUPS that construct an LLM client, deliberately NOT to
the root callback. ``/etc/alphalens/env`` is shared by every systemd unit, so a
root-level gate would take down ``alphalens edgar detect`` — a 15-minute poller
that never reads an OpenRouter variable — over a typo in an LLM routing knob.
The blast radius has to match the variable's readership.
"""

from __future__ import annotations

import typer


def validate_provider_routing_env() -> None:
    """Reject a malformed routing knob before any command body runs."""
    from alphalens_pipeline.data.alt_data.openrouter_client import provider_routing_from_env

    try:
        provider_routing_from_env()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
