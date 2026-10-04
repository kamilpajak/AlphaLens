"""Every column of the brief store is declared by the stage that computes it.

The brief parquet under ``thematic_briefs/`` is written by four stages in
sequence — map-themes, the event merge, score, brief — and then widened by
seven enricher families. Before this change, 46 of its 178 names existed only
as string literals inside the function that happened to write them: the score
stage's 42, the three day-ranking columns, and ``event_overlap``. A rename
there changed the stored schema with nothing in the tree to notice.

These tests close that by running each stage for real over frozen fixtures and
comparing the column names it produces against the tuple that stage now
publishes. Four further assertions, the whole of
``TestRegistryCoversRealData``, are about the ASSEMBLED registry in
:mod:`alphalens_pipeline.brief_contract.columns` rather than about one stage:

* the columns of a replayed brief frame must be a SUBSET of
  ``BRIEF_STORE_COLUMNS``;
* the two shadow-only detector names must be absent from both ``EVENT_STAGE``
  and the registry;
* ``BRIEF_STORE_COLUMNS`` must be EXACTLY the union of the thirteen owner
  declarations (``_OWNER_DECLARATIONS``) minus those two shadow-only names;
* every published stage grouping must be a subset of the registry.

The union equality is the one that bounds the registry from above; every other
assertion in this file only bounds it from below.

Every comparison here that replays or builds a frame carries a row-count
control beside it, because a set equality over an empty frame can be true and
empty. The class docstring of ``TestStageDeclaresWhatItWrites`` records which
four of them held green on a forced empty frame before those controls existed.

WRITING LITERALS IS WHAT GIVES THIS GATE POWER
----------------------------------------------
A stage can make its tuple load-bearing by unpacking it into the writer, or by
iterating it to stamp the frame. That INVERTS the gate: the stored column name
then follows the declaration, so renaming an entry renames the production
column and no assertion below can fail. Five tuples in the tree are in that
position today — ``EVENT_FACT_COLUMNS``, ``EVENT_CANDIDATE_COLUMNS``,
``ONEIL_COLUMNS``, ``BUFFETT_COLUMNS`` and ``OPTIONS_COLUMNS`` — and the gate
is measurably blind to all five; the arithmetic is below.

THREE of the four stages re-measured here write their columns as LITERALS and
READ their tuple only to check the frame, warning through
``brief_contract.validation.warn_on_column_disagreement`` when the two
disagree: ``score_candidates``, ``merge_event_candidates`` (inside
``_warn_if_stamps_disagree``) and ``_sort_and_dedup_for_brief``. Counted, not
assumed — ``grep -rn warn_on_column_disagreement apps/ --include "*.py" |
grep -v /tests/`` prints nine lines: three imports and three calls across
those three stage modules, plus the definition, the ``__all__`` entry and one
docstring reference inside ``validation.py``. None of them is in the mapping
orchestrator.

THE MAP STAGE IS THE FOURTH AND IT DIFFERS IN BOTH HALVES. It routes nothing
through the shared emitter and makes no runtime check of the candidates frame
against ``MAP_THEMES_COLUMNS`` at all, so the equality below is the only thing
comparing the two. (The mapping orchestrator does carry two column warnings of
its own, but they are about other frames: the proposal-funnel and
theme-decision SIDECAR parquets, each against its own tuple, and each followed
by a ``reindex`` that imposes the tuple on the frame — the repair-instead-of-
report shape ``validation.py`` exists to avoid.)

And the map stage does not only write literals — on two branches its tuple IS
the writer. ``map_themes`` falls back to
``pd.DataFrame(columns=list(MAP_THEMES_COLUMNS))`` when every candidate is
dropped, and ``write_empty_candidates`` builds the quiet-day parquet the same
way. On a day that publishes candidates the frame comes from the row dicts the
mapper assembled, so the equality is a real claim about the data; on those two
branches it compares the tuple against a frame built from the tuple. That is
why the map assertion carries an existence control, and the class docstring of
``TestStageDeclaresWhatItWrites`` records the measurement.

WHAT THIS GATE DOES NOT PROTECT
-------------------------------
It never reads ``~/.alphalens``, and that is MEASURED, not asserted. Counting
every ``builtins.open``, ``pandas.read_parquet``, ``pathlib.Path.glob`` and
``pathlib.Path.exists`` whose path lies under ``~/.alphalens`` while this
module runs gives 0. It gave 78 before this change, and the four existing
golden map tests gave 350, because two readers of the operator's live
``thematic_events`` store were never frozen: ``map_themes`` ends by calling
``proposal_shadow.write_proposal_shadow``, and ``score_candidates`` calls
``catalyst_resolver.build_template_entity_index``. Each took its own
``events_dir`` default. Both are now frozen onto the fixture window in
``tests/golden/map_fixtures.py`` and ``tests/golden/test_golden_score_replay.py``,
and both projections are byte-identical to the committed goldens before and
after. Re-measure with a probe rather than trusting this paragraph: no test in
this file can observe the difference (measured — removing either freeze kills
nothing).

Taking inputs from fixtures alone is a deliberate limit, not an omission: the
operator's store holds many dates of many widths, so a gate phrased against it
would assert whatever the newest file happened to contain. The only guard in
this repo against a test touching live operator state lives in
``tests/brokers/__init__.py``, and a test under ``tests/thematic/`` inherits
none of it.

It does not re-measure the seven enricher families — Buffett quant and qual,
O'Neil, the panel, market state, options telemetry and short interest, 53
names between them. Three of the seven (``ONEIL_COLUMNS``,
``BUFFETT_COLUMNS``, ``OPTIONS_COLUMNS``) are iterated by their own writer, so
they are in the inverted position and a rename changes production. The other
four (``QUAL_COLUMNS``, ``SI_COLUMNS``, ``PANEL_COLUMNS``,
``MARKET_STATE_COLUMNS``) are read by no writer at all. Either way this file
has no power over them: measured, renaming one entry in each of the seven left
every test in THIS file green, while at least one test in the family's own
module went red every time. Per-family counts are deliberately not quoted
here. They are not produced by one rule — five families are counted in their
own enrichment module while ``QUAL_COLUMNS`` and ``PANEL_COLUMNS`` only reach
their published figure by counting a second module — and the count depends on
which entry is renamed (``OPTIONS_COLUMNS`` ranges 3 to 10 across its 16
entries, ``MARKET_STATE_COLUMNS`` 4 to 5 across its 8). The qualitative claim
is the one that holds under every entry: this file is blind to all 53, and
``SI_COLUMNS`` is the thinnest pinned elsewhere, by a single empty-frame dtype
test. The registry imports those tuples and asserts membership only.

The containment assertion does not reach them either, and the reason is the
fixture, not the assertion. ``brief_day/scored.parquet`` carries 89 columns and
none of them is an ``oneil_``, ``buffett_``, ``options_`` or ``event_`` name,
so the replayed brief frame cannot contain an enricher column at all. Widening
that fixture is how the reach would grow, and it is not part of this change.

A renamed column still reaches the Django API as a silent ``None``. The
serializers and models in ``apps/alphalens-django`` name these columns by
string literal, and that project does not depend on ``alphalens-pipeline``, so
nothing there can import the tuples this gate pins. If ``technical_rsi``
becomes ``technical_rsi_v2``, this gate goes red and the API keeps serving the
field as null until someone edits the Django side by hand. Closing that half
needs a parity test on the Django side — the precedent is
``tests/test_expert_column_parity.py``, which ast-parses the Django mirror as
data — and it is not part of this change.

WHERE THE DECLARATION IS THE WRITER, THIS GATE HAS NO POWER
-----------------------------------------------------------
One rule covers several of the gaps below, so it is stated once. When a tuple
is ITERATED to stamp the columns, the declaration and the stored name are the
same fact; there is no divergence for a gate to catch, and renaming an entry
changes production with nothing red anywhere. The honest arithmetic over the
178 names, counted this session:

* 114 — the gate has RENAME power. These are the names of
  ``MAP_THEMES_COLUMNS`` (47), ``SCORE_COLUMNS`` (42),
  ``BRIEF_STAGE_COLUMNS`` (23) and ``EVENT_PROVENANCE_COLUMNS`` (2), each
  compared by EQUALITY against a frame the stage really produced. Renaming
  either side alone turns this file red (measured on one name per stage).
* 42 — the declaration IS the writer. ``EVENT_FACT_COLUMNS`` (9, stamped by
  ``for col in EVENT_FACT_COLUMNS``), the two names
  ``EVENT_CANDIDATE_COLUMNS`` contributes that no earlier stage supplies
  (``event_sic``, ``event_next_earnings_date``; the detector builds its whole
  output frame from that tuple), and the three iterated enricher families
  ``OPTIONS_COLUMNS`` (16), ``ONEIL_COLUMNS`` (9) and ``BUFFETT_COLUMNS`` (6).
* 22 — declared and read by nobody: ``MARKET_STATE_COLUMNS`` (8),
  ``QUAL_COLUMNS`` (8), ``SI_COLUMNS`` (4), ``PANEL_COLUMNS`` (2).

The three buckets are disjoint and sum to 178. They are per
(name, declaration) pair: a name declared twice is counted once, at its
strongest position.

ONE CAVEAT THE SUM HIDES, AND IT IS BIGGER THAN ONE NAME. 22 of the 114 names
in the first bucket are declared a SECOND time, in
``EVENT_CANDIDATE_COLUMNS``. The detector builds its whole output frame with
``pd.DataFrame(rows, columns=list(EVENT_CANDIDATE_COLUMNS))``, so for those 22
the stored name comes FROM that tuple. The gate's power over them covers their
earlier-stage writer and NOT the detector: rename one of the 22 in
``EVENT_CANDIDATE_COLUMNS`` alone and the event lane stores the new name with
nothing in this file red. So "114 names have rename power" means "each is
protected at one declaration", and for 22 of them there is a second
declaration this gate does not reach.

That 22 is NOT the third bucket's 22. The two are unrelated sets of the same
size — the third bucket is the four enricher families no writer reads, this one
is names the detector re-declares — and the coincidence is worth stating
because the numbers sit a paragraph apart.

The split of this 22 by earlier-stage owner: 21 belong to
``MAP_THEMES_COLUMNS`` (``ticker``, ``theme``, ``company_name``,
``market_cap``, ``llm_confidence``, ``rationale``, ``verified``,
``theme_search_keywords``, the three ``source_event_*`` and the ten gate
columns — ``gate_verdict_json``, the six ``gates_*`` and the three
``n_gates_*``) and one to ``EVENT_PROVENANCE_COLUMNS`` (``source``). Counted
this session:

    power = (set(MAP_THEMES_COLUMNS) | set(SCORE_COLUMNS)
             | set(BRIEF_STAGE_COLUMNS) | set(EVENT_PROVENANCE_COLUMNS))
    len(power & set(EVENT_CANDIDATE_COLUMNS))  # -> 22

The same intersection against ``EVENT_FACT_COLUMNS`` and against each of the
seven enricher tuples is EMPTY, so the detector is the only place in the tree
where a gated name carries a second declaration.

Five further gaps, named rather than papered over, each measured.

The quiet-day path is not measured here: this file measures the published-day
frame, and ``EMPTY_BRIEF_COLUMNS`` stays pinned by its existing consumer in
``tests/thematic/argumentation/test_support_guard_wiring.py``.

A COORDINATED rename — writer and tuple changed in one edit — passes at the
map and brief stages. That is correct for a registry gate, and it is exactly
where the Django residual above lives. It does NOT pass at the score stage:
the containment assertion replays briefs over the frozen
``brief_day/scored.parquet``, which still carries the OLD name, so the frame
then holds a column the registry no longer lists. Measured on
``technical_rsi``, renamed in both the tuple and ``_build_candidate_row``:
``FAILED (failures=1)``, the one failure being
``test_an_observed_brief_frame_is_covered_by_the_registry``.

The maintenance consequence follows and is easy to mistake for a broken gate:
renaming a score column also requires re-recording that fixture. Until it is
re-recorded the gate is red, and the failure is about the fixture, not about
the rename being wrong.

``EVENT_FACT_COLUMNS`` (9 names) is written by iterating the tuple
(``for col in EVENT_FACT_COLUMNS: out[col] = None``, and ``ev.get(col)`` for
the overlap copy), so renaming an entry renames the stamped column and also
the key looked up in the detector's row: the fact would arrive as ``None``
with nothing red. Measured: renaming ``event_gate_version`` in that tuple
killed no test in this file.

``EVENT_CANDIDATE_COLUMNS`` (35 names declared, 33 of them in the registry) is
the same inversion one layer up, and it is the larger of the two: the detector
builds its output frame with
``pd.DataFrame(rows, columns=list(EVENT_CANDIDATE_COLUMNS))``. Measured:
renaming ``event_sic`` in that tuple killed no test in this file. This gate
cannot close either one, and no subset assertion against the tuple would —
that would take the tuple as ground truth about the data when the tuple is the
thing in question. Closing them needs literals in the detector and the merge,
which is a change to production code and not part of this one.

A stage that stops CALLING ``warn_on_column_disagreement`` loses its operator
signal silently. The positive control below exercises the shared emitter, not
each stage's wiring to it: measured, deleting the call outright in
``scorer.py``, in ``merge.py`` or in the brief orchestrator left every test
here green, in all three cases. The set comparisons still catch the drift
itself; only the live log line goes quiet. An AST gate over the source is the
shape that would catch a missing caller — the precedent is ``tests/brokers``
on missing journal writers.

Assertions are on column NAMES only, never dtypes. 24 of the 42 score columns
change dtype between a data-rich day and a starved one (``technical_rsi``
goes float64 -> object when every fetch comes back empty), so a dtype clause
would go red on any thin-data day without a schema having moved.
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.brief_contract.columns import (
    BRIEF_STAGE,
    BRIEF_STORE_COLUMNS,
    ENRICHER_STAGES,
    EVENT_STAGE,
    MAP_STAGE,
    SCORE_STAGE,
)
from alphalens_pipeline.brief_contract.validation import warn_on_column_disagreement
from alphalens_pipeline.events.insider_cluster_detect import EVENT_CANDIDATE_COLUMNS
from alphalens_pipeline.events.merge import (
    EVENT_FACT_COLUMNS,
    EVENT_PROVENANCE_COLUMNS,
    merge_event_candidates,
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

from tests.golden.map_fixtures import MAP_FIXTURES
from tests.golden.test_golden_brief_replay import _FIXTURES as _BRIEF_FIXTURES
from tests.golden.test_golden_brief_replay import _replay_briefs
from tests.golden.test_golden_map_characterization import _replay_map
from tests.golden.test_golden_score_replay import _FIXTURES as _SCORE_FIXTURES
from tests.golden.test_golden_score_replay import _replay_score

_SCORE_INPUT_PARQUET = _SCORE_FIXTURES / "candidates.parquet"
_BRIEF_INPUT_PARQUET = _BRIEF_FIXTURES / "scored.parquet"

# A DELIBERATE second copy of ``events.merge.EVENT_SHADOW_ONLY_COLUMNS``.
#
# That tuple has three jobs at once: it is the drop list in the merge, the
# subtraction set in ``brief_contract.columns``, and — until this constant
# existed — the expected set of the two tests below. Importing it for the
# third job made the thing under test its own oracle. Measured on the
# imported form: renaming ``"eligible"`` to ``"eligibl"`` in the merge let
# the detector's ``eligible`` column through into the merge output (34 -> 35
# columns), widened the registry 178 -> 179 to absorb it, and killed no test.
# Writing the two names here instead means a rename of the production tuple
# makes the test DISAGREE rather than follow along.
#
# The cost of the second copy is the usual one: a DELIBERATE addition to the
# production tuple must be mirrored here, and the union test's failure message
# names this constant so the next reader knows where to look. Measured on
# ``event_sic``: ``Items in the first set but not the second: 'event_sic'``
# followed by that message.
#
# One limit of that, also measured: it only fires for a name NO OTHER STAGE
# supplies. Adding ``verified`` to the production tuple passes silently,
# because the map stage declares it too and the registry subtracts the
# shadow-only names from the detector's contribution alone.
_SHADOW_ONLY_COLUMNS: tuple[str, ...] = ("eligible", "exclusion_reason")

# Every stage declaration, imported from the module that OWNS it rather than
# from the registry that assembles them. The union of these, minus the two
# shadow-only names above, is a derivation of ``BRIEF_STORE_COLUMNS`` that
# shares no expression with ``columns._ALL_STAGES``.
_OWNER_DECLARATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("MAP_THEMES_COLUMNS", MAP_THEMES_COLUMNS),
    ("SCORE_COLUMNS", SCORE_COLUMNS),
    ("BRIEF_STAGE_COLUMNS", BRIEF_STAGE_COLUMNS),
    ("EVENT_CANDIDATE_COLUMNS", EVENT_CANDIDATE_COLUMNS),
    ("EVENT_FACT_COLUMNS", EVENT_FACT_COLUMNS),
    ("EVENT_PROVENANCE_COLUMNS", EVENT_PROVENANCE_COLUMNS),
    ("OPTIONS_COLUMNS", OPTIONS_COLUMNS),
    ("ONEIL_COLUMNS", ONEIL_COLUMNS),
    ("MARKET_STATE_COLUMNS", MARKET_STATE_COLUMNS),
    ("QUAL_COLUMNS", QUAL_COLUMNS),
    ("BUFFETT_COLUMNS", BUFFETT_COLUMNS),
    ("SI_COLUMNS", SI_COLUMNS),
    ("PANEL_COLUMNS", PANEL_COLUMNS),
)

# A two-column thematic frame is enough for the merge assertions: the stage
# stamps its own columns onto whatever it is handed, and a narrow input makes
# "what did this stage add" a plain set difference. ``verified`` is required
# because the merge casts it at the end.
_THEMATIC_TICKER = "AAA"
_EVENT_ONLY_TICKER = "BBB"


def _minimal_thematic_frame() -> pd.DataFrame:
    return pd.DataFrame({"ticker": [_THEMATIC_TICKER], "verified": [True]})


def _no_eligible_events() -> pd.DataFrame:
    """An event frame with no rows: the merge returns after stamping."""
    return pd.DataFrame(columns=list(EVENT_CANDIDATE_COLUMNS))


def _one_eligible_event(ticker: str) -> pd.DataFrame:
    """One eligible event row carrying every detector column.

    An eligible row is what drives the two branches a zero-row frame leaves
    unexecuted: the overlap loop when its ticker is already thematic, and the
    ``drop(columns=EVENT_SHADOW_ONLY_COLUMNS)`` append when it is not.
    """
    row: dict[str, object] = dict.fromkeys(EVENT_CANDIDATE_COLUMNS)
    row["ticker"] = ticker
    row["verified"] = True
    row["eligible"] = True
    return pd.DataFrame([row], columns=list(EVENT_CANDIDATE_COLUMNS))


class TestStageDeclaresWhatItWrites(unittest.TestCase):
    """Each stage's published tuple equals the names that stage really writes.

    EVERY EQUALITY HERE CARRIES AN EXISTENCE CONTROL
    ------------------------------------------------
    A set equality over a frame with no rows can be TRUE AND EMPTY, so each
    assertion below first asserts that the frame it is about carries rows.
    This is not symmetry for its own sake: measured, four of the seven
    assertions in this file hold GREEN once their stage is forced onto an
    empty frame, because on that path the frame's columns come from the very
    tuple under test.

    The map stage is the worst of the four. When every candidate is dropped,
    ``map_themes`` returns ``pd.DataFrame(columns=list(MAP_THEMES_COLUMNS))``,
    so the equality compares the tuple against a frame BUILT from the tuple
    and the 47 map names — the largest block in the rename-power bucket —
    stop being measured with nothing red. Measured by patching
    ``_rows_for_theme`` to drop every row: the replay returned 0 rows and 47
    columns, the equality alone passed for both fixtures, and the control
    turned both red ("replay produced no rows").

    The other three false greens, and the three assertions that need no help,
    all measured by the same forcing:

    * the merge with NO ELIGIBLE EVENTS, handed a zero-row thematic frame —
      GREEN, because stamping a column needs no rows. Now ``1 != 0``.
    * the merge with an APPENDED EVENT, same zero-row thematic frame — GREEN
      at one row instead of two, so the append it is named after never
      happened. Now ``2 != 1``.
    * the containment assertion in ``TestRegistryCoversRealData`` — GREEN over
      the quiet-day brief frame (0 rows, 23 columns, every one of them listed
      in the registry).
    * the merge with an OVERLAPPING EVENT goes red on its own: with no
      thematic row to overlap, the event is appended instead and brings the
      detector's columns with it.
    * the SCORE stage returns ``candidates.copy()``, so it adds no column at
      all and the equality is already red on all 42 score names.
    * the BRIEF stage returns the ``EMPTY_BRIEF_COLUMNS`` frame, already red
      on the three day-ranking names (``also_in_themes``, ``rank_in_day``,
      ``cohort_size_in_day``).

    The controls on those last three are guards, not live catches, and they
    are written anyway: whether an empty frame fails the equality is a
    property of today's quiet-day schemas, not of the assertion. The
    precedent for the shape is
    ``tests/golden/test_golden_map_characterization.py``, which asserts its
    golden ``row_count`` is above zero for exactly this reason.
    """

    def test_map_stage_declares_what_it_writes(self):
        for fixture in MAP_FIXTURES:
            with self.subTest(fixture=fixture.name), tempfile.TemporaryDirectory() as td:
                # Arrange / Act
                produced = _replay_map(fixture, Path(td))

                # Assert — existence control first: the all-candidates-dropped
                # branch returns a frame built FROM MAP_THEMES_COLUMNS, which
                # satisfies the equality and measures nothing.
                self.assertFalse(produced.empty, f"{fixture.name} replay produced no rows")
                self.assertEqual(set(MAP_THEMES_COLUMNS), set(produced.columns))

    def test_event_merge_declares_what_it_stamps_with_no_eligible_rows(self):
        # Measured redundant: this case and the overlapping one below never
        # died except alongside the appended-event case, which is the only
        # one that executes the shadow-only drop. They are kept because each
        # names a different EXIT PATH of the stage, so a failure says which
        # path broke; neither carries power of its own.
        #
        # Arrange
        thematic = _minimal_thematic_frame()

        # Act
        merged = merge_event_candidates(thematic, _no_eligible_events())
        stamped = set(merged.columns) - set(thematic.columns)

        # Assert — the thematic row must survive. Measured: handing this
        # stage a zero-row thematic frame leaves the column equality GREEN,
        # because stamping a column needs no rows.
        self.assertEqual(1, len(merged))
        self.assertEqual(set(EVENT_PROVENANCE_COLUMNS) | set(EVENT_FACT_COLUMNS), stamped)

    def test_event_merge_declares_what_it_stamps_for_an_overlapping_event(self):
        # Arrange — the event ticker is already on the thematic list, so the
        # overlap loop runs and nothing is appended.
        thematic = _minimal_thematic_frame()

        # Act
        merged = merge_event_candidates(thematic, _one_eligible_event(_THEMATIC_TICKER))
        stamped = set(merged.columns) - set(thematic.columns)

        # Assert — exactly one row, which is also what says the event was
        # folded into the thematic row instead of appended beside it.
        self.assertEqual(1, len(merged))
        self.assertEqual(set(EVENT_PROVENANCE_COLUMNS) | set(EVENT_FACT_COLUMNS), stamped)

    def test_event_merge_declares_what_it_stamps_for_an_appended_event(self):
        # Arrange — a ticker the thematic lane does not carry, so the row is
        # appended and brings the detector's own columns in with it. The two
        # shadow-only names must not be among them.
        thematic = _minimal_thematic_frame()
        detector_columns = set(EVENT_CANDIDATE_COLUMNS) - set(_SHADOW_ONLY_COLUMNS)
        expected = (
            set(EVENT_PROVENANCE_COLUMNS)
            | set(EVENT_FACT_COLUMNS)
            | (detector_columns - set(thematic.columns))
        )

        # Act
        merged = merge_event_candidates(thematic, _one_eligible_event(_EVENT_ONLY_TICKER))
        stamped = set(merged.columns) - set(thematic.columns)

        # Assert — two rows: the thematic one plus the appended event. The
        # count is what makes the append observable; measured, a zero-row
        # thematic frame leaves the column equality green at one row.
        self.assertEqual(2, len(merged))
        self.assertEqual(expected, stamped)

    def test_score_stage_declares_what_it_writes(self):
        # Arrange
        candidates = pd.read_parquet(_SCORE_INPUT_PARQUET)

        # Act
        scored = _replay_score()
        added = set(scored.columns) - set(candidates.columns)

        # Assert — the control is a guard, not a live catch: measured, this
        # stage's empty branch (``return candidates.copy()``) already turns
        # the equality red, because it adds no column at all and all 42 score
        # names come back missing.
        self.assertFalse(scored.empty, "score replay produced no rows")
        self.assertEqual(set(SCORE_COLUMNS), added)

    def test_brief_stage_declares_what_it_writes(self):
        # Arrange
        scored = pd.read_parquet(_BRIEF_INPUT_PARQUET)

        # Act
        with tempfile.TemporaryDirectory() as td:
            briefs = _replay_briefs(Path(td))
        added = set(briefs.columns) - set(scored.columns)

        # Assert — as with the score stage, a guard rather than a live catch:
        # measured, the quiet-day branch returns the ``EMPTY_BRIEF_COLUMNS``
        # frame, which already fails this equality on the three day-ranking
        # names. The control keeps that from becoming a false green if the
        # quiet-day schema is ever widened to match a published day.
        self.assertFalse(briefs.empty, "brief replay produced no rows")
        self.assertEqual(set(BRIEF_STAGE_COLUMNS), added)


class TestRegistryCoversRealData(unittest.TestCase):
    """The assembled registry is checked against an observed frame, not itself."""

    def test_an_observed_brief_frame_is_covered_by_the_registry(self):
        # The only assertion in this file that compares the ASSEMBLED
        # registry against OBSERVED DATA, and the only one with unique power
        # over a COORDINATED rename. Renaming a score column in both the
        # tuple and the writer kills this test ALONE: the stage's own set
        # comparison agrees with itself again, while the frozen
        # ``scored.parquet`` still carries the old name. Measured on
        # ``technical_rsi`` -> ``FAILED (failures=1)``, this test.
        #
        # Against a one-sided mutation it has no power of its own: across 33
        # such mutations it never died alone, dying four times alongside the
        # stage comparison it duplicates. The other thing only it would catch
        # is a column reaching a real brief frame that no stage declares at
        # all, which no mutation of a declaration can manufacture.
        #
        # Arrange / Act — 112 columns: the scored fixture's 89 plus the brief
        # stage's own, replayed offline.
        with tempfile.TemporaryDirectory() as td:
            briefs = _replay_briefs(Path(td))

        # Assert — existence control first. A containment claim over a frame
        # with no rows says nothing about a real brief, and measured, this
        # assertion holds GREEN over the quiet-day frame (0 rows, 23 columns,
        # all of them in the registry).
        self.assertFalse(briefs.empty, "brief replay produced no rows")
        self.assertEqual(set(), set(briefs.columns) - set(BRIEF_STORE_COLUMNS))

    def test_the_registry_excludes_the_detector_shadow_only_columns(self):
        # Arrange — ``eligible`` and ``exclusion_reason`` are declared by the
        # detector and dropped by the merge, so they are the two names
        # EVENT_STAGE must subtract out of EVENT_CANDIDATE_COLUMNS. Named by
        # the test's own literal, not by the production drop list: see
        # ``_SHADOW_ONLY_COLUMNS``.
        shadow_only = set(_SHADOW_ONLY_COLUMNS)

        # Act / Assert — measured: deleting that subtraction widens the
        # registry from 178 names to 180, and kills this test and the union
        # equality. Those two are the only things holding it.
        self.assertEqual(set(), shadow_only & set(EVENT_STAGE))
        self.assertEqual(set(), shadow_only & set(BRIEF_STORE_COLUMNS))

    def test_the_registry_is_exactly_the_union_of_the_owner_declarations(self):
        """Nothing enters ``BRIEF_STORE_COLUMNS`` that no stage declares.

        Every other assertion in this file points the same way — a stage is a
        SUBSET of the registry, a frame is a SUBSET of the registry — so a
        name added to the registry's fold and written by nobody used to pass
        the whole file. This is the only assertion that bounds it from above.

        The expected set is derived from the thirteen declarations imported
        DIRECTLY from the modules that own them (``_OWNER_DECLARATIONS``),
        minus the test's own ``_SHADOW_ONLY_COLUMNS`` literal. It shares no
        expression with ``columns._ALL_STAGES``: that fold goes through the
        registry's six re-exported stage groupings and its own subtraction,
        this one goes through the owner modules. Measured on the 13 tests this
        module holds: adding a literal ``"ghost_column"`` to the registry's
        fold turns this test red and leaves the other TWELVE green
        (``Ran 13 tests`` / ``FAILED (failures=1)``).
        """
        # Arrange
        expected = {
            column for _name, declaration in _OWNER_DECLARATIONS for column in declaration
        } - set(_SHADOW_ONLY_COLUMNS)

        # Act / Assert
        self.assertEqual(
            expected,
            set(BRIEF_STORE_COLUMNS),
            "registry disagrees with the owner declarations; if a shadow-only "
            "column was added to events.merge.EVENT_SHADOW_ONLY_COLUMNS, "
            "mirror it into _SHADOW_ONLY_COLUMNS in this module",
        )

    def test_every_published_stage_grouping_reaches_the_registry(self):
        # Kept for its FAILURE MESSAGE, not for power of its own: measured
        # redundant against the union equality above, which it can no longer
        # die without. What it adds is the name of the stage whose
        # declaration the assembly dropped, which a set difference over 178
        # names does not say.
        #
        # Arrange
        registry = set(BRIEF_STORE_COLUMNS)
        stages: list[tuple[str, tuple[str, ...]]] = [
            ("MAP_STAGE", MAP_STAGE),
            ("SCORE_STAGE", SCORE_STAGE),
            ("BRIEF_STAGE", BRIEF_STAGE),
            ("EVENT_STAGE", EVENT_STAGE),
        ]
        stages += [(f"ENRICHER_STAGES[{i}]", s) for i, s in enumerate(ENRICHER_STAGES)]

        # Act / Assert — each stage is named separately so a failure says
        # WHICH declaration the assembly dropped.
        for name, stage in stages:
            with self.subTest(stage=name):
                self.assertEqual(set(), set(stage) - registry)


class TestColumnDisagreementIsReported(unittest.TestCase):
    """Positive control for the operator signal all three stages route through.

    The set comparisons above pin what each stage writes. They say nothing
    about the warning a stage emits when it drifts, and that warning is the
    only thing an operator sees on a live run — so it gets a test that makes
    it fire on purpose.

    This replaces an earlier ``assertNoLogs`` over the whole scorer logger.
    That shape asserted the absence of a warning rather than the presence of
    one, and ``scorer.py`` has five other ``logger.warning`` sites, so any of
    them firing during the golden replay would have turned the gate red for
    an unrelated reason.
    """

    def test_a_disagreeing_frame_logs_the_missing_and_extra_columns(self):
        # Arrange — a stage that forgot to write "b" and wrote "z" instead.
        log = logging.getLogger("tests.brief_contract.positive_control")

        # Act
        with self.assertLogs(log, level="WARNING") as captured:
            missing, extra = warn_on_column_disagreement(
                log,
                writer="a_stage",
                declaration="A_STAGE_COLUMNS",
                produced=["a", "z"],
                declared=("a", "b"),
            )

        # Assert
        self.assertEqual((["b"], ["z"]), (missing, extra))
        self.assertEqual(1, len(captured.output))
        self.assertIn("a_stage columns disagree with A_STAGE_COLUMNS", captured.output[0])
        self.assertIn("missing=['b']", captured.output[0])
        self.assertIn("extra=['z']", captured.output[0])

    def test_a_generator_of_produced_columns_reports_the_whole_drift(self):
        # ``produced`` is consumed twice inside the emitter — once for the
        # membership set, once for the ordered ``extra`` list. Before the
        # ``list()`` on its first line, a generator argument was exhausted by
        # the first pass and the warning reported a drift as HALF a drift:
        # missing=['b'] with extra=[] (measured). Every call site today passes
        # a list comprehension, so this case exists to keep the emitter total
        # over any iterable rather than to reproduce a live bug.
        #
        # Arrange — the same drift as the case above, handed over lazily.
        log = logging.getLogger("tests.brief_contract.positive_control")

        # Act
        with self.assertLogs(log, level="WARNING") as captured:
            missing, extra = warn_on_column_disagreement(
                log,
                writer="a_stage",
                declaration="A_STAGE_COLUMNS",
                produced=(column for column in ("a", "z")),
                declared=("a", "b"),
            )

        # Assert
        self.assertEqual((["b"], ["z"]), (missing, extra))
        self.assertIn("extra=['z']", captured.output[0])

    def test_an_agreeing_frame_logs_nothing(self):
        # Arrange
        log = logging.getLogger("tests.brief_contract.positive_control")

        # Act
        with self.assertNoLogs(log, level="WARNING"):
            missing, extra = warn_on_column_disagreement(
                log,
                writer="a_stage",
                declaration="A_STAGE_COLUMNS",
                produced=["b", "a"],
                declared=("a", "b"),
            )

        # Assert
        self.assertEqual(([], []), (missing, extra))


if __name__ == "__main__":
    unittest.main()
