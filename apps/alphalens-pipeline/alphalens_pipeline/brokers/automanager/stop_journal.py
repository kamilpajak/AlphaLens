"""The standalone-stop journal: its path, its lines, and everything that reads
or writes one.

Extracted from ``control_loop`` unchanged, in two steps (architecture audit
2026-10-02, finding 2 / #1677). Step 2 took the path, the line parsers, the
folds and the append -- the layer the rest of the partition was waiting on,
because every large cluster still reachable for extraction called back into one
of these helpers. Step 3 took what was left of the same concern: the per-kind
line WRITERS (``_journal_stop_placed``, ``_journal_trailed``, ...), the
remaining folds, the compactor's trackers and electors, and the two counters
bound to the journal (``_make_next_gen`` / ``_make_next_amend_seq``).

What determined the step-3 set is worth stating, because a hand-picked one
would rot: it is the MAXIMAL set of control_loop functions that reach these
helpers and is CLOSED under calls. Nothing in it needs anything that stayed
behind, which is why the move could not leave a back-edge.

Several journal functions deliberately stayed in ``control_loop``, and they are
the ones that reach the journal only THROUGH this module -- the compactor
(``_compact_standalone_stop_journal_lines``), the arm gate
``_governing_plan_lookup`` (it answers in ``_ArmRefusal``, a refusal record
that is not a journal concept), ``_uics_moving_their_stop``,
``_journal_entry_planned_disaster`` and ``_fold_trailed_since_latest_plan``.
The #1669 stop-move selector surface (``_elect_stop_placed``,
``_latest_stop_move``, ``_stop_move_closer``) also stayed: it would drag
``_StandingStop``, the fill-reconcile pass's record, in with it. All of them
are candidates for a later step, not an oversight.

One journal, several kinds of line. ``state_paths.standalone_stops_path()`` is the
single seam (ADR 0016); the ``planned`` lines carry the prices the broker
cannot know, and the ``tranche_plan`` lines carry the take-profit ladder. No
journal line confers protection -- the protection pass derives that from live
broker state -- so these folds answer "what did we intend", never "what is
armed".

``_coerce`` lives here because its own docstring says what it is: "the 'skip
this malformed field' primitive for the journal compactor".

Logging note, as in steps 1 and 2: the moved code logs through THIS module's
logger, so a warning that used to read as ``control_loop`` now reads as
``stop_journal``. The text is unchanged.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import math
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from broker_contract.contract import _QTY_EPS
from broker_contract.trade_intent.codec import _decode_reaction_primitive

from alphalens_pipeline.brokers.automanager import entry_trails, state_paths
from alphalens_pipeline.brokers.automanager.position_manager import (
    _TERMINAL_NON_FILLED,
    PlannedExit,
)

if TYPE_CHECKING:  # annotations only; the runtime construction imports it in-body
    from broker_contract.sizing import TpTranchePlan

    from alphalens_pipeline.brokers.reconcile import ReconcileVerdict

logger = logging.getLogger(__name__)


def _apply_generation_reset(
    kind: Any,
    line: Mapping[str, Any],
    uic: int,
    governing_key: dict[int, str],
    accumulators: tuple[dict[int, Any], ...],
    *,
    include_planned: bool = False,
) -> bool:
    """The identity-keyed generation reset shared by the fired/trailed folds
    (see ``_fold_fired_since_latest_plan`` for the incident history): a keyless
    plan line or one with a DIFFERENT ``pick_key`` clears the uic's
    accumulators; a SAME-key re-append does not; a retraction always clears.
    Returns True when the line was a plan/retraction line (the caller consumes
    it and moves on).

    ``include_planned`` (#1236) additionally treats ``planned`` /
    ``planned_retracted`` lines as generation markers. OFF by default, and ON for
    the TRAILED selection only: the fired-tranche and round-trip-closure folds are
    about a TP ladder, which only a ``tranche_plan`` line describes, and widening
    their reset would change what counts as an already-fired tranche. The trailed
    level is different — it belongs to a POSITION, and a pick with no take-profit
    journals no ``tranche_plan`` at all, so under the tranche-only rule its uic
    inherited the previous position's level. Every pick journals ``planned``
    lines, which is what makes them the right generation marker here. A
    multi-tier pick writes several of them under ONE ``pick_key``, so the tiers
    do not reset each other."""
    plan_kinds = (_TRANCHE_PLAN_KIND, "planned") if include_planned else (_TRANCHE_PLAN_KIND,)
    retracted_kinds = (
        (_TRANCHE_PLAN_RETRACTED_KIND, _PLANNED_RETRACTED_KIND)
        if include_planned
        else (_TRANCHE_PLAN_RETRACTED_KIND,)
    )
    if kind in plan_kinds:
        key = line.get("pick_key")
        if key is None or str(key) != governing_key.get(uic):
            for acc in accumulators:
                acc.pop(uic, None)
        if key is None:
            governing_key.pop(uic, None)
        else:
            governing_key[uic] = str(key)
        return True
    if kind in retracted_kinds:
        for acc in accumulators:
            acc.pop(uic, None)
        governing_key.pop(uic, None)
        return True
    return False


_GENERATION_TAIL_RE = re.compile(r"-g[1-9]\d*$")


def _pick_key_from_stop_ref(ref: str | None) -> str | None:
    """Recover the colon-form pick key from a stop ref (fallback when the uic
    has no ``tranche_plan`` ``pick_key`` on record).

    The entry-trail stop ref is ``<ticker>-<trade_date>-entry-t<i>-stop-<gen>``
    (``position_manager._exit_stop_ref`` over the watch crid). A classic
    bracket ref has a different shape and returns ``None`` — the caller treats
    that as "no entry-trail siblings exist", which is true by construction."""
    if not ref or "-entry-t" not in ref:
        return None
    prefix = ref.split("-entry-t", 1)[0]  # "<ticker>-<YYYY-MM-DD>[-g<N>]"
    # #1371: a same-day re-arm's crid carries `-g<N>` (N >= 1, digits) after
    # the date. Peel it before the date walk and put it back on the key, so
    # the recovered key is the SAME string the watch_open lines carry.
    generation_suffix = ""
    generation_match = _GENERATION_TAIL_RE.search(prefix)
    if generation_match:
        generation_suffix = generation_match.group(0)
        prefix = prefix[: generation_match.start()]
    # rpartition: the DATE is the fixed-shape tail; the ticker may itself carry
    # a hyphen (yfinance-style class shares, e.g. BRK-B) — zen LOW on #1222.
    head, sep, day = prefix.rpartition("-")
    head, sep2, month = head.rpartition("-")
    ticker, sep3, year = head.rpartition("-")
    if not (sep and sep2 and sep3 and ticker):
        return None
    trade_date = f"{year}-{month}-{day}"
    try:
        dt.date.fromisoformat(trade_date)
    except ValueError:
        return None
    return f"{ticker}:{trade_date}{generation_suffix}"


def _standalone_stop_journal_path() -> Path:
    """The out-of-band standalone-stop journal path — funnels through the ONE
    broker-state path seam (state_paths.standalone_stops_path(), ADR 0016 /
    design memo D2), resolved fresh on EVERY call, never cached at import
    time. A thin named wrapper (rather than calling the seam directly at each
    of the three call sites below) so tests monkeypatch ONE attribute."""
    return state_paths.standalone_stops_path()


def _append_standalone_stop_journal(record: Mapping[str, Any]) -> None:
    """Append one line to the out-of-band standalone-stop journal (never rewrites).

    Flush + fsync after the append so a plan price / capability marker is durable
    the instant it is written — a buffered write lost to a crash (or systemd
    SIGKILL) would silently drop a disaster-stop plan, and the protection pass
    can never re-derive a price the broker does not know."""
    from alphalens_pipeline.brokers.journal import append_json_line

    append_json_line(_standalone_stop_journal_path(), record, default=str)


_INITIAL_GEN = 0  # entry-placement plan is generation 0; resizes bump it via next_gen() (Task 4)


def _iter_standalone_stop_journal() -> Iterator[dict[str, Any]]:
    """Yield parsed lines from the standalone-stop journal; malformed lines skipped."""
    path = _standalone_stop_journal_path()
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        yield from _parse_standalone_stop_lines(fh)


def _parse_standalone_stop_lines(raw_lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """The parsing rules of :func:`_iter_standalone_stop_journal`, over lines
    already read, so the boot compactor parses exactly the bytes it snapshots
    (#1648) instead of reading the file a second time."""

    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def _latest_planned_by_crid(
    lines: Iterable[Mapping[str, Any]],
) -> dict[str, tuple[int, Mapping[str, Any]]]:
    """The newest well-formed ``planned`` line per entry client_request_id
    (append-only: highest ``gen`` wins). Non-``planned``, keyless, or malformed
    (bad uic / stop_price) lines are skipped.

    A ``planned_retracted`` marker (#1249) REMOVES the crid in write order — a
    ``planned`` line appended AFTER the marker is a genuinely new plan and
    governs again (entry-trail crids are sticky-terminal and bracket crids are
    UUIDs, so a retracted crid cannot resurrect by accident). This is the ONE
    choke point both the fold and the compaction read, so retraction semantics
    cannot drift between them."""
    latest: dict[str, tuple[int, Mapping[str, Any]]] = {}
    for line in lines:
        kind = line.get("kind")
        if kind == _PLANNED_RETRACTED_KIND:
            retracted_crid = line.get("client_request_id")
            if retracted_crid:
                latest.pop(str(retracted_crid), None)
            continue
        if kind != "planned":
            continue
        crid = line.get("client_request_id")
        if not crid:
            continue
        gen = _planned_line_gen(line)
        if gen is None:
            continue
        prev = latest.get(str(crid))
        if prev is None or gen >= prev[0]:
            latest[str(crid)] = (gen, line)
    return latest


def _planned_line_gen(line: Mapping[str, Any]) -> int | None:
    """The ``gen`` of a well-formed ``planned`` line; ``None`` for any
    malformation (missing/bad uic or stop_price — the line is skipped)."""
    if line.get("uic") is None:
        return None
    try:
        gen = int(line.get("gen", _INITIAL_GEN))
        int(line["uic"])
        float(line["stop_price"])
    except (KeyError, TypeError, ValueError):
        return None
    return gen


_TRANCHE_PLAN_KIND = "tranche_plan"


_TRANCHE_PLAN_RETRACTED_KIND = "tranche_plan_retracted"


# #1249: the per-crid retraction marker for ``planned`` disaster-stop lines —
# the tranche marker's sibling, keyed by client_request_id instead of uic.
# Consumed inside ``_latest_planned_by_crid`` (write-order pop), so the fold
# and the compaction inherit retraction at the same choke point.
_PLANNED_RETRACTED_KIND = "planned_retracted"


def fold_tranche_plans(
    lines: Iterable[Mapping[str, Any]],
) -> dict[int, tuple[tuple[TpTranchePlan, ...], float, float]]:
    """Fold the append-only ``tranche_plan`` journal lines into the newest ladder
    per uic -- ``{uic: (tp_tranches, reference_qty, stop_price)}``.

    Append-only: the LAST well-formed line for a uic wins (mirrors
    ``_latest_planned_by_crid``'s "last wins" semantics, keyed per-uic since a
    live position nets to one uic). Non-``tranche_plan`` lines and malformed
    lines (missing/unparsable uic, non-list ``tp_tranches``, or a tranche
    missing/mistyping any of its five fields) are skipped ENTIRELY -- a
    malformed line contributes nothing, never a partial fold.

    A ``tranche_plan_retracted`` line (2026-08-19 adjudication finding 3 -- a
    watch that ended with no fill) REMOVES the uic's governing ladder; a later
    plan line for the uic governs again (still last-wins, in write order)."""
    out: dict[int, tuple[tuple[TpTranchePlan, ...], float, float]] = {}
    for line in lines:
        kind = line.get("kind")
        if kind == _TRANCHE_PLAN_RETRACTED_KIND:
            retracted_uic = _coerce(line, "uic", int)
            if retracted_uic is not None:
                out.pop(retracted_uic, None)
            continue
        if kind != _TRANCHE_PLAN_KIND:
            continue
        parsed = _parse_tranche_plan_line(line)
        if parsed is None:
            continue
        uic, tranches, reference_qty, stop_price = parsed
        out[uic] = (tranches, reference_qty, stop_price)
    return out


def _parse_tranche_plan_line(
    line: Mapping[str, Any],
) -> tuple[int, tuple[TpTranchePlan, ...], float, float] | None:
    """Parse one ``tranche_plan`` line; None for ANY malformation (a bad line
    contributes nothing, never a partial fold)."""
    from broker_contract.sizing import TpTranchePlan

    raw_uic = line.get("uic")
    raw_tranches = line.get("tp_tranches")
    if raw_uic is None or not isinstance(raw_tranches, list):
        return None
    try:
        uic = int(raw_uic)
        reference_qty = float(line["reference_qty"])
        stop_price = float(line["stop_price"])
        # `float()` happily parses JSON's `NaN` / `Infinity`, so a malformed
        # or hand-edited line could otherwise become a GOVERNING ladder
        # carrying a non-finite size. Refused at the SOURCE as well as at
        # the sizer, because a bad line should contribute nothing rather
        # than be caught later by whichever consumer happens to look first.
        if not math.isfinite(reference_qty) or not math.isfinite(stop_price):
            raise ValueError(
                f"non-finite tranche_plan scalars for uic {uic}: "
                f"reference_qty={reference_qty!r} stop_price={stop_price!r}"
            )
        tranches = tuple(
            TpTranchePlan(
                tranche_index=int(t["tranche_index"]),
                target_price=float(t["target_price"]),
                # Legacy lines carry "tranche_pct". Read them as a FRACTION,
                # because that is what the writer meant: every tranche_plan
                # record on the LIVE rail was written by the geometry
                # producer with the literal 1.0 for "the whole position"
                # (verified 2026-08-25 against all three live journal
                # lines). Converting them as percentages would resize an
                # in-flight position's exit to 1% and leave the rest naked.
                tranche_frac=float(t["tranche_frac"] if "tranche_frac" in t else t["tranche_pct"]),
                r_multiple=float(t["r_multiple"]),
                tag=str(t["tag"]),
            )
            for t in raw_tranches
        )
        # Same source-refusal as the scalars above: tranche_frac is covered by
        # TpTranchePlan's [0, 1] guard (NaN fails the range check), but
        # target_price and r_multiple have no construction guard — a
        # hand-edited line carrying a non-finite take-profit must contribute
        # nothing, never become a governing ladder whose TP limit goes to the
        # broker.
        for tranche in tranches:
            if not math.isfinite(tranche.target_price) or not math.isfinite(tranche.r_multiple):
                raise ValueError(
                    f"non-finite tranche field for uic {uic}: "
                    f"target_price={tranche.target_price!r} r_multiple={tranche.r_multiple!r}"
                )
    except (KeyError, TypeError, ValueError):
        return None
    return uic, tranches, reference_qty, stop_price


def _fold_governing_plan_pick_keys(lines: Iterable[Mapping[str, Any]]) -> dict[int, str | None]:
    """The governing ``tranche_plan``'s ``pick_key`` per uic, in write order
    (last wins; ``None`` = a keyless bracket-path plan). A
    ``tranche_plan_retracted`` line removes the uic — an already-retracted plan
    can never be matched (and so never re-retracted) by the sweep below."""
    governing: dict[int, str | None] = {}
    for line in lines:
        kind = line.get("kind")
        if kind not in (_TRANCHE_PLAN_KIND, _TRANCHE_PLAN_RETRACTED_KIND):
            continue
        uic = _coerce(line, "uic", int)
        if uic is None:
            continue
        if kind == _TRANCHE_PLAN_KIND:
            key = line.get("pick_key")
            governing[uic] = None if key is None else str(key)
        else:
            governing.pop(uic, None)
    return governing


def _retract_planned_lines(
    crids: Iterable[str],
    *,
    note: str,
    journal_lines: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    """Append one ``planned_retracted`` marker per STILL-GOVERNING crid (#1249).

    Idempotence lives here so every retraction class gets it for free: a crid
    already retracted (or never journaled — a tier that expired before its
    fire-arm) is skipped, never marker-stacked. ``journal_lines`` lets a sweep
    that already read the journal skip the re-read; omitted, the journal is
    read fresh. Broad exception boundary on purpose — retraction is journal
    housekeeping, a read/write failure degrades to a WARN and a retry on the
    next tick, never an aborted caller. Returns the number of markers written."""
    try:
        lines = (
            journal_lines if journal_lines is not None else list(_iter_standalone_stop_journal())
        )
        latest = _latest_planned_by_crid(lines)
        count = 0
        for crid in crids:
            entry = latest.get(str(crid))
            if entry is None:
                continue  # absent or already retracted — nothing to do
            _gen, line = entry
            _append_standalone_stop_journal(
                {
                    "kind": _PLANNED_RETRACTED_KIND,
                    "client_request_id": str(crid),
                    "uic": _coerce(line, "uic", int),
                    "note": note,
                }
            )
            logger.info("planned line %s retracted — %s", crid, note)
            count += 1
        return count
    except Exception:
        logger.warning("planned-line retraction failed — will retry next tick", exc_info=True)
        return 0


# The journal field that carries the stop LEVEL on each stop-move marker kind
# (#1621). The kinds are the ones the amend-success branch of the protection
# executor writes; a test drives that branch and checks each written kind is
# here and has an alert reason in ``trade_alerts``.
_STOP_MOVE_LEVEL_KEY: Mapping[str, str] = {"trailed": "level", "reanchored": "stop_price"}


class _StopMove(NamedTuple):
    """The newest stop-move marker for one standing stop."""

    kind: str
    level: float | None


def _positive_float_or_none(value: Any) -> float | None:
    """``value`` as a finite positive float, else ``None`` (a level for display)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def stop_move_of(line: Mapping[str, Any]) -> _StopMove:
    """The kind and level of a line :func:`select_stop_move_lines` returned."""
    kind = str(line.get("kind"))
    return _StopMove(kind, _positive_float_or_none(line.get(_STOP_MOVE_LEVEL_KEY[kind])))


def select_stop_move_lines(
    lines: Iterable[Mapping[str, Any]],
    since_by_uic: Mapping[int, float],
    until_by_uic: Mapping[int, float] | None = None,
) -> dict[int, Mapping[str, Any]]:
    """Per uic of ``since_by_uic``, the newest stop-move marker (``trailed`` /
    ``reanchored``) written in ``[since, until]`` (``until`` open when the uic
    has none), or no entry when nothing moved the stop in that window.

    The ONE answer to "what moved THIS stop" (#1669). The stop-fill alert, the
    boot compactor and ``broker trades`` each ask it, so they cannot name
    different moves for one stop. Scoped by time, not by the plan generation the
    protection folds use: a marker from an earlier position on a reused uic
    predates this stop's placement.

    A later line breaks a timestamp tie. A line whose ``ts`` is not a finite
    number, or whose level is not a finite positive number, is skipped, so a
    malformed newer line never hides a good older one."""
    until = until_by_uic or {}
    newest: dict[int, tuple[float, Mapping[str, Any]]] = {}
    for line in lines:
        level_key = _STOP_MOVE_LEVEL_KEY.get(str(line.get("kind")))
        if level_key is None or _positive_float_or_none(line.get(level_key)) is None:
            continue
        uic = _coerce(line, "uic", int)
        if uic is None or uic not in since_by_uic:
            continue
        ts = _coerce(line, "ts", float)
        if ts is None or not math.isfinite(ts) or ts < since_by_uic[uic]:
            continue
        if uic in until and ts > until[uic]:
            continue
        kept = newest.get(uic)
        if kept is None or ts >= kept[0]:
            newest[uic] = (ts, line)
    return {uic: line for uic, (_ts, line) in newest.items()}


def _coerce(line: Mapping[str, Any], key: str, caster: Callable[[Any], Any]) -> Any:
    """Cast ``line[key]`` via ``caster``, or return None if the key is missing or
    the value is uncastable — the "skip this malformed field" primitive for the
    journal compactor."""
    try:
        return caster(line[key])
    except (KeyError, TypeError, ValueError):
        return None


# --- Moved out of control_loop (step 3 of the partition) ---------------------
# Every reader and writer of a journal LINE belongs to the module that owns the
# journal FILE. control_loop reaches these through the module prefix, exactly as
# it already reaches what step 2 extracted.


def _fold_fired_since_latest_plan(lines: Iterable[Mapping[str, Any]]) -> dict[int, frozenset[str]]:
    """Fired-tranche tags per uic, RESET on each new ``tranche_plan`` line.

    A uic is stable per instrument (Saxo nets by uic), and the standalone-stop
    journal is append-only and NEVER cleared — so a position that fully exits
    (every tranche fired) and is later RE-ENTERED on the same uic would, under a
    fold of every ``tranche_fired`` line ever written for the uic, inherit the
    PRIOR trade's fired tags and silently suppress the new trade's whole TP ladder
    forever. Processing the journal in write order (``_iter_standalone_stop_
    journal`` already yields it that way), a new ``tranche_plan`` line for a
    uic clears its accumulator — only ``tranche_fired`` lines AFTER the LATEST
    plan for that uic count.

    Identity-keyed reset (2026-08-19 adjudication finding 4): a ``tranche_plan``
    line carrying the SAME ``pick_key`` as the uic's governing plan is an
    idempotent re-append (the ``already_watching`` crash-recovery re-drive
    re-journals the pick's plan every tick until its retirement record lands)
    and must NOT reset — resetting would re-arm already-fired tranches and
    re-sell the remainder at the tranche-0 target. A DIFFERENT ``pick_key``, a
    keyless line (the bracket path — today's always-reset semantics), or a
    ``tranche_plan_retracted`` line (finding 3) still clears the accumulator."""
    fired: dict[int, set[str]] = {}
    governing_key: dict[int, str] = {}
    for line in lines:
        uic = _coerce(line, "uic", int)
        if uic is None:
            continue
        kind = line.get("kind")
        if _apply_generation_reset(kind, line, uic, governing_key, (fired,)):
            continue
        if kind == "tranche_fired":
            tag = line.get("tag")
            if tag:
                fired.setdefault(uic, set()).add(str(tag))
    return {u: frozenset(t) for u, t in fired.items()}


def _fold_round_trip_closures_since_latest_plan(
    lines: Iterable[Mapping[str, Any]],
) -> dict[int, frozenset[str | None]]:
    """Durable round-trip closure evidence per uic, RESET on each new plan
    generation (#1223).

    The fired-terminal retraction sweep needs POSITIVE journal evidence that a
    fired tier's position lifecycle CONCLUDED before it may consult the
    positions endpoint at all: a positions read alone can report flat while a
    fresh fill has not materialized yet (retracting then would strip a LIVE
    position's exit management — fail-deadly), and a held position whose
    sibling watches merely expired looks terminal without ever having closed.

    Evidence setters, counted only while the uic's plan generation is OPEN (a
    ``tranche_plan`` line was seen and not retracted — a closure can never
    predate the plan it closes, and requiring this keeps the boot compactor's
    reordered output folding identically):
      - a full ``stop_filled`` (falsy ``partial``); its element is the stop
        ref's parsed pick key, or ``None`` when the ref has no entry-trail
        shape (a classic bracket stop round-tripping the uic still closed it);
      - a ``tranche_fired`` carrying ``position_closed`` (element ``None`` —
        the line has no ref to attribute).
    Reset shares :func:`_apply_generation_reset` verbatim: a keyless or
    different-key plan line and a retraction clear the uic; the
    ``already_watching`` same-key re-append does not."""
    closures: dict[int, set[str | None]] = {}
    governing_key: dict[int, str] = {}
    generation_open: set[int] = set()
    for line in lines:
        uic = _coerce(line, "uic", int)
        if uic is None:
            continue
        kind = line.get("kind")
        if _apply_generation_reset(kind, line, uic, governing_key, (closures,)):
            if kind == _TRANCHE_PLAN_KIND:
                generation_open.add(uic)
            else:
                generation_open.discard(uic)
            continue
        if uic in generation_open:
            _fold_closure_evidence(line, kind, uic, closures)
    return {u: frozenset(s) for u, s in closures.items()}


def _fold_closure_evidence(
    line: Mapping[str, Any], kind: Any, uic: int, closures: dict[int, set[str | None]]
) -> None:
    """Fold one open-generation line's closure evidence (in place): a full
    ``stop_filled`` adds its parsed pick key (or ``None`` for a classic
    bracket ref); a position-closing ``tranche_fired`` adds ``None``."""
    if kind == "stop_filled" and not line.get("partial"):
        ref = line.get("ref")
        closures.setdefault(uic, set()).add(
            _pick_key_from_stop_ref(ref if isinstance(ref, str) else None)
        )
    elif kind == _TRANCHE_FIRED_KIND and line.get("position_closed"):
        closures.setdefault(uic, set()).add(None)


def _owed_pick_key_from_stop_fill(line: Mapping[str, Any]) -> str | None:
    """The pick a full ``stop_filled`` with a parseable entry-trail ref owes a
    sibling retire, or ``None``. The ONE rule for both the owed fold and the
    boot compactor's election of the lines it reads (#1327)."""
    if line.get("kind") != "stop_filled" or line.get("partial"):
        return None
    ref = line.get("ref")
    return _pick_key_from_stop_ref(ref if isinstance(ref, str) else None)


def _elect_owed_stop_fill_lines(lines: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per owed pick key, the one ``stop_filled`` line the boot compactor keeps
    so ``_owed_from_stop_fill_refs`` folds the same after a boot (#1327).

    The fold only needs ONE line per pick key (``setdefault``), so the newest
    by ``ts`` is kept (a later line breaks a tie; a line without a usable
    ``ts`` is kept only while no timestamped one is seen). Sorted by pick key
    for a stable file order."""
    elected: dict[str, tuple[float | None, Mapping[str, Any]]] = {}
    for line in lines:
        pick_key = _owed_pick_key_from_stop_fill(line)
        if pick_key is None:
            continue
        ts = _coerce(line, "ts", float)
        kept = elected.get(pick_key)
        if kept is None or (ts is not None and (kept[0] is None or ts >= kept[0])):
            elected[pick_key] = (ts, line)
    return [dict(elected[pick_key][1]) for pick_key in sorted(elected)]


def _ticker_from_ref(ref: str | None) -> str | None:
    """The ticker a crid or stop ref was built for, via the same parser that
    recovers its pick key (dashed tickers and ``-g<N>`` generations included);
    ``None`` for a ref of another shape."""
    pick_key = _pick_key_from_stop_ref(ref)
    return pick_key.split(":", 1)[0] if pick_key else None


_DISASTER_STOP_SIDE = "SELL"  # protective exit of a long entry


def _build_planned_line(
    *,
    entry_crid: str,
    uic: int,
    side: str,
    stop_price: float,
    take_profit: float | None,
    tier_index: int,
    gen: int = _INITIAL_GEN,
    geometry_stamp: dict[str, Any] | None = None,
    pick_key: str | None = None,
    reaction: Any = None,
) -> dict[str, Any]:
    """One append-only `planned` journal line — the plan PRICES the broker cannot
    know (disaster stop + in-band TP), keyed to the entry client_request_id and
    its ORIGINAL tier_index, plus the resize `gen`. `_fold_planned_exits` (Task 4)
    reads these back per-uic into PlannedExit; NO line here confers protection —
    protection is derived from live broker state only (design memo §7).

    ``geometry_stamp`` records WHICH exit was placed (#1414) — namespaced under
    a single ``"geometry"`` key so it can never collide with a field
    `_fold_planned_exits` reads, and it is never read by the fold (it confers no
    protection; the #1112 arm gates are what read it). ``None`` (the default)
    omits the key entirely, so a caller that never passes it keeps a
    byte-identical record to pre-PR-6a.

    ``pick_key`` (#1236) is the plan's TRADE identity — the same
    ``ticker:trade_date[-gN]`` string ``tranche_plan`` lines already carry. It is
    here because a pick with no take-profit journals no ``tranche_plan`` at all, so
    ``tranche_plan`` alone cannot say which trade governs such a uic; a
    ``trailed`` level then outlived the position that earned it. Absent key ->
    key omitted, byte-identical to every line written before #1236."""
    record: dict[str, Any] = {
        "kind": "planned",
        "client_request_id": entry_crid,
        "uic": int(uic),
        "side": side,
        "stop_price": float(stop_price),
        "take_profit": None if take_profit is None else float(take_profit),
        "tier_index": int(tier_index),
        "gen": int(gen),
    }
    if geometry_stamp is not None:
        record["geometry"] = geometry_stamp
    # #1236: what the DOCUMENT declared about managing this stop. The protection
    # pass never sees the intent, so the declaration has to travel on the line the
    # pass DOES read.
    #
    # Written with ``dataclasses.asdict`` and read back with the CODEC's decoder —
    # two different functions, which is a drift risk worth naming rather than
    # papering over: a codec change that renamed a wire key would keep this write
    # emitting the old one, the read would fail, and the fold degrades to
    # "declared nothing", silently disarming every pick after that deploy.
    # ``test_a_journaled_declaration_round_trips_through_the_codec`` is what turns
    # that into a red test instead of a log line.
    if reaction is not None:
        record["reaction"] = dataclasses.asdict(reaction)
    # A BLANK key is absent, not an identity: `_apply_generation_reset` compares
    # keys as strings, so stamping "" would make two unrelated picks match each
    # other while still failing to match a genuinely keyless line.
    if pick_key:
        record["pick_key"] = str(pick_key)
    return record


def _read_persisted_gen(uic: int) -> tuple[int, float | None]:
    """Latest ``(gen, qty)`` recorded for a uic in the append-only gen journal;
    ``(_INITIAL_GEN, None)`` when the uic has never been sized (append-only, so
    the last matching line wins)."""
    gen = _INITIAL_GEN
    last_qty: float | None = None
    for line in _iter_standalone_stop_journal():
        if line.get("kind") != "gen":
            continue
        try:
            if int(line["uic"]) != uic:
                continue
            gen = int(line["gen"])
            last_qty = float(line["qty"])
        except (KeyError, TypeError, ValueError):
            continue
    return gen, last_qty


def _make_next_gen(uic: int) -> Callable[[float], int]:
    """A per-uic resize counter bound to the persisted gen journal (memo §4.5).

    Returns the SAME generation for a same-size retry — Saxo's 15 s request-id
    dedup then catches the re-POST — and a DISTINCT, incremented generation when
    the intended sell qty changes by more than ``_QTY_EPS`` (a resize is a
    distinct order, never falsely deduped to the stale, smaller one). The bump is
    appended, never rewritten, so the counter survives a systemd restart. The
    size compare uses ``_QTY_EPS`` — never a bare float ``>=`` (A-S6/B-S2)."""

    def _next_gen(qty: float) -> int:
        gen, last_qty = _read_persisted_gen(uic)
        if last_qty is not None and abs(qty - last_qty) <= _QTY_EPS:
            return gen  # same-size retry -> stable ref (dedup-safe)
        if last_qty is not None:
            gen += 1  # resize -> distinct ref (never deduped to the stale order)
        _append_standalone_stop_journal(
            {"kind": "gen", "uic": int(uic), "gen": int(gen), "qty": float(qty)}
        )
        return gen

    return _next_gen


def _fold_planned_exits(lines: Iterable[Mapping[str, Any]]) -> dict[int, PlannedExit]:
    """Fold the append-only ``planned`` journal lines into ONE PlannedExit per
    NETTED uic (saxo-oco memo §7) — PLAN PRICES only, NEVER a protected set.

    Protection is derived from live broker state every tick (Tasks 5/6); no
    journal line confers it, so ``intent`` / ``placed`` lines contribute nothing
    here. Keying is per-uic (the unit Saxo nets to), never per-client_request_id.

    Governing rules (memo §8):
      - disaster stop = the MAX stop for a long (tightest) — defensive if
        journaled tiers disagree;
      - TP + entry_crid = the SHALLOWEST tier (min ``tier_index``), so the
        deterministic ref is fill-order-independent; two plans at that tier
        (the conflicting shape) are broken by ``client_request_id`` (#1328) —
        a property of the data, so the boot compactor's crid-sorted rewrite
        cannot change which plan governs, and neither can any other order;
      - a repeated ``tier_index`` on one uic reveals >1 distinct plan (each plan
        owns exactly one tier per index) -> ``conflicting`` so Task 5 refuses to
        merge. Malformed lines are skipped."""
    # Latest planned line per entry tier (append-only: highest gen wins per crid).
    latest_by_crid = _latest_planned_by_crid(lines)

    tiers_by_uic: dict[int, list[Mapping[str, Any]]] = {}
    for _gen, line in latest_by_crid.values():
        tiers_by_uic.setdefault(int(line["uic"]), []).append(line)

    result: dict[int, PlannedExit] = {}
    for uic, tiers in tiers_by_uic.items():
        index_counts: dict[int, int] = {}
        for line in tiers:
            idx = int(line.get("tier_index", 0))
            index_counts[idx] = index_counts.get(idx, 0) + 1
        n_plans = max(index_counts.values())
        stop_price = max(float(line["stop_price"]) for line in tiers)
        governing = min(
            tiers,
            key=lambda line: (int(line.get("tier_index", 0)), str(line["client_request_id"])),
        )
        tp_raw = governing.get("take_profit")
        result[uic] = PlannedExit(
            uic=uic,
            entry_crid=str(governing["client_request_id"]),
            side=str(governing.get("side", _DISASTER_STOP_SIDE)),
            stop_price=stop_price,
            tp_price=None if tp_raw is None else float(tp_raw),
            conflicting=n_plans > 1,
            n_plans=n_plans,
            next_gen=_make_next_gen(uic),
            next_amend_seq=_make_next_amend_seq(uic),
            reaction=_reaction_from_governing(governing),
        )
    return result


def _reaction_from_governing(governing: Mapping[str, Any]) -> Any:
    """Fold the governing planned line's ``"reaction"`` stamp (#1236) into the
    declared reaction primitive, or ``None`` when the key is absent or will not
    decode.

    A malformed optional key must NOT take the line with it. ``_fold_planned_exits``
    is called inside ``build_protection_view``, and ``_run_protection_pass``
    catches only ``BrokerError`` — so an unguarded decoder here would escape the
    whole protection pass and leave every position unmanaged for that tick. The
    line still carries the disaster stop, which is the never-naked guarantee, so
    losing the declaration is survivable and losing the line is not.

    ``None`` for every line written before #1236, which is why those plans resolve
    to "the stop is never moved"."""
    raw = governing.get("reaction")
    if not isinstance(raw, dict):
        return None
    try:
        return _decode_reaction_primitive(raw)
    except (TypeError, ValueError):  # TradeIntentDecodeError is a ValueError
        logger.warning(
            "planned line for uic %s carries an undecodable reaction stamp %r — "
            "treating the pick as declaring nothing (its stop will not be moved)",
            governing.get("uic"),
            raw,
        )
        return None


# --- Live-exit TP-tranche ladder persistence (INC-5 Task 1) ------------------
# The `planned` journal line above carries only a scalar `take_profit` -- not
# enough for the live-exit engine (`live_exit_engine.ManagedExit`), which needs
# the FULL tp_tranches tuple + the tranche-sizing base (`reference_qty`) to
# rebuild a managed position from the journal alone. `_build_tranche_plan_line`
# / `fold_tranche_plans` persist that ladder per uic, append-only, mirroring the
# `_build_planned_line` / `_fold_planned_exits` pattern. INERT here: nothing
# reads the fold until the live-exits tick phase (Task 2) is wired.


_TRANCHE_FIRED_KIND = "tranche_fired"


def _build_tranche_plan_line(
    *,
    uic: int,
    tp_tranches: tuple[TpTranchePlan, ...],
    reference_qty: float,
    stop_price: float,
    pick_key: str | None = None,
    instrument_currency: str | None = None,
    sizing_currency: str | None = None,
    exchange_mic: str | None = None,
) -> dict[str, Any]:
    """One append-only ``tranche_plan`` journal line -- the per-uic TP ladder the
    live-exit engine needs (INC-5) but the ``planned`` line does not carry.
    JSON-serializable (each ``TpTranchePlan`` is decomposed to a plain dict).
    Confers no protection by itself -- only the tranche reference prices/pcts,
    the sizing base, and the stop price the engine amends the standalone SL
    around. Written ONCE per placement (never per tier); a same-uic re-arm
    simply appends a newer line (append-only fold, last well-formed line wins,
    exactly like ``_build_planned_line``).

    ``pick_key`` (2026-08-19 adjudication finding 4) is the plan's trade
    identity: the entry-trail watch routing stamps ``ticker:trade_date`` so
    :func:`_fold_fired_since_latest_plan` treats a crash-recovery re-drive's
    re-append as the SAME trade (no fired-set reset). ``None`` (the bracket
    path) omits the key -- a keyless line keeps today's always-reset
    semantics."""
    line: dict[str, Any] = {
        "kind": _TRANCHE_PLAN_KIND,
        "uic": int(uic),
        "tp_tranches": [
            {
                "tranche_index": int(t.tranche_index),
                "target_price": float(t.target_price),
                "tranche_frac": float(t.tranche_frac),
                "r_multiple": float(t.r_multiple),
                "tag": str(t.tag),
            }
            for t in tp_tranches
        ],
        "reference_qty": float(reference_qty),
        "stop_price": float(stop_price),
    }
    if pick_key is not None:
        line["pick_key"] = str(pick_key)
    # #1238 PR 3: the currency pair the #1112 exit gate prices the round trip
    # with. Written only when known -- an absent stamp folds to the
    # conservative legacy facts, exactly like a pre-#1238 line.
    if instrument_currency:
        line["instrument_currency"] = str(instrument_currency)
    if sizing_currency:
        line["sizing_currency"] = str(sizing_currency)
    # #1271: the venue MIC joins the currency stamps so the gates can pick the
    # venue's OWN fee card (Xetra vs Euronext are both EUR with different
    # minimums). Absent on legacy lines -> the currency fallback in
    # ``fee_card_for`` governs, exactly like a pre-#1271 line.
    if exchange_mic:
        line["exchange_mic"] = str(exchange_mic)
    return line


def _fold_currency_stamp_line(
    line: Mapping[str, Any], out: dict[int, tuple[str | None, str | None, str | None]]
) -> None:
    """Fold one journal line into the currency-stamp map (in place): a
    retraction pops the uic, a well-formed ``tranche_plan`` overwrites it
    (last wins), everything else is ignored."""
    kind = line.get("kind")
    if kind == _TRANCHE_PLAN_RETRACTED_KIND:
        retracted_uic = _coerce(line, "uic", int)
        if retracted_uic is not None:
            out.pop(retracted_uic, None)
        return
    if kind != _TRANCHE_PLAN_KIND:
        return
    if _parse_tranche_plan_line(line) is None:
        return
    uic = _coerce(line, "uic", int)
    if uic is None:
        return
    instrument_ccy = line.get("instrument_currency")
    sizing_ccy = line.get("sizing_currency")
    mic = line.get("exchange_mic")
    out[uic] = (
        str(instrument_ccy) if instrument_ccy else None,
        str(sizing_ccy) if sizing_ccy else None,
        str(mic) if mic else None,
    )


def _terminal_watch_picks(
    fold: entry_trails.EntryTrailFold,
) -> dict[str, tuple[int, bool, tuple[str, ...]]]:
    """``{pick_key: (uic, any_tier_fired, tier_crids)}`` for every pick whose
    entry watch is FULLY terminal (2026-08-19 adjudication finding 3 / #1223).
    A pick with any still-open or ARMED tier is live (``terminal_kind is None``
    — an armed sibling skipped by the #1198 retire still owns a resting native
    BUY, so the first skip also encodes #1223's "never retract while any tier
    is open or armed"). ``tier_crids`` carries the pick's tier watch crids so
    the planned-line retraction (#1249) can derive the fire-crids without a
    second fold walk. Records whose pick_key or uic cannot be reconstructed are
    skipped — never retract on doubt."""
    states_by_pick: dict[str, list[tuple[str, entry_trails.EntryTrailTierState]]] = {}
    for crid, state in fold.tiers.items():
        record = state.watch_open
        if record is None:
            continue
        key = record.get("pick_key")
        if key is None:
            continue
        states_by_pick.setdefault(str(key), []).append((str(crid), state))
    out: dict[str, tuple[int, bool, tuple[str, ...]]] = {}
    for pick_key, crid_states in states_by_pick.items():
        if any(s.terminal_kind is None for _crid, s in crid_states):
            continue  # a tier still watches / arms — the pick is live
        uics = {_coerce(s.watch_open or {}, "uic", int) for _crid, s in crid_states}
        if len(uics) != 1 or None in uics:
            continue  # unmappable / inconsistent — never retract on doubt
        fired = any(s.terminal_kind == entry_trails.KIND_FIRED for _crid, s in crid_states)
        crids = tuple(sorted(crid for crid, _s in crid_states))
        out[pick_key] = (cast(int, next(iter(uics))), fired, crids)
    return out


def _retract_planned_for_verdicts(verdicts: Iterable[ReconcileVerdict]) -> None:
    """#1249 class (c): retract the ``planned`` line of every bracket whose
    verdict proves the entry TERMINALLY NEVER FILLED (CANCELLED / REJECTED /
    EXPIRED with no fill evidence). Per-order proof, not per-uic state: an
    entry that provably never filled has a planned line that covers nothing
    regardless of what else lives on the uic — exactly the stale-UUID-crid
    class the entry-trail sweeps cannot reach. Any ``filled_quantity`` in the
    verdict details vetoes (a partial fill leaves a position); UNRESOLVED /
    UNKNOWN outcomes never retract; a missing crid never retracts."""
    crids = sorted(
        {
            str(v.details["client_request_id"])
            for v in verdicts
            if v.status in _TERMINAL_NON_FILLED
            and not v.details.get("filled_quantity")
            and v.details.get("client_request_id")
        }
    )
    if crids:
        _retract_planned_lines(crids, note="entry verdict: terminal without a fill")


def _mark_oco_unsupported(uic: int) -> None:
    """Persist the per-instrument OCO-unsupported capability flag (saxo-oco memo §7).

    Append one out-of-band ``oco_unsupported`` line keyed by int uic. Written by
    the Stage-2 executor when ``place_oco_exit`` fails (any BrokerError — a
    structural ``SellOrdersAlreadyExist`` / ``TooFarFromEntry`` reject, a rate
    limit, or a 202) so the rung 1 -> 2 upgrade is never re-attempted on that uic,
    even after a systemd restart — the rung-1 stop stays the proven terminal rung.
    ``_fold_oco_unsupported`` reads these lines back into
    ``build_protection_view``'s ``ProtectionView.oco_unsupported``."""
    _append_standalone_stop_journal({"kind": "oco_unsupported", "uic": int(uic)})


def _journal_oco_too_far(uic: int, *, clock: Callable[[], float] = time.time) -> None:
    """Persist a timestamped ``oco_too_far`` marker (overnight-drift memo, action 5).

    Written by the B0 executor on a clean ``TooFarFromMarket`` OCO reject
    INSTEAD of the permanent ``oco_unsupported`` flag. ``build_protection_view``
    unions markers newer than ``_OCO_TOO_FAR_TTL_S`` into the EXISTING
    ``ProtectionView.oco_unsupported`` set, so downstream B0 logic is untouched
    and the uic automatically becomes OCO-eligible again for fresh fills once
    the TTL expires."""
    _append_standalone_stop_journal({"kind": "oco_too_far", "uic": int(uic), "ts": float(clock())})


def _journal_oco_placed(uic: int, *, clock: Callable[[], float] = time.time) -> None:
    """Persist a timestamped ``oco_placed`` marker (saxo Stage-3 memo, H1b/A1).

    Written by the executor ONLY on a CONFIRMED 2xx B0 OCO placement.
    ``build_protection_view`` folds markers newer than ``_OCO_PLACED_TTL_S`` into
    ``ProtectionView.oco_recently_placed`` so a second B0 cannot double-commit atop
    a resting OCO pair that live list-orders has not yet surfaced. The ``clock``
    seam keeps the marker's ``ts`` testable (default wall clock)."""
    _append_standalone_stop_journal({"kind": "oco_placed", "uic": int(uic), "ts": float(clock())})


def _journal_amend_failed(uic: int, *, clock: Callable[[], float] = time.time) -> None:
    """Persist a timestamped ``amend_failed`` marker (saxo Stage-3 memo, A4).

    Written by the executor on ANY AmendStop failure. Folded (within
    ``_AMEND_FAILED_TTL_S``) into ``ProtectionView.amend_recently_failed`` so the
    NEXT tick's grow/downsize arm SKIPS amend and falls to the proven B1 additive /
    place-residual-first primitive. NOT a permanent latch — a benign fill-race 400
    self-clears after the TTL and amend is retried."""
    _append_standalone_stop_journal({"kind": "amend_failed", "uic": int(uic), "ts": float(clock())})


def _journal_stop_placed(
    uic: int,
    qty: float,
    *,
    order_id: str,
    ref: str,
    stop_price: float | None = None,
    clock: Callable[[], float] = time.time,
) -> None:
    """Persist a timestamped ``stop_placed`` outcome record.

    Written by the executor ONLY on a confirmed standalone-stop placement, with the
    qty ACTUALLY placed (post execute-time clamp). Since #1219 the record also
    carries the broker ``order_id`` (the ONLY durable handle the stop-fill
    reconcile pass can later compare against the open-orders book — no other
    journal line retains it) and the deterministic ``ref``
    (``PlaceStop.request_id``) that renders the operator label on the fill alert.
    ``_fold_standing_stop_ids`` consumes both; fill-to-protection latency stays
    measurable as before (``oco_placed`` covers the OCO path). ``stop_price`` is
    the level the stop was placed at, so the fill alert names it as a fact
    (#1621); omitted when absent, and a record written before it reads as "level
    unknown". The ``clock`` seam keeps the record's ``ts`` testable (default wall
    clock)."""
    record: dict[str, Any] = {
        "kind": "stop_placed",
        "uic": int(uic),
        "qty": float(qty),
        "order_id": str(order_id),
        "ref": str(ref),
    }
    if stop_price is not None:
        record["stop_price"] = float(stop_price)
    record["ts"] = float(clock())
    _append_standalone_stop_journal(record)


def _journal_stop_filled(
    uic: int,
    *,
    order_id: str,
    qty: float,
    avg_price: float | None,
    ref: str | None = None,
    partial: bool = False,
    clock: Callable[[], float] = time.time,
) -> None:
    """Append the terminal ``stop_filled`` line for a reconciled stop fill (#1219).

    The top-level ``order_id`` is the idempotence latch: once written,
    ``_fold_standing_stop_ids`` excludes the uic, so the reconcile pass never
    re-resolves or re-alerts the same fill (restart-safe by construction, like
    the entry-side ``fired`` line). ``qty`` / ``avg_price`` carry the realized
    exit the offline exec-quality join needs; ``avg_price`` may be ``None`` when
    the audit row's price is unparseable — the fill still terminates."""
    _append_standalone_stop_journal(
        {
            "kind": "stop_filled",
            "uic": int(uic),
            "order_id": str(order_id),
            "qty": float(qty),
            "avg_price": avg_price if avg_price is None else float(avg_price),
            # Durable retire-trigger fields (#1198 crash window): `ref` carries
            # the generation-exact pick attribution into the restart-safe
            # sweep; `partial` (PARTIALLY_FILLED terminal — residual exposure
            # remains) marks a fill that must announce but never retire.
            "ref": ref,
            "partial": bool(partial),
            "ts": float(clock()),
        }
    )


def _journal_amend_ok(uic: int, qty: float, *, clock: Callable[[], float] = time.time) -> None:
    """Persist a timestamped ``amend_ok`` outcome record (observability-only).

    Written by the executor ONLY on a confirmed AmendStop PATCH, with the qty the
    stop was amended to (the live-clamped absolute target). Read by nothing in the
    protection logic — no fold consumes it — it exists so fill-to-protection latency
    is measurable on the amend path (``amend_failed`` already covers failures). The
    ``clock`` seam keeps the record's ``ts`` testable (default wall clock)."""
    _append_standalone_stop_journal(
        {"kind": "amend_ok", "uic": int(uic), "qty": float(qty), "ts": float(clock())}
    )


def _journal_reanchored(
    uic: int,
    avg_price: float,
    *,
    stop_price: float | None = None,
    clock: Callable[[], float] = time.time,
) -> None:
    """Persist a timestamped ``reanchored`` marker (PR-6b, broker-manager
    extraction memo §4.3). Written by the executor ONLY on a CONFIRMED
    reanchor AmendStop PATCH success (never on a failed attempt — a failed
    amend journals ``amend_failed`` like any other amend and simply retries).
    ``_fold_reanchored_markers`` folds these into
    ``ProtectionView.reanchored_by_uic``, the PERMANENT per-blend idempotence
    latch (no TTL — unlike ``oco_placed`` / ``amend_failed``, a confirmed
    reanchor for a given avg_price never needs to re-fire for that same
    blend).

    ``stop_price`` is the LEVEL the PATCH actually placed — already through the
    never-below-brief-floor envelope, so it can never sit under
    ``plan.stop_price``. It is recorded because the latch alone was not enough
    (#1518): ``_build_managed_exits`` raised its stop only from the ``trailed``
    fold, so a re-anchored uic carried the placement-time plan stop into
    ``ManagedExit``, and the amend that frees the first take-profit tranche
    wrote that stale level back to the broker. Optional and omitted when
    absent, so a marker written before #1518 folds to no floor and its uic
    keeps the old behaviour rather than inventing one."""
    record: dict[str, Any] = {
        "kind": "reanchored",
        "uic": int(uic),
        "avg_price": float(avg_price),
    }
    if stop_price is not None:
        record["stop_price"] = float(stop_price)
    record["ts"] = float(clock())
    _append_standalone_stop_journal(record)


def _journal_trailed(
    uic: int,
    level: float,
    *,
    peak: float | None = None,
    last_price: float | None = None,
    clock: Callable[[], float] = time.time,
) -> None:
    """Persist a timestamped ``trailed`` marker (Task 4). Written by the executor
    ONLY on a CONFIRMED trail AmendStop PATCH success (never on a failed attempt —
    a failed amend journals ``amend_failed`` like any other amend and simply
    retries). Mirrors ``_journal_reanchored``: ``_fold_trailed_since_latest_plan`` folds
    these into ``ProtectionView.trailed_stop_by_uic``, the never-DOWN ratchet floor
    a new trail proposal must clear by ``stop_decision.TRAIL_STEP_EPS``.

    ``level`` is the stop price actually placed (the ratchet floor, read by the
    fold). ``peak`` / ``last_price`` are the high-water mark and live price the
    trail was computed from — telemetry substrate for the future /edge trailing
    lens; the fold ignores them, so a missing one is harmless (omitted here).
    """
    marker: dict[str, Any] = {
        "kind": "trailed",
        "uic": int(uic),
        "level": float(level),
        "ts": float(clock()),
    }
    if peak is not None:
        marker["peak"] = float(peak)
    if last_price is not None:
        marker["last_price"] = float(last_price)
    _append_standalone_stop_journal(marker)


def _journal_envelope_clamped(
    uic: int,
    *,
    policy: str,
    proposed: float,
    clamped: float,
    prior_stop: float,
    avg_price: float,
    clock: Callable[[], float] = time.time,
) -> None:
    """Persist a timestamped ``envelope_clamped`` telemetry record (#1015, INC-2
    memo section 7). Written by the executor ONLY on a CONFIRMED reanchor
    AmendStop PATCH success whose proposed target the never-below-brief-floor
    envelope clamped (``position_manager._maybe_reanchor`` stamps the
    ``envelope_*`` facts on the action; a divergence-free reanchor carries
    ``None`` and never writes this). Read by nothing in the protection logic —
    no fold consumes it (like ``amend_ok``), so the boot compactor drops it at
    restart. It is the ONLY one of the three amend-outcome markers that is pure
    telemetry: ``reanchored`` and ``trailed`` ARE fold-consumed and the
    compactor keeps them (#1324)."""
    _append_standalone_stop_journal(
        {
            "kind": "envelope_clamped",
            "uic": int(uic),
            "policy": str(policy),
            "proposed": float(proposed),
            "clamped": float(clamped),
            "prior_stop": float(prior_stop),
            "avg_price": float(avg_price),
            "ts": float(clock()),
        }
    )


def _select_trailed_lines(lines: Iterable[Mapping[str, Any]]) -> dict[int, Mapping[str, Any]]:
    """The ONE ``trailed`` marker still governing each uic — newest by ``ts``
    within the CURRENT plan generation.

    The single source of truth for both consumers: ``_fold_trailed_since_latest_plan``
    reads the level off these lines and ``_elect_trailed_lines`` keeps the lines
    themselves. Before #1236 the two ran the same logic twice and a comment asked
    the next author to keep them in step; a compaction that elects a different set
    from the fold either drops a live ratchet floor or resurrects a dead one,
    which is #1324. Sharing makes that structural.

    The generation reset now observes ``planned`` lines as well as ``tranche_plan``
    ones (#1236). A pick with no take-profit journals NO ``tranche_plan``, so under
    the tranche-only rule a level trailed by an EARLIER position on the uic
    survived into it — measured, and no take-profit is precisely the trail-only shape
    a trailing pick uses. ``planned`` lines are written by every pick, which is
    why they are the ones that close it.

    ORDERING. The reset is write-order-based, and that is safe because the
    ELECTION runs on the journal in write order, before the compactor reorders
    anything: a marker the election drops is simply not in the rewritten file. A
    marker is skipped WITHOUT advancing the newest-ts cursor when its ``level`` or
    ``ts`` will not parse, so a malformed newer line can never evict a good older
    one."""
    latest_ts: dict[int, float] = {}
    latest_line: dict[int, Mapping[str, Any]] = {}
    governing_key: dict[int, str] = {}
    for line in lines:
        uic = _coerce(line, "uic", int)
        if uic is None:
            continue
        kind = line.get("kind")
        if _apply_generation_reset(
            kind, line, uic, governing_key, (latest_ts, latest_line), include_planned=True
        ):
            continue
        if kind != "trailed":
            continue
        try:
            float(line["level"])  # parse guard; the level itself is read by the caller
            ts = float(line["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if uic not in latest_ts or ts >= latest_ts[uic]:
            latest_ts[uic] = ts
            latest_line[uic] = line
    return latest_line


def _read_persisted_amend_seq(uic: int) -> int:
    """The highest ``amend_seq`` recorded for ``uic`` in the append-only journal, or
    ``-1`` when the uic has never been amend-sequenced (so the first seq is 0)."""
    seq = -1
    for line in _iter_standalone_stop_journal():
        if line.get("kind") != "amend_seq":
            continue
        try:
            if int(line["uic"]) != uic:
                continue
            seq = max(seq, int(line["seq"]))
        except (KeyError, TypeError, ValueError):
            continue
    return seq


def _make_next_amend_seq(uic: int) -> Callable[[], int]:
    """A per-uic MONOTONIC amend-sequence bound to the journal (saxo Stage-3 memo).

    Returns ``max+1`` ALWAYS (never qty-keyed), so a genuine re-resize to a
    previously-seen target qty gets a FRESH ``-amend-<seq>`` ref and is never
    dedup-swallowed by Saxo's 15s request-id window (mitigation A3/H3). Absolute-
    target semantics make a cross-tick re-emit safe (two sets of Amount=owned =
    owned, never 2x), so monotonic-not-qty-keyed never double-commits. The bump is
    appended, never rewritten, so the counter survives a systemd restart."""

    def _next_seq() -> int:
        seq = _read_persisted_amend_seq(uic) + 1
        _append_standalone_stop_journal({"kind": "amend_seq", "uic": int(uic), "seq": int(seq)})
        return seq

    return _next_seq


def _track_tranche_plan(
    line: Mapping[str, Any],
    uic: int,
    *,
    ladder_line: dict[int, Mapping[str, Any]],
    latest_plan: dict[int, Mapping[str, Any]],
    governing_key: dict[int, str],
    fired_lines: dict[int, list[Mapping[str, Any]]],
    closure_lines: dict[int, dict[str | None, Mapping[str, Any]]],
) -> None:
    """Advance the tranche-compaction state for one ``tranche_plan`` line,
    delegating the identity-keyed reset to ``_apply_generation_reset`` (the
    live folds' single implementation): a keyless line or a ``pick_key``
    differing from the uic's governing key resets the fired and closure
    accumulators; a same-key re-append (the crash-recovery re-drive) does
    not. The line becomes the uic's latest plan always, and its
    ladder-governing plan only when ``fold_tranche_plans`` accepts it as
    well-formed (a corrupt line still resets fired but never governs the
    ladder — the folds' own semantics)."""
    _apply_generation_reset(
        _TRANCHE_PLAN_KIND, line, uic, governing_key, (fired_lines, closure_lines)
    )
    latest_plan[uic] = line
    if fold_tranche_plans([line]):
        ladder_line[uic] = line


def _track_non_plan_tranche_line(
    line: Mapping[str, Any],
    kind: Any,
    uic: int,
    *,
    ladder_line: dict[int, Mapping[str, Any]],
    latest_plan: dict[int, Mapping[str, Any]],
    governing_key: dict[int, str],
    fired_lines: dict[int, list[Mapping[str, Any]]],
    closure_lines: dict[int, dict[str | None, Mapping[str, Any]]],
) -> None:
    """Fold one retraction / ``stop_filled`` / ``tranche_fired`` line into the
    election trackers (in place). The caller pre-filters kinds, so the final
    branch only ever sees ``tranche_fired`` lines."""
    if kind == _TRANCHE_PLAN_RETRACTED_KIND:
        for tracker in (ladder_line, latest_plan, governing_key, fired_lines, closure_lines):
            tracker.pop(uic, None)
    elif kind == "stop_filled":
        # Closure evidence only while the generation is open — mirroring
        # the closure fold's own gate (a pre-plan fill never counts).
        if uic in latest_plan and not line.get("partial"):
            ref = line.get("ref")
            key = _pick_key_from_stop_ref(ref if isinstance(ref, str) else None)
            closure_lines.setdefault(uic, {})[key] = line
    elif line.get("tag") or line.get("position_closed"):
        fired_lines.setdefault(uic, []).append(line)


def _track_stop_filled_by_id(
    line: Mapping[str, Any], stop_filled_by_id: dict[str, tuple[float, dict[str, Any]]]
) -> None:
    """Elect the newest ``stop_filled`` per order id (ts tie keeps the later
    line), in place; a line without a usable order id / ts contributes nothing."""
    order_id = line.get("order_id")
    ts = _coerce(line, "ts", float)
    if isinstance(order_id, str) and order_id and ts is not None:
        kept = stop_filled_by_id.get(order_id)
        if kept is None or ts >= kept[0]:
            stop_filled_by_id[order_id] = (ts, dict(line))
