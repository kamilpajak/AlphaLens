"""Reusable scorer library — validated signal-computation primitives.

These modules host pure-function scorer code extracted from CLOSED /
RESEARCH_ONLY screener paradigms whose underlying signals proved
empirically valid even when the paradigm itself failed. Consumers
include live production pipelines (e.g. ``alphalens_pipeline.thematic``)
that compose these primitives into multi-signal decision tools.

Doctrine: validated scorers leave reusable libraries for multi-signal
corroboration in OTHER tools, never as standalone strategies. The
adapter / framework code that wires a scorer into a full strategy
remains in ``alphalens_research.screeners.<paradigm>`` and is governed
by that paradigm's ``__status__`` marker.
"""
