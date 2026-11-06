"""One definition of the ticker-episode panel built from the selection-label store.

Four scripts under ``scripts/ml/`` each carried their own copy of the same chain:
read the label store, join it to ``thematic_briefs`` on (brief_date, ticker),
keep the resolved rows, keep the population, collapse same-ticker rows into
episodes and label each episode with its arrival session. The copies had already
drifted in ways that change a number rather than raise:

* one joined a native ``dt.date`` ``brief_date`` against a key synthesised from
  the brief parquet's filename (a brief parquet carries no ``brief_date`` column
  at all), and matched zero rows while returning a clean-looking empty frame;
* one applied the maturity-status filter AFTER the episode collapse, so an
  episode was represented by a row that the filter then dropped;
* none of the four pinned ``sel_label_version``, so two label definitions could
  pool into one panel with nothing said.

This module is that chain, once. Every pre-registered choice stays the caller's,
and :class:`PanelSpec` has no default on any of its eleven fields. The reason is
not the same for all of them: the cutoff, the population and the status column
are pre-registration decisions, and the status column's name carries a horizon,
so a default would put a second copy of a frozen choice here. The
resolved-status token, the anchor column and the arrival dtype each have exactly
one owner in the pipeline tier already, so they are the caller's for symmetry
rather than because a default would duplicate anything.

Two frozen choices this module does not expose, which is worth stating rather
than leaving to be discovered: the episode collapse is called bare, so it takes
``ticker_episode_dedup``'s own defaults — a chained FIVE-session window on
``XNYS``. The five sessions are ledger rule 5's definition of a ticker-episode.
They are not promoted to the spec because all four copies call it bare too, so
the unit here is the unit every recorded number was computed with, and the two
cases in ``TestTheUnitIsEpisodesNotRows`` pin the window from both sides — a
near repeat is one episode, a distant repeat is two.

Outcome blindness
-----------------
There are two public builders, never one with a boolean. The blind one takes no
``outcome`` argument at all, so there is no value a caller could pass to receive
one and no keyword a ``**kwargs`` dict could flip. Blindness is held in three
layers:

1. **Signature and allowlist.** The blind path's label columns come from
   :func:`blind_label_columns`, built from :data:`BLIND_STRUCTURE_COLUMNS` plus
   the structural columns the spec names. An allowlist rather than a denylist:
   a ``sel_ar_*`` denylist would still let a held-out SIGNAL column through, and
   the held-out dependence structure is itself a look. The allowlist is
   caller-extensible in exactly one place — a ``version_pins`` key is appended
   to it — which is why a pin key must be a version column. Without that
   restriction a caller could name any label-store column, have the blind path
   read it, filter the panel on it and get it back, screened by nothing but the
   denylist this paragraph rejects as insufficient.
2. **Argument screening.** A name matching :data:`_OUTCOME_SHAPED` in
   ``brief_columns``, in the ``version_pins`` keys or in the spec's status and
   anchor columns is refused BY NAME, before any file is opened. That blocks
   reaching a second held-out label through the feature list, and the refusal
   has to arrive here rather than from layer 3: measured with this screen
   removed, two files were opened and the held-out column was read into memory
   before anything was raised.
3. **Result assertion.** No column of the blind frame may match that pattern.
   This is a tripwire against a future edit to the first two layers rather than
   a layer with a hole of its own. It catches a merge suffix (``sel_ar_20_y``),
   and through the public API it is reachable today only by widening the
   allowlist, which is what its test does. A brief store that one day gains an
   outcome column is caught by layer 1, because the blind path reads the brief
   side for the join key alone.

The non-ISO arrival guard
-------------------------
An unusable ``anchor_session`` is refused rather than dropped, and the reason is
counter-intuitive enough to state: ``.str.slice`` hands a missing value back as a
float NaN even after ``astype(str)``, and ``nunique()`` does not count a NaN. So
a bad anchor UNDER-COUNTS the arrival clusters while leaving the episode count
untouched, and a cluster figure measured against a frozen floor shrinks with
nothing raised. The guard was carried from ``a20_power.py``, where the same
paragraph says a bad anchor DELETES an episode — true there, because that chain
ends in ``groupby(...).size()`` and a NaN key drops a whole group. This chain
ends in ``len()`` and ``nunique()``, so the consequence is re-derived for it
rather than carried over. The guard is checked against the rows the panel KEEPS,
which is why it runs after the collapse: a bad anchor on a row the status
filter, the population filter or the collapse itself discards is not this
panel's problem.

What this module deliberately does NOT fix
------------------------------------------
``ticker_episode_dedup`` chains episodes on ``session_on_or_after(brief_date)``
while the ``arrival`` column here is the STORED ``anchor_session``. Owner
decision D4 says those two differ — measured on 296 of 466 burnt rows — so the
episode-chaining clock and the cluster labels come from two definitions of
arrival. Every panel in the repo has this property and every registered number
was computed with it. Reconciling them would move every panel, so it stays, and
it has its own issue.

No call site is migrated onto this loader, and for the two ``a20`` scripts that
is a decision rather than a delay. ``2026_09_a20_atr_confirmation.py`` IS the
#1227 registration — its docstring is the registered protocol — and ``a20_power.py``
produced the power figure that registration is being judged on. Their blind
paths can be diffed, because ``--preflight`` prints its panel. Their RUN paths
cannot: proving that ``held_out_panel`` still returns the same episodes would mean
reading held-out ``sel_ar_20`` values, which is the one-shot look itself. A
registered instrument must not be refactored on a proof that cannot be taken, so
both keep their copies until that look is spent.

The pin those copies lack is measured, not hypothetical, and today it costs
nothing: read on the production store 2026-11-06, ``sel_label_version`` holds one
value, ``sel-label-v3``, on all 2268 rows and on all 1767 held-out rows. So
requiring the pin changes no panel size today. The guard is for the day a second
definition appears, which is exactly the day nobody would notice.

The other two copies are not migrated either. ``2026_09_sel_ar_20_burnt_probe.py`` and
``2026_10_jev_discovery_burnt.py`` (a) keep ``arrival`` as a native ``dt.date``
and feed it to ``session_open_utc``, so the string form would change what that
call receives; (b) filter the cutoff on the ``brief_date`` COLUMN rather than the
parquet stem, and those two are the same filter only if one file holds one brief
date — which is why :func:`_read_labels` asserts exactly that; (c) raise
``AssertionError`` on a duplicated brief key where ``merge(validate="m:1")``
raises pandas' ``MergeError``; and (d) have never run, so there is no recorded
number to diff a migration against, and a migration with no before-number is not
verifiable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pandas as pd

#: Which side of the discovery cutoff a panel takes. The boundary is CLOSED on
#: the burnt side: the cutoff date itself is burnt, never held out.
SIDE_BURNT = "burnt"
SIDE_HELD_OUT = "held_out"

#: ``briefed`` keeps the rows the brief carried (``briefed_any_theme``); ``all``
#: applies no mask and so includes the LLM-proposed arm the label stamper also
#: writes. Which one a registration uses is a pre-registration decision.
POPULATION_BRIEFED = "briefed"
POPULATION_ALL = "all"

#: The label-store columns every panel needs, outcome or not. ``brief_date`` and
#: ``ticker`` are the join key, ``briefed_any_theme`` is a selection fact fixed
#: at brief time. The status and anchor columns are added from the spec, because
#: their names carry a horizon and must never be inferred from one.
BLIND_STRUCTURE_COLUMNS: tuple[str, ...] = ("brief_date", "ticker", "briefed_any_theme")

#: The join key, and the only brief-store column the blind path ever reads.
BRIEF_KEY_COLUMN = "ticker"

#: The column name the arrival session is returned under, on both paths.
ARRIVAL_COLUMN = "arrival"

#: A label value, by name shape. The stamper writes four families, not one:
#: ``sel_ar_h`` and ``sel_ar_inc_lo_hi``, the beta-scaled ``sel_zar_h``, and the
#: path mean ``sel_car_mean_20`` (``selection_label.VALUE_KEYS``). ``car_*``
#: covers the earlier event-window labels (``event_car.EVENT_CAR_COLUMNS``), a
#: family neither store this loader reads holds today. Used to screen caller
#: arguments and to assert the returned frame, never to decide what to read —
#: that is an allowlist. A name shape goes stale when a family is added, so
#: ``TestTheScreenCoversEveryStampedLabel`` pins it against ``VALUE_KEYS`` and
#: ``EVENT_CAR_COLUMNS`` rather than against these prefixes.
_OUTCOME_SHAPED = re.compile(r"^(sel_ar_|sel_zar_|sel_car_|car_)")

#: A version column, by name shape. The only caller-extensible part of the
#: blind allowlist is a ``version_pins`` key, so a key has to be one of these:
#: ``sel_label_version``, ``mapper_config_version`` and the like.
_VERSION_COLUMN = re.compile(r"[a-z0-9_]+_version")

#: An arrival session as the label store stamps it: a plain ISO date, exactly ten
#: characters. Measured 2026-09-24 on 1001 held-out rows, every one matched.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

#: How many offending values an error message quotes before it stops.
_SAMPLE_SIZE = 5

#: Length of an ISO date. The anchor is sliced to it before the ISO check, so an
#: anchor that one day carries a time does not split one session into several.
_ISO_DATE_LENGTH = 10


class PanelError(RuntimeError):
    """The panel cannot be built as asked.

    Raised for an empty join, a pin that matches nothing, a required pin left
    unset, an unusable arrival session and a missing column — that is, for store
    and argument defects. Callers map it onto their own VOID class, but the two
    are not the same thing: a registration VOIDs when there is not enough data
    yet, and this is raised when the data that is there cannot be read as asked.
    """


@dataclass(frozen=True)
class PanelSpec:
    """Everything the panel definition needs, all of it the caller's choice.

    No field has a default, and ``TestNoSpecFieldHasADefault`` asserts
    that over EVERY field rather than over a list of the interesting ones. The
    cutoff, the population and the horizon-carrying status column are
    pre-registered choices a default would put a second copy of here; the rest
    are the caller's for symmetry, so that one rule covers the whole spec.
    """

    #: The ``selection_labels`` store root.
    labels_dir: Path
    #: The ``thematic_briefs`` store root.
    briefs_dir: Path
    #: :data:`SIDE_BURNT` or :data:`SIDE_HELD_OUT`.
    side: Literal["burnt", "held_out"]
    #: The caller's frozen discovery cutoff, as ISO text. Compared against the
    #: parquet STEM, and asserted against the ``brief_date`` column.
    cutoff: str
    #: :data:`POPULATION_BRIEFED` or :data:`POPULATION_ALL`.
    population: Literal["briefed", "all"]
    #: e.g. ``"sel_label_status_20"``. Named, never derived from a horizon.
    status_column: str
    #: The one status value that means "this horizon resolved", e.g. ``"ok"``.
    resolved_status: str
    #: e.g. ``"anchor_session"``. READ, never recomputed from ``brief_date``.
    anchor_column: str
    #: True returns ``arrival`` as sliced ISO text (what the a20 family uses for
    #: clustering); False returns the stored value verbatim, which is what a
    #: caller passing it to a calendar helper needs.
    arrival_as_str: bool
    #: version column -> required value, applied BEFORE the episode collapse
    #: and before the status and population filters. A key must be a version
    #: column (``*_version``) and must not be a column the panel already reads
    #: as structure, because a pin key is appended to the blind read allowlist.
    #: The maturity status is filtered through :attr:`resolved_status`, never
    #: through a pin.
    version_pins: Mapping[str, str]
    #: Columns that MUST appear in :attr:`version_pins` or the build is refused.
    #: This is how a caller makes "two label definitions cannot pool" a
    #: structural fact rather than a thing someone remembered.
    require_pinned: frozenset[str]


def blind_label_columns(spec: PanelSpec) -> tuple[str, ...]:
    """Every label-store column the outcome-blind path may read, in read order.

    The allowlist, as a function of the spec rather than a literal set, because
    the status and anchor column names belong to the caller. A test can compare
    what was actually requested against this.

    Each column appears once. This function validates nothing itself, so a spec
    whose status or anchor column is also a pin key would otherwise put a name
    in the read list twice, and the build then died inside pandas with a message
    naming neither the pin nor the column.
    """
    return tuple(
        dict.fromkeys(
            (
                *BLIND_STRUCTURE_COLUMNS,
                spec.anchor_column,
                spec.status_column,
                *sorted(spec.version_pins),
            )
        )
    )


def episode_panel_blind(spec: PanelSpec) -> pd.DataFrame:
    """The panel's keys, status and population. Reads no outcome and no signal.

    Returns one row per ticker-episode with ``brief_date``, ``ticker``,
    ``arrival``, the population flag, the status column and any pinned version
    columns. The brief store is read for the JOIN KEY alone, so a brief feature
    cannot arrive here even by accident.
    """
    return _build(spec, outcome=None, brief_columns=())


def episode_panel_with_outcome(
    spec: PanelSpec,
    *,
    outcome: str,
    brief_columns: Sequence[str],
) -> pd.DataFrame:
    """The same panel plus one label column and the named brief features.

    Calling this on the held-out side of a registration IS the start of that
    look. ``outcome`` is the one label column this call may read. A row missing
    the outcome or any required brief column is dropped BEFORE the collapse, so
    an episode is represented by its first USABLE row.

    ``brief_columns`` lives here rather than on :class:`PanelSpec` on purpose:
    that makes "a blind caller cannot name a brief feature column" true by
    signature instead of by a runtime check. Every named column is required in
    every file: a column added to the brief store mid-history needs an optional
    read, and that has no caller yet, so it is not offered. A name no file
    carries is refused rather than returned as a null column.
    """
    return _build(spec, outcome=outcome, brief_columns=tuple(brief_columns))


def episode_counts(spec: PanelSpec) -> tuple[int, int]:
    """``(episodes, arrival clusters)``, outcome-blind.

    The blind panel keeps at least as many resolved ROWS as
    :func:`episode_panel_with_outcome` would on the same spec, by however many
    carry a null outcome or a null feature. The two COUNTS can move either way,
    though, and that is not an upper bound: dropping a row can UN-CHAIN an
    episode, so the outcome path can return MORE episodes and MORE clusters than
    this preflight does. Measured case — one ticker on sessions 0, 5 and 10 is
    one blind episode in one cluster and two outcome episodes in two clusters,
    because the middle row is what held the two ends inside one chain window.

    Zero is an absence of data, not a defect: it means no row on this side has
    matured or was briefed. It cannot be told apart from a mistyped
    :attr:`PanelSpec.resolved_status` token, because both look like "no row
    carries it". A store defect still raises — a pin that matches nothing, a key
    mismatch — because "the keys do not match" must not be reported as "not
    enough data yet".
    """
    panel = episode_panel_blind(spec)
    return len(panel), int(panel[ARRIVAL_COLUMN].nunique())


# ------------------------------------------------------------------ internals
def _screen_names(names: Sequence[str], *, role: str) -> None:
    """Refuse an outcome-shaped column name, by name, before anything is read."""
    offenders = [name for name in names if _OUTCOME_SHAPED.match(name)]
    if offenders:
        raise PanelError(
            f"{role} names an outcome-shaped column: {sorted(offenders)}. A label "
            f"value may only be read through the 'outcome' argument."
        )


def _validated(spec: PanelSpec) -> None:
    """Everything that can be refused without opening a file."""
    if spec.side not in (SIDE_BURNT, SIDE_HELD_OUT):
        raise PanelError(f"unknown side {spec.side!r}")
    if spec.population not in (POPULATION_BRIEFED, POPULATION_ALL):
        raise PanelError(f"unknown population {spec.population!r}")
    # The by-name screens come first, so that an outcome-shaped argument is
    # always reported as what it is rather than as the wrong column shape.
    _screen_names((spec.status_column, spec.anchor_column), role="the spec")
    _screen_names(tuple(spec.version_pins), role="version_pins")
    if not isinstance(spec.cutoff, str) or not _ISO_DATE.fullmatch(spec.cutoff):
        raise PanelError(
            f"cutoff must be ISO text (YYYY-MM-DD), got {spec.cutoff!r}; it is "
            f"compared against the parquet stem as TEXT, so anything else moves "
            f"the discovery boundary instead of raising"
        )
    if Path(spec.labels_dir).resolve() == Path(spec.briefs_dir).resolve():
        raise PanelError(
            f"the label store and the brief store are the same directory "
            f"({spec.labels_dir}); the join to the brief side is what keeps the "
            f"panel to the names the brief carried, and it would stop doing so"
        )
    missing_pins = sorted(spec.require_pinned - set(spec.version_pins))
    if missing_pins:
        raise PanelError(
            f"these columns must be pinned and are not: {missing_pins}. Two "
            f"definitions of the same quantity would otherwise pool silently."
        )
    _validated_pin_keys(spec)


def _validated_pin_keys(spec: PanelSpec) -> None:
    """A pin key is the only caller-extensible part of the blind allowlist.

    So it has to be a version column, and it must not be a column the panel
    already reads as structure: the first keeps layer 1 an allowlist, the second
    stops a duplicated name in the read list and tells the caller what the
    status filter is for.
    """
    not_versions = sorted(key for key in spec.version_pins if not _VERSION_COLUMN.fullmatch(key))
    if not_versions:
        raise PanelError(
            f"version_pins may only name a version column (*_version): "
            f"{not_versions}. A pin key is appended to the blind read allowlist, "
            f"so any other column would be read back and the panel filtered on it."
        )
    structural = sorted(
        set(spec.version_pins) & {*BLIND_STRUCTURE_COLUMNS, spec.status_column, spec.anchor_column}
    )
    if structural:
        raise PanelError(
            f"version_pins names {structural}, which the panel already reads as "
            f"structure. The maturity status is filtered through resolved_status, "
            f"not through a pin."
        )


def _paths(root: Any, spec: PanelSpec) -> list[Path]:
    """The parquets on this side of the cutoff, by filename stem.

    Filtering by stem rather than by the ``brief_date`` column means the held-out
    files are never opened on a burnt read. :func:`_read_labels` asserts the two
    filters are the same thing.
    """
    paths = sorted(Path(root).glob("*.parquet"))
    if spec.side == SIDE_HELD_OUT:
        return [path for path in paths if path.stem > spec.cutoff]
    return [path for path in paths if path.stem <= spec.cutoff]


def _schema_names(path: Path) -> set[str]:
    import pyarrow.parquet as pq

    return set(pq.ParquetFile(path).schema.names)


def _read_labels(paths: Sequence[Path], columns: Sequence[str]) -> pd.DataFrame:
    """The label side, with every requested column required to exist.

    Also asserts that every row's ``brief_date`` equals its file's stem. That is
    what makes a stem filter and a ``brief_date`` column filter interchangeable,
    and the two are used by different call sites. The writer does build one file
    per brief date; this turns that into a checked fact.
    """
    frames = []
    for path in paths:
        available = _schema_names(path)
        missing = sorted(set(columns) - available)
        if missing:
            raise PanelError(f"{path.name} does not carry {missing}")
        frame = pd.read_parquet(path, columns=list(columns))
        stamped = set(frame["brief_date"].astype(str).str.slice(0, _ISO_DATE_LENGTH))
        unexpected = sorted(stamped - {path.stem})
        if unexpected:
            raise PanelError(
                f"{path.name} holds brief_date {unexpected[:_SAMPLE_SIZE]}, which its "
                f"name does not match; a stem filter and a brief_date filter are then "
                f"different filters"
            )
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def _read_briefs(paths: Sequence[Path], *, brief_columns: Sequence[str]) -> pd.DataFrame:
    """The brief side, keyed from the FILENAME.

    A brief parquet carries no ``brief_date`` column, so there is no other key
    available. Stamping it as the stem (a string) is why the label side has to be
    stringified too.
    """
    frames = []
    for path in paths:
        available = _schema_names(path)
        missing = sorted(set(brief_columns) - available)
        if missing:
            raise PanelError(f"{path.name} does not carry {missing}")
        wanted = [BRIEF_KEY_COLUMN, *brief_columns]
        frames.append(pd.read_parquet(path, columns=wanted).assign(brief_date=path.stem))
    return pd.concat(frames, ignore_index=True)


def _apply_pins(panel: pd.DataFrame, pins: Mapping[str, str]) -> pd.DataFrame:
    """Keep the rows matching every pin, refusing a pin that matches nothing.

    A pin matching nothing is a defect, not an empty result: it means the store
    never held the version the caller believes it is reading. That is why the
    pins run before the status and population filters, which CAN legitimately
    keep nothing.

    The missing-column branch is not reachable through the public API, because a
    pin key goes into the read list and :func:`_read_labels` reports it against
    the FILE first, by name. It is kept for a direct caller and is mutated
    directly by its own test.
    """
    for column, value in pins.items():
        if column not in panel.columns:
            raise PanelError(f"cannot pin {column!r}: the panel does not carry it")
        keep = panel[column].astype(str) == str(value)
        if not bool(keep.any()):
            present = sorted(set(panel[column].astype(str)))[:_SAMPLE_SIZE]
            raise PanelError(
                f"pinning {column}={value!r} keeps no rows; the panel carries {present}"
            )
        panel = panel[keep]
    return panel


def _arrival(panel: pd.DataFrame, spec: PanelSpec) -> pd.DataFrame:
    """Validate the stored anchor, then expose it as ``arrival``."""
    text = panel[spec.anchor_column].astype(str).str.slice(0, _ISO_DATE_LENGTH)
    # ``repr`` INSIDE the comprehension, not around the sorted result: after
    # ``astype(str)`` a missing value comes back as a float NaN, so a panel
    # carrying one null and one non-ISO anchor would sort a float against a str
    # and raise TypeError instead of the message meant for the caller.
    unusable = sorted(
        {
            repr(value)
            for value in text
            if not (isinstance(value, str) and _ISO_DATE.fullmatch(value))
        }
    )
    if unusable:
        raise PanelError(
            f"{spec.anchor_column} is unusable on {len(unusable)} value(s) in the "
            f"panel: {unusable[:_SAMPLE_SIZE]}. An arrival session must be an ISO "
            f"date; a null one is not counted by nunique(), so it would "
            f"UNDER-COUNT the arrival clusters rather than raise."
        )
    stored = panel[spec.anchor_column]
    panel = panel.drop(columns=[spec.anchor_column])
    # The string form is what the a20 family clusters on. The stored form is what
    # a caller passing the arrival to a calendar helper needs, so it is handed
    # back verbatim rather than round-tripped through text.
    panel[ARRIVAL_COLUMN] = text if spec.arrival_as_str else stored
    return panel


def _build(spec: PanelSpec, *, outcome: str | None, brief_columns: Sequence[str]) -> pd.DataFrame:
    """The one chain. Order is load-bearing and is stated step by step below."""
    from alphalens_research.diagnostics.options_retro import ticker_episode_dedup

    _validated(spec)
    _screen_names(brief_columns, role="brief_columns")

    label_paths = _paths(spec.labels_dir, spec)
    brief_paths = _paths(spec.briefs_dir, spec)
    if not label_paths or not brief_paths:
        raise PanelError(
            f"no parquets on the {spec.side} side of {spec.cutoff} "
            f"({len(label_paths)} label, {len(brief_paths)} brief)"
        )

    label_columns = list(blind_label_columns(spec))
    if outcome is not None and outcome not in label_columns:
        label_columns.append(outcome)
    labels = _read_labels(label_paths, label_columns)
    briefs = _read_briefs(brief_paths, brief_columns=brief_columns)

    # The join key is a string on both sides deliberately. The label store stamps
    # a native date and the brief key can only come from a filename; a silent type
    # mismatch empties the join and hands back a clean-looking empty frame.
    labels["brief_date"] = labels["brief_date"].astype(str)
    labels["ticker"] = labels["ticker"].astype(str).str.upper()
    briefs["ticker"] = briefs["ticker"].astype(str).str.upper()

    try:
        panel = labels.merge(briefs, on=["brief_date", "ticker"], how="inner", validate="m:1")
    except pd.errors.MergeError as exc:
        raise PanelError(f"the brief side is not unique per (brief_date, ticker): {exc}") from exc
    if panel.empty:
        raise PanelError(
            "the join matched 0 rows. That is a key defect, not an absence of data: "
            "check that the two stores agree on brief_date and ticker."
        )

    # The pin runs FIRST of the four row filters, because it is the only one
    # whose empty result is a DEFECT. After the status or population filter it
    # would see an already-empty panel and report the label version as the
    # reason nothing is left, which is false about the store.
    panel = _apply_pins(panel, spec.version_pins)

    # EVERY row filter runs BEFORE the collapse, the pin above included. The
    # chained collapse keeps the FIRST row of an episode, so filtering after it
    # represents an episode by a row that was dropped — measured as an empty
    # panel, not as a smaller one.
    panel = panel[panel[spec.status_column].astype(str) == spec.resolved_status]
    if spec.population == POPULATION_BRIEFED:
        # A null flag is not briefed. The store writes this column nullable, so
        # a row the stamper never resolved arrives as null, and treating that as
        # briefed would widen the population a registration froze.
        panel = panel[panel["briefed_any_theme"].fillna(False).astype(bool)]
    if outcome is not None:
        panel = panel.dropna(subset=[outcome, *brief_columns])

    # The collapse is the only de-duplication. Two rows sharing a
    # (brief_date, ticker) share one arrival, so the chained rule sees distance
    # zero and drops the second; a drop_duplicates before it would remove rows
    # this never keeps, and read as a second mechanism.
    panel = ticker_episode_dedup(panel).reset_index(drop=True)
    # The anchor guard comes after the collapse so that its scope is the rows
    # the panel KEEPS, which is what its message claims. The collapse itself
    # keys on brief_date and never reads the anchor.
    panel = _arrival(panel, spec)

    _assert_blind(panel, outcome=outcome)
    return panel


def _assert_blind(panel: pd.DataFrame, *, outcome: str | None) -> None:
    """The layer the signature and the allowlist cannot replace.

    Catches a merge suffix such as ``sel_ar_20_y`` and a store that gains an
    outcome column after the registration froze.
    """
    allowed = {outcome} if outcome is not None else set()
    leaked = sorted(
        column
        for column in panel.columns
        if _OUTCOME_SHAPED.match(str(column)) and column not in allowed
    )
    if leaked:
        raise PanelError(f"the panel carries outcome-shaped columns it was not asked for: {leaked}")
