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

This module is that chain, once. It owns no frozen constant: the cutoff, the
population, the status column, the resolved-status token and the arrival dtype
are all the caller's, because they are pre-registered choices and a default here
would leave two copies of each to keep frozen.

Outcome blindness
-----------------
There are two public builders, never one with a boolean. The blind one takes no
``outcome`` argument at all, so there is no value a caller could pass to receive
one and no keyword a ``**kwargs`` dict could flip. Blindness is held in three
layers, because each has a hole the others close:

1. **Signature and allowlist.** The blind path's label columns come from
   :func:`blind_label_columns`, built from :data:`BLIND_STRUCTURE_COLUMNS` plus
   the structural columns the spec names. An allowlist rather than a denylist:
   a ``sel_ar_*`` denylist would still let a held-out SIGNAL column through, and
   the held-out dependence structure is itself a look.
2. **Argument screening.** A name matching :data:`_OUTCOME_SHAPED` in
   ``brief_columns`` is refused by name, before any file is opened. That blocks
   reaching a second held-out label through the feature list.
3. **Result assertion.** No column of the blind frame may match that pattern.
   This is the layer the first two cannot replace: it catches a merge suffix
   (``sel_ar_20_y``) and a brief store that one day gains an outcome column. The
   brief store does gain columns after registrations freeze.

The non-ISO arrival guard
-------------------------
An unusable ``anchor_session`` is refused rather than dropped, and the reason is
counter-intuitive enough to state: ``.str.slice`` hands a missing value back as a
float NaN even after ``astype(str)``, and ``groupby`` drops a NaN key by default.
So a bad anchor DELETES an episode instead of inflating the cluster count, and
the panel shrinks with nothing raised anywhere. The guard is checked against the
rows the panel KEEPS, not against the file: a bad anchor on a row the status or
population filter excludes is not this panel's problem.

What this module deliberately does NOT fix
------------------------------------------
``ticker_episode_dedup`` chains episodes on ``session_on_or_after(brief_date)``
while the ``arrival`` column here is the STORED ``anchor_session``. Owner
decision D4 says those two differ — measured on 296 of 466 burnt rows — so the
episode-chaining clock and the cluster labels come from two definitions of
arrival. Every panel in the repo has this property and every registered number
was computed with it. Reconciling them would move every panel, so it stays, and
it has its own issue.

Two of the four copies are NOT migrated onto this loader yet, for reasons worth
recording so they are not re-litigated. ``2026_09_sel_ar_20_burnt_probe.py`` and
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

#: A label value, by name shape. Outcomes live under ``sel_ar_*`` (the #1477
#: selection labels), ``car_*`` (the earlier event-window labels) and
#: ``sel_label_value*``. Used to screen caller arguments and to assert the
#: returned frame, never to decide what to read — that is an allowlist.
_OUTCOME_SHAPED = re.compile(r"^(sel_ar_|car_|sel_label_value)")

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

    No field has a default. Every one of them is either a pre-registered
    constant or a store layout fact, and a default would put a second copy of it
    here to keep frozen.
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
    #: column -> required value, applied BEFORE the episode collapse.
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
    """
    return (
        *BLIND_STRUCTURE_COLUMNS,
        spec.anchor_column,
        spec.status_column,
        *sorted(spec.version_pins),
    )


def episode_panel_blind(spec: PanelSpec) -> pd.DataFrame:
    """The panel's keys, status and population. Reads no outcome and no signal.

    Returns one row per ticker-episode with ``brief_date``, ``ticker``,
    ``arrival``, the population flag, the status column and any pinned version
    columns. The brief store is read for the JOIN KEY alone, so a brief feature
    cannot arrive here even by accident.
    """
    return _build(spec, outcome=None, brief_columns=(), optional_brief_columns=())


def episode_panel_with_outcome(
    spec: PanelSpec,
    *,
    outcome: str,
    brief_columns: Sequence[str],
    optional_brief_columns: Sequence[str] = (),
) -> pd.DataFrame:
    """The same panel plus one label column and the named brief features.

    Calling this on the held-out side of a registration IS the start of that
    look. ``outcome`` is the one label column this call may read. A row missing
    the outcome or any required brief column is dropped BEFORE the collapse, so
    an episode is represented by its first USABLE row.

    ``brief_columns`` lives here rather than on :class:`PanelSpec` on purpose:
    that makes "a blind caller cannot name a brief feature column" true by
    signature instead of by a runtime check. ``optional_brief_columns`` are read
    from the files whose schema carries them and come back null elsewhere, which
    is what a column added to the brief store mid-history needs.
    """
    return _build(
        spec,
        outcome=outcome,
        brief_columns=tuple(brief_columns),
        optional_brief_columns=tuple(optional_brief_columns),
    )


def episode_counts(spec: PanelSpec) -> tuple[int, int]:
    """``(episodes, arrival clusters)``, outcome-blind.

    An UPPER bound on what :func:`episode_panel_with_outcome` would return on the
    same spec, by however many resolved rows carry a null outcome or a null
    feature. The bound points the safe way: a preflight can refute a run and
    never promise one. A store defect still raises, because "the keys do not
    match" must not be reported as "not enough data yet".
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
    missing_pins = sorted(spec.require_pinned - set(spec.version_pins))
    if missing_pins:
        raise PanelError(
            f"these columns must be pinned and are not: {missing_pins}. Two "
            f"definitions of the same quantity would otherwise pool silently."
        )
    _screen_names((spec.status_column, spec.anchor_column), role="the spec")
    _screen_names(tuple(spec.version_pins), role="version_pins")


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


