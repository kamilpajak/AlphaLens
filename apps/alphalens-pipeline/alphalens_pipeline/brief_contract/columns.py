"""Which columns a brief parquet carries, and which stage owns each one.

NOTHING IN PRODUCTION READS THIS MODULE
---------------------------------------
Measured: ``BRIEF_STORE_COLUMNS`` and the per-stage groupings below have
exactly one importer in the tree, and it is
``tests/thematic/test_brief_column_registry.py``. No pipeline module, no CLI
command and no Django view reads any of them. (The sibling
``brief_contract.validation`` is different — three production stages import
its emitter.) So this file is a DECLARATION, assembled for a CI gate and for
a human asking which stage owns a column. It is not a runtime contract, and
nothing anywhere validates a parquet against it.

Wiring it into production was considered and ruled out on cost. The imports
below pull in the whole scoring and enricher stack eagerly: measured with
``python -X importtime -c "import alphalens_pipeline.brief_contract.columns"``,
the cumulative import time is 0.41-0.78 s over three runs, and wall clock for
the same command is 0.48-0.54 s against 0.01 s for a bare interpreter. The
edgar-detect timer fires every 15 minutes, so that cost would land on a
process with no use for the list.

What the assembly buys is a single failure site. :data:`BRIEF_STORE_COLUMNS`
is the union of the declarations published by the stages that compute those
columns, so a column cannot enter or leave the store without the tuple beside
its writer moving too, and the gate named above compares each of those tuples
against a frame that stage really produced.

THE STORE HAS NO SINGLE LAST WRITER
-----------------------------------
Three asymmetries are easy to get wrong when reading this list as "the brief
schema", so they are stated rather than implied.

A QUIET day's parquet is not this list. When no candidate survives, the brief
stage writes a typed-empty frame of ``EMPTY_BRIEF_COLUMNS`` — the identity
columns it inherits plus the columns it owns, and none of the three
day-ranking ones. A day with no cohort has no rank to publish.

``QUAL_COLUMNS`` arrive AFTER the parquet is written. ``alphalens experts
enrich`` runs once the brief stage has published the file and stamps the
Buffett qualitative columns onto it in place, so the brief stage is not the
store's last writer even on a normal day.

``OPTIONS_COLUMNS`` can appear with no brief run at all.
``refresh_published_telemetry`` adds the missing options columns to an
already-published frame, which means the stored width can grow between a
brief's publication and anyone reading it.

WHAT THIS CONTRACT DOES NOT REACH
---------------------------------
The Django project serves these columns to the SPA, and it names every one of
them by string literal — ``apps/alphalens-django`` does not depend on
``alphalens-pipeline`` and so cannot import a single tuple assembled here.
Rename a column on the pipeline side and the registry gate goes red, which is
the point; but the API keeps on serving that field as ``None`` until someone
edits the Django side by hand. Nothing in this module closes that half. The
pattern that would is ``tests/test_expert_column_parity.py``, which imports
the real pipeline tuples and ast-parses the Django mirror as data; extending
it from the expert families to the whole contract is its own piece of work.

THE SEVEN ENRICHER FAMILIES SPLIT THREE AND FOUR
------------------------------------------------
They are imported, not re-measured, and the reason differs per family.

THREE are load-bearing AT THEIR WRITER, so the tuple and the stored column
name cannot disagree: ``ONEIL_COLUMNS`` (the enricher iterates it to build
each Series), ``BUFFETT_COLUMNS`` (same, plus ``dict.fromkeys`` for the
all-null row) and ``OPTIONS_COLUMNS`` (``dict.fromkeys(OPTIONS_COLUMNS)``
nulls the frame, and the brief stage iterates it again to backfill a
published parquet). The cost of that shape is an INVERSION: because the
tuple decides the stored name, renaming an entry renames the production
column, and no comparison of frame against tuple can see it. The gate's own
docstring carries the arithmetic of how many of the 178 names sit in that
position.

FOUR are declarations only — ``QUAL_COLUMNS``, ``SI_COLUMNS``,
``PANEL_COLUMNS`` and ``MARKET_STATE_COLUMNS``. Measured: no writer reads
them. Each writer stamps its columns as string literals, so a rename in one
of these four tuples changes no stored column and raises nothing.

"No writer" is not the same as "unpinned", and it would be wrong to read it
that way. Each of the four is pinned by its own module's tests, which iterate
the tuple over a real enriched frame — measured by renaming one entry per
family:
``QUAL_COLUMNS`` kills 3 tests (``test_buffett_qual_enrichment``,
``test_experts_protocol``), ``MARKET_STATE_COLUMNS`` 4
(``test_market_state``), ``PANEL_COLUMNS`` 2 (``test_disagreement`` and the
Django mirror in ``test_expert_column_parity``) and ``SI_COLUMNS`` 1
(``thematic/short_interest_telemetry/test_enrichment``). ``SI_COLUMNS`` is
the thin one: its single pin is the empty-frame dtype test.

The registry takes all seven as given either way, and the gate re-measures
none of them.
"""

