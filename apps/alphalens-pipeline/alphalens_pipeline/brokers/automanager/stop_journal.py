"""The standalone-stop journal: its path, its line parsers and its folds.

Extracted from ``control_loop`` unchanged (architecture audit 2026-10-02,
finding 2 / #1677, step 2). This is the layer the rest of the partition was
waiting on: before the move, every large cluster still reachable for
extraction -- the placement drain, the entry-watch pass, the protection
executor -- called back into one of these helpers, so moving any of them would
have recreated the cycle the move is meant to remove. They come out first and
nothing here calls back.

One journal, two kinds of line. ``state_paths.standalone_stops_path()`` is the
single seam (ADR 0016); the ``planned`` lines carry the prices the broker
cannot know, and the ``tranche_plan`` lines carry the take-profit ladder. No
journal line confers protection -- the protection pass derives that from live
broker state -- so these folds answer "what did we intend", never "what is
armed".

``_coerce`` lives here because its own docstring says what it is: "the 'skip
this malformed field' primitive for the journal compactor".
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphalens_pipeline.brokers.automanager import state_paths

if TYPE_CHECKING:  # annotations only; the runtime construction imports it in-body
    from broker_contract.sizing import TpTranchePlan

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


def _coerce(line: Mapping[str, Any], key: str, caster: Callable[[Any], Any]) -> Any:
    """Cast ``line[key]`` via ``caster``, or return None if the key is missing or
    the value is uncastable — the "skip this malformed field" primitive for the
    journal compactor."""
    try:
        return caster(line[key])
    except (KeyError, TypeError, ValueError):
        return None