def _read_briefs(
    paths: Sequence[Path],
    *,
    brief_columns: Sequence[str],
    optional_brief_columns: Sequence[str],
) -> pd.DataFrame:
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
        wanted = [
            BRIEF_KEY_COLUMN,
            *brief_columns,
            *[column for column in optional_brief_columns if column in available],
        ]
        frames.append(pd.read_parquet(path, columns=wanted).assign(brief_date=path.stem))
    return pd.concat(frames, ignore_index=True)


def _apply_pins(panel: pd.DataFrame, pins: Mapping[str, str]) -> pd.DataFrame:
    """Keep the rows matching every pin, refusing a pin that matches nothing.

    A pin matching nothing is a defect, not an empty result: it means the store
    never held the version the caller believes it is reading.
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
    unusable = sorted(
        {value for value in text if not (isinstance(value, str) and _ISO_DATE.fullmatch(value))}
    )
    if unusable:
        raise PanelError(
            f"{spec.anchor_column} is unusable on {len(unusable)} value(s) in the "
            f"panel: {list(map(repr, unusable))[:_SAMPLE_SIZE]}. An arrival session "
            f"must be an ISO date; an unusable one would DELETE an episode rather "
            f"than raise, because groupby drops a null key."
        )
    stored = panel[spec.anchor_column]
    panel = panel.drop(columns=[spec.anchor_column])
    # The string form is what the a20 family clusters on. The stored form is what
    # a caller passing the arrival to a calendar helper needs, so it is handed
    # back verbatim rather than round-tripped through text.
    panel[ARRIVAL_COLUMN] = text if spec.arrival_as_str else stored
    return panel


def _build(
    spec: PanelSpec,
    *,
    outcome: str | None,
    brief_columns: Sequence[str],
    optional_brief_columns: Sequence[str],
) -> pd.DataFrame:
    """The one chain. Order is load-bearing and is stated step by step below."""
    from alphalens_research.diagnostics.options_retro import ticker_episode_dedup

    _validated(spec)
    _screen_names(brief_columns, role="brief_columns")
    _screen_names(optional_brief_columns, role="optional_brief_columns")

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
    briefs = _read_briefs(
        brief_paths,
        brief_columns=brief_columns,
        optional_brief_columns=optional_brief_columns,
    )

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

    # EVERY filter runs BEFORE the collapse. The chained collapse keeps the FIRST
    # row of an episode, so filtering afterwards represents an episode by a row
    # that was dropped — measured as an empty panel, not as a smaller one.
    panel = panel[panel[spec.status_column].astype(str) == spec.resolved_status]
    if spec.population == POPULATION_BRIEFED:
        panel = panel[panel["briefed_any_theme"].fillna(False).astype(bool)]
    panel = _apply_pins(panel, spec.version_pins)
    if outcome is not None:
        panel = panel.dropna(subset=[outcome, *brief_columns])

    panel = _arrival(panel, spec)
    panel = panel.drop_duplicates(subset=["brief_date", "ticker"])
    panel = ticker_episode_dedup(panel).reset_index(drop=True)

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