from __future__ import annotations

from alphalens_pipeline.events.insider_cluster_detect import EVENT_CANDIDATE_COLUMNS
from alphalens_pipeline.events.merge import (
    EVENT_FACT_COLUMNS,
    EVENT_PROVENANCE_COLUMNS,
    EVENT_SHADOW_ONLY_COLUMNS,
)
from alphalens_pipeline.experts.buffett.qual_enrichment import QUAL_COLUMNS
from alphalens_pipeline.experts.buffett.quant_enrichment import BUFFETT_COLUMNS
from alphalens_pipeline.experts.disagreement import PANEL_COLUMNS
from alphalens_pipeline.experts.oneil.quant_enrichment import ONEIL_COLUMNS
from alphalens_pipeline.market.market_state import MARKET_STATE_COLUMNS
from alphalens_pipeline.thematic.argumentation.orchestrator import BRIEF_STAGE_COLUMNS
from alphalens_pipeline.thematic.mapping.orchestrator import MAP_THEMES_COLUMNS
from alphalens_pipeline.thematic.options_telemetry.enrichment import OPTIONS_COLUMNS
from alphalens_pipeline.thematic.screening.scorer import SCORE_COLUMNS
from alphalens_pipeline.thematic.short_interest_telemetry.enrichment import SI_COLUMNS

# ---- Per-stage groupings ------------------------------------------------
# Published so a reader can ask which stage owns a name instead of grepping
# the pipeline for the string.

#: ``alphalens thematic map-themes`` — the candidates frame every later stage
#: widens.
MAP_STAGE: tuple[str, ...] = MAP_THEMES_COLUMNS

_SHADOW_ONLY: frozenset[str] = frozenset(EVENT_SHADOW_ONLY_COLUMNS)

#: The event lane, active only under ``ALPHALENS_EVENT_LANE=1``. The merge
#: drops ``EVENT_SHADOW_ONLY_COLUMNS`` before anything downstream sees them,
#: so those two are subtracted here: they are detector bookkeeping and never
#: reach a scored or brief parquet. Without the subtraction this registry
#: would over-declare the store by exactly those two names. Deduplicated
#: because the detector's own tuple already names ``source`` and the nine
#: event facts, which the merge then stamps again.
EVENT_STAGE: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            *(c for c in EVENT_CANDIDATE_COLUMNS if c not in _SHADOW_ONLY),
            *EVENT_FACT_COLUMNS,
            *EVENT_PROVENANCE_COLUMNS,
        )
    )
)

#: ``alphalens thematic score`` — the Layer 4 signal composition.
SCORE_STAGE: tuple[str, ...] = SCORE_COLUMNS

#: ``alphalens thematic brief`` — the LLM prose, the guard telemetry and the
#: day's ranking. A quiet day carries ``EMPTY_BRIEF_COLUMNS`` instead.
BRIEF_STAGE: tuple[str, ...] = BRIEF_STAGE_COLUMNS

#: The seven families that widen an already-scored frame, each declared and
#: enforced at its own writer. Ordered widest-first so the assembled registry
#: reads in descending contribution.
ENRICHER_STAGES: tuple[tuple[str, ...], ...] = (
    OPTIONS_COLUMNS,
    ONEIL_COLUMNS,
    MARKET_STATE_COLUMNS,
    QUAL_COLUMNS,
    BUFFETT_COLUMNS,
    SI_COLUMNS,
    PANEL_COLUMNS,
)

_ALL_STAGES: tuple[tuple[str, ...], ...] = (
    MAP_STAGE,
    SCORE_STAGE,
    BRIEF_STAGE,
    EVENT_STAGE,
    *ENRICHER_STAGES,
)

#: Every column name a brief parquet can carry. Deduplicated with
#: ``dict.fromkeys`` rather than a set so the order is reproducible across
#: interpreter runs: stages in pipeline order, columns in each stage's own
#: declared order, first occurrence winning. Columns shared between stages
#: (the map stage and the event lane both supply the thematic identity
#: columns) therefore appear once, at their earliest writer.
BRIEF_STORE_COLUMNS: tuple[str, ...] = tuple(
    dict.fromkeys(column for stage in _ALL_STAGES for column in stage)
)
