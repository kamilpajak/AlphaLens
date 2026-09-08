"""Entry-trailing scaffolding (PR-T0, entry-trailing design memo §5/§6) —
the flag reader, the ``entry_trails.jsonl`` fold, the virtual watching-gross
valuation, and the startup compaction.

PR-T0 is strictly INERT: nothing here places orders or runs a watcher. The
tests pin the three money-relevant contracts the later PRs build on:

- the runtime flag reader fails CLOSED to ``0`` (feature OFF) on any
  malformed / out-of-bounds value — today's behavior, never a live trail;
- malformed journal lines and records missing ``crid`` are COUNTED and
  surfaced by the fold (the gross-cap consumer fails closed on them), never
  silently dropped;
- compaction is fold-equivalent per kind and PRESERVES UNKNOWN KINDS
  VERBATIM (memo G4 — the standalone-stops compactor eats unknown kinds and
  that bug class must not recur here), with O(1) output per crid.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import entry_trails as et

_CRID = "KO:2026-08-12:T1-entry-0"


def _line(kind: str, crid: str | None = _CRID, **fields: Any) -> str:
    record: dict[str, Any] = {"kind": kind}
    if crid is not None:
        record["crid"] = crid
    record.update(fields)
    return json.dumps(record, sort_keys=True)


def _watch_open(
    crid: str = _CRID,
    *,
    limit: Any = 10.0,
    qty: Any = 100,
    fx_rate: Any = None,
    **extra: Any,
) -> str:
    return _line(
        et.KIND_WATCH_OPEN,
        crid,
        limit=limit,
        qty=qty,
        d_bps=50,
        window_end="2026-08-21",
        fx_rate=fx_rate,
        **extra,
    )


class TestEntryTrailBpsReader(unittest.TestCase):
    """``entry_trail_bps`` — unset/blank/malformed/out-of-bounds all fail
    CLOSED to 0 (feature OFF, today's behavior); explicit ``"0"`` is valid."""

    def setUp(self) -> None:
        # The invalid-flag warning is latched once per process (the reader runs
        # every daemon tick); reset the latch so each test observes its warning.
        self.enterContext(mock.patch.object(et, "_entry_trail_bps_warned", False))

    def _read(self, value: str | None) -> int:
        env = {} if value is None else {et.ENTRY_TRAIL_BPS_ENV: value}
        with mock.patch.dict("os.environ", env, clear=True):
            return et.entry_trail_bps()

    def test_env_var_name_and_bound(self) -> None:
        self.assertEqual(et.ENTRY_TRAIL_BPS_ENV, "ALPHALENS_BROKER_ENTRY_TRAIL_BPS")
        # Memo §6: bound 150, NOT 300 — the replay's edge is dead by d≈2-3%,
        # so the bound must exclude the measured-bad region.
        self.assertEqual(et.ENTRY_TRAIL_BPS_MAX, 150)

    def test_unset_is_off(self) -> None:
        self.assertEqual(self._read(None), 0)

    def test_blank_is_off(self) -> None:
        self.assertEqual(self._read("   "), 0)

    def test_explicit_zero_is_valid_off(self) -> None:
        self.assertEqual(self._read("0"), 0)

    def test_in_bounds_values_pass_through(self) -> None:
        self.assertEqual(self._read("50"), 50)
        self.assertEqual(self._read("150"), 150)

    def test_above_bound_fails_closed_with_warning(self) -> None:
        with self.assertLogs(et.logger, level="WARNING"):
            self.assertEqual(self._read("151"), 0)

    def test_negative_fails_closed_with_warning(self) -> None:
        with self.assertLogs(et.logger, level="WARNING"):
            self.assertEqual(self._read("-1"), 0)

    def test_malformed_fails_closed_with_warning(self) -> None:
        with self.assertLogs(et.logger, level="WARNING"):
            self.assertEqual(self._read("abc"), 0)

    def test_invalid_value_warns_once_per_process(self) -> None:
        # The reader runs on EVERY daemon tick (~45s); an invalid flag must
        # warn once, not spam the journal for the whole daemon lifetime.
        with self.assertLogs(et.logger, level="WARNING") as captured:
            self.assertEqual(self._read("abc"), 0)
            self.assertEqual(self._read("abc"), 0)
            self.assertEqual(self._read("999"), 0)
        self.assertEqual(len(captured.records), 1, "the warn latch fires once per process")


class TestKindWhitelist(unittest.TestCase):
    def test_whitelist_and_terminal_set(self) -> None:
        self.assertEqual(
            et.ENTRY_TRAIL_KINDS,
            frozenset(
                {
                    "watch_open",
                    "touched",
                    "trough",
                    "trail_armed",
                    "fired",
                    "expired",
                    "suspended",
                    "cancelled",
                }
            ),
        )
        self.assertEqual(
            et.ENTRY_TRAIL_TERMINAL_KINDS,
            frozenset({"fired", "expired", "suspended", "cancelled"}),
        )
        self.assertTrue(et.ENTRY_TRAIL_TERMINAL_KINDS <= et.ENTRY_TRAIL_KINDS)


class TestFoldEntryTrailLines(unittest.TestCase):
    def test_empty_input_folds_empty(self) -> None:
        fold = et.fold_entry_trail_lines([])
        self.assertEqual(fold.tiers, {})
        self.assertEqual(fold.malformed, 0)

    def test_latest_non_terminal_kind_wins_in_file_order(self) -> None:
        fold = et.fold_entry_trail_lines(
            [
                _watch_open(),
                _line(et.KIND_TOUCHED),
                _line(et.KIND_TROUGH, trough=9.4),
                _line(et.KIND_TRAIL_ARMED, order_id="O-1", trigger=9.45),
            ]
        )
        state = fold.tiers[_CRID]
        self.assertEqual(state.latest_kind, et.KIND_TRAIL_ARMED)
        self.assertIsNone(state.terminal_kind)

    def test_terminal_marker_is_kept_alongside_the_last_state(self) -> None:
        fold = et.fold_entry_trail_lines(
            [_watch_open(), _line(et.KIND_TOUCHED), _line(et.KIND_FIRED, realized_qty=100)]
        )
        state = fold.tiers[_CRID]
        self.assertEqual(state.terminal_kind, et.KIND_FIRED)
        self.assertEqual(state.latest_kind, et.KIND_TOUCHED)

    def test_every_terminal_kind_marks_the_tier_terminal(self) -> None:
        for kind in sorted(et.ENTRY_TRAIL_TERMINAL_KINDS):
            with self.subTest(kind=kind):
                fold = et.fold_entry_trail_lines([_watch_open(), _line(kind)])
                self.assertEqual(fold.tiers[_CRID].terminal_kind, kind)

    def test_min_trough_is_the_minimum_ever_seen(self) -> None:
        # Not "the latest": a restart resumes trough = min(journaled, fresh)
        # (memo §5) — the fold must expose the running MINIMUM.
        fold = et.fold_entry_trail_lines(
            [
                _watch_open(),
                _line(et.KIND_TROUGH, trough=9.5),
                _line(et.KIND_TROUGH, trough=9.2),
                _line(et.KIND_TROUGH, trough=9.3),
            ]
        )
        self.assertEqual(fold.tiers[_CRID].min_trough, 9.2)

    def test_non_finite_or_non_positive_trough_never_folds(self) -> None:
        # A price must be finite and strictly positive; NaN/-inf/-1 troughs
        # are journal corruption and must not poison the running minimum
        # (NaN would freeze every later comparison, -inf would win it).
        fold = et.fold_entry_trail_lines(
            [
                _line(et.KIND_TROUGH, trough=float("nan")),
                _line(et.KIND_TROUGH, trough=float("-inf")),
                _line(et.KIND_TROUGH, trough=-1.0),
                _line(et.KIND_TROUGH, trough=9.5),
            ]
        )
        self.assertEqual(fold.tiers[_CRID].min_trough, 9.5)

    def test_latest_watch_open_record_wins(self) -> None:
        fold = et.fold_entry_trail_lines([_watch_open(qty=100), _watch_open(qty=40)])
        watch = fold.tiers[_CRID].watch_open
        self.assertIsNotNone(watch)
        assert watch is not None
        self.assertEqual(watch["qty"], 40)

    def test_latest_trail_armed_order_id_wins_and_null_folds_to_none(self) -> None:
        # The G3 write-ahead journals a null-id line BEFORE the POST, then the
        # real-id line after — latest wins.
        armed = et.fold_entry_trail_lines(
            [
                _watch_open(),
                _line(et.KIND_TRAIL_ARMED, order_id=None),
                _line(et.KIND_TRAIL_ARMED, order_id="O-9"),
            ]
        )
        self.assertEqual(armed.tiers[_CRID].armed_order_id, "O-9")
        reverted = et.fold_entry_trail_lines(
            [
                _watch_open(),
                _line(et.KIND_TRAIL_ARMED, order_id="O-9"),
                _line(et.KIND_TRAIL_ARMED, order_id=None),
            ]
        )
        self.assertIsNone(reverted.tiers[_CRID].armed_order_id)

    def test_latest_trail_armed_ceiling_wins_and_a_legacy_line_folds_to_none(self) -> None:
        # #1317: the ceiling the order was armed with is journaled, so a fill can
        # be measured against it. Same latest-wins shape as the order id — the
        # write-ahead carries the geometry value, the post-POST line the
        # tick-quantized value the adapter actually put on the wire.
        armed = et.fold_entry_trail_lines(
            [
                _watch_open(),
                _line(et.KIND_TRAIL_ARMED, order_id=None, ceiling=58.9504),
                _line(et.KIND_TRAIL_ARMED, order_id="O-9", ceiling=58.95),
            ]
        )
        self.assertEqual(armed.tiers[_CRID].armed_ceiling, 58.95)
        # Every tier armed before #1317 shipped has no ceiling on its line. That
        # must read as "unknown", never as a usable number.
        legacy = et.fold_entry_trail_lines(
            [_watch_open(), _line(et.KIND_TRAIL_ARMED, order_id="O-9")]
        )
        self.assertIsNone(legacy.tiers[_CRID].armed_ceiling)

    def test_a_corrupt_ceiling_folds_to_none_rather_than_a_bad_number(self) -> None:
        # Same SEMANTIC gate as trough/limit/fx_rate: a zero or negative ceiling
        # would make every fill look like a breach. (A numeric STRING is
        # accepted, as it is for every other price on this journal — the gate is
        # about the value, not its JSON type.)
        for bad in (0, -1.5, float("nan"), float("inf"), True, None):
            with self.subTest(ceiling=bad):
                fold = et.fold_entry_trail_lines(
                    [_watch_open(), _line(et.KIND_TRAIL_ARMED, order_id="O-9", ceiling=bad)]
                )
                self.assertIsNone(fold.tiers[_CRID].armed_ceiling)

    def test_latest_trail_armed_trigger_wins_and_a_legacy_line_folds_to_none(self) -> None:
        # The ADOPT path re-journals a line for an order a PREVIOUS tick POSTed,
        # so both arm-time numbers must come from that tick, not this one — a
        # record mixing an old ceiling with a fresh trigger describes no order
        # that ever existed.
        armed = et.fold_entry_trail_lines(
            [
                _watch_open(),
                _line(et.KIND_TRAIL_ARMED, order_id="O-9", trigger=58.8327, ceiling=58.95),
            ]
        )
        self.assertEqual(armed.tiers[_CRID].armed_trigger, 58.8327)
        legacy = et.fold_entry_trail_lines(
            [_watch_open(), _line(et.KIND_TRAIL_ARMED, order_id="O-9")]
        )
        self.assertIsNone(legacy.tiers[_CRID].armed_trigger)

    def test_re_opening_a_watch_clears_the_armed_ceiling(self) -> None:
        # The re-arm resets the arm state; a stale ceiling from the previous
        # session's order must not be compared against the next fill.
        fold = et.fold_entry_trail_lines(
            [
                _watch_open(),
                _line(et.KIND_TRAIL_ARMED, order_id="O-1", trigger=10.05, ceiling=10.07),
                _watch_open(awaiting_fresh_low=True),
            ]
        )
        self.assertIsNone(fold.tiers[_CRID].armed_ceiling)
        self.assertIsNone(fold.tiers[_CRID].armed_trigger)

    def test_re_opening_a_watch_clears_the_armed_order_id(self) -> None:
        # Memo §5 CRITICAL-2 re-arm: a DayOrder-cancelled tier is re-admitted to
        # the watch pass by re-appending watch_open, which must reset the arm
        # state — else the stale resting-order id lingers past the re-arm.
        fold = et.fold_entry_trail_lines(
            [
                _watch_open(),
                _line(et.KIND_TRAIL_ARMED, order_id="O-1", trigger=10.05),
                _watch_open(awaiting_fresh_low=True),
            ]
        )
        state = fold.tiers[_CRID]
        self.assertIsNone(state.armed_order_id, "re-opening the watch clears the resting-order id")
        self.assertEqual(state.latest_kind, et.KIND_WATCH_OPEN)

    def test_two_crids_fold_independently(self) -> None:
        fold = et.fold_entry_trail_lines(
            [_watch_open("crid-a"), _watch_open("crid-b"), _line(et.KIND_EXPIRED, "crid-b")]
        )
        self.assertIsNone(fold.tiers["crid-a"].terminal_kind)
        self.assertEqual(fold.tiers["crid-b"].terminal_kind, et.KIND_EXPIRED)

    def test_malformed_json_is_counted_never_dropped_silently(self) -> None:
        fold = et.fold_entry_trail_lines(["{not json", '["a", "list"]', _watch_open()])
        self.assertEqual(fold.malformed, 2)
        self.assertIn(_CRID, fold.tiers)

    def test_missing_or_blank_crid_is_counted(self) -> None:
        fold = et.fold_entry_trail_lines(
            [_line(et.KIND_TOUCHED, crid=None), _line(et.KIND_TOUCHED, crid="  ")]
        )
        self.assertEqual(fold.malformed, 2)
        self.assertEqual(fold.tiers, {})

    def test_blank_lines_are_skipped_not_malformed(self) -> None:
        fold = et.fold_entry_trail_lines(["", "   ", _watch_open()])
        self.assertEqual(fold.malformed, 0)

    def test_unknown_kind_is_ignored_by_the_fold(self) -> None:
        # An unknown kind (a NEWER binary's record) contributes nothing to
        # this fold — but the compactor must still preserve it (G4, below).
        fold = et.fold_entry_trail_lines([_watch_open(), _line("future_kind")])
        self.assertEqual(fold.tiers[_CRID].latest_kind, et.KIND_WATCH_OPEN)
        self.assertEqual(fold.malformed, 0)


class TestWatchingVirtualGrossAcct(unittest.TestCase):
    """The G5 virtual reservation: NON-terminal watch_open records valued at
    tier LIMIT x qty, folded into ACCOUNT currency through the record's own
    journaled ``fx_rate`` — mirrors ``_committed_working_gross_acct``."""

    def _value(self, lines: list[str]) -> tuple[float, int]:
        return et.watching_virtual_gross_acct(et.fold_entry_trail_lines(lines))

    def test_empty_fold_is_zero(self) -> None:
        self.assertEqual(self._value([]), (0.0, 0))

    def test_non_terminal_watch_opens_sum_limit_times_qty(self) -> None:
        total, bad = self._value(
            [_watch_open("crid-a", limit=10.0, qty=100), _watch_open("crid-b", limit=5.0, qty=40)]
        )
        self.assertAlmostEqual(total, 1_200.0)
        self.assertEqual(bad, 0)

    def test_fx_rate_divides_into_account_currency(self) -> None:
        # fx_rate is instrument-ccy per 1 account-ccy (submission_log schema
        # 2 direction, same as _committed_working_gross_acct): 1_000 USD at
        # rate 0.25 is 4_000 acct-ccy. A multiplication bug would yield 250.
        total, bad = self._value([_watch_open(limit=10.0, qty=100, fx_rate=0.25)])
        self.assertAlmostEqual(total, 4_000.0)
        self.assertEqual(bad, 0)

    def test_terminal_tiers_release_their_reservation(self) -> None:
        for kind in sorted(et.ENTRY_TRAIL_TERMINAL_KINDS):
            with self.subTest(kind=kind):
                total, bad = self._value([_watch_open(limit=10.0, qty=100), _line(kind)])
                self.assertEqual(total, 0.0)
                self.assertEqual(bad, 0)

    def test_unvaluable_watch_open_is_counted(self) -> None:
        total, bad = self._value([_watch_open(limit=None, qty=100)])
        self.assertEqual(total, 0.0)
        self.assertEqual(bad, 1)

    def test_non_terminal_tier_without_watch_open_is_counted(self) -> None:
        # A tier tracked only by a touched record has real (virtual)
        # exposure the fold cannot value — surfaced, never skipped.
        total, bad = self._value([_line(et.KIND_TOUCHED)])
        self.assertEqual(total, 0.0)
        self.assertEqual(bad, 1)

    def test_fold_malformed_count_propagates(self) -> None:
        total, bad = self._value(["{not json", _watch_open(limit=10.0, qty=10)])
        self.assertAlmostEqual(total, 100.0)
        self.assertEqual(bad, 1)

    def test_semantically_invalid_values_fail_closed_never_crash(self) -> None:
        # Castable but semantically invalid values are UNVALUABLE, never
        # summed and never a crash: fx_rate=0.0 would divide by zero (a
        # ZeroDivisionError here escapes _place_pick and aborts the tick
        # before protection runs); negative limit/qty/fx_rate would SHRINK
        # the reservation on a money gate; JSON true coerces to 1.0.
        cases = {
            "limit=-1": _watch_open(limit=-1.0),
            "limit=0": _watch_open(limit=0.0),
            "limit=inf": _watch_open(limit=float("inf")),
            "limit=true": _watch_open(limit=True),
            "qty=0": _watch_open(qty=0),
            "qty=-5": _watch_open(qty=-5),
            "fx_rate=0": _watch_open(fx_rate=0.0),
            "fx_rate=-2": _watch_open(fx_rate=-2.0),
            "fx_rate=nan": _watch_open(fx_rate=float("nan")),
        }
        for label, line in cases.items():
            with self.subTest(case=label):
                total, bad = self._value([line])
                self.assertEqual(total, 0.0)
                self.assertEqual(bad, 1)


def _rich_entry_trail_journal() -> list[str]:
    """Every whitelisted kind, two crids, redundant intermediates the
    compaction must fold away, plus an unknown kind and a malformed line."""
    return [
        _watch_open("crid-a", limit=10.0, qty=100),
        _line(et.KIND_TOUCHED, "crid-a"),
        _line(et.KIND_TROUGH, "crid-a", trough=9.5),
        _line(et.KIND_TROUGH, "crid-a", trough=9.2),
        _line(et.KIND_TROUGH, "crid-a", trough=9.3),
        _line(et.KIND_TRAIL_ARMED, "crid-a", order_id="O-1", trigger=9.25, ceiling=9.27),
        _watch_open("crid-b", limit=5.0, qty=40),
        _line(et.KIND_TOUCHED, "crid-b"),
        _line(et.KIND_EXPIRED, "crid-b"),
        _line("future_kind", "crid-a", payload="opaque"),
        "{not json",
    ]


def _fold_data(fold: et.EntryTrailFold) -> tuple[Any, int]:
    return (
        {
            crid: (
                s.crid,
                s.watch_open,
                s.latest_kind,
                s.min_trough,
                s.terminal_kind,
                s.armed_order_id,
                # #1317: in the equivalence tuple so a compactor that drops the
                # armed ceiling fails here instead of going quiet.
                s.armed_ceiling,
                s.armed_trigger,
                # #1376: the latest terminal record rides the fold (fired avg
                # price / realized qty, cancel note) — a compactor that kept
                # the marker but lost the record must fail here.
                s.terminal_record,
            )
            for crid, s in fold.tiers.items()
        },
        fold.malformed,
    )


class TestCompactEntryTrailLines(unittest.TestCase):
    def test_fold_is_identical_on_original_vs_compacted(self) -> None:
        original = _rich_entry_trail_journal()
        compacted = et.compact_entry_trail_lines(original)
        self.assertEqual(
            _fold_data(et.fold_entry_trail_lines(original)),
            _fold_data(et.fold_entry_trail_lines(compacted)),
        )

    def test_round_trip_per_kind_every_whitelisted_kind_survives(self) -> None:
        # THE G4 regression class: the standalone-stops compactor EATS kinds
        # missing from its whitelist. Every whitelisted entry-trail kind must
        # survive its own single-kind round trip.
        for kind in sorted(et.ENTRY_TRAIL_KINDS):
            with self.subTest(kind=kind):
                lines = [_watch_open(), _line(kind, trough=9.9)]
                compacted = et.compact_entry_trail_lines(lines)
                self.assertEqual(
                    _fold_data(et.fold_entry_trail_lines(lines)),
                    _fold_data(et.fold_entry_trail_lines(compacted)),
                )
                self.assertTrue(
                    any(json.loads(line).get("kind") == kind for line in compacted),
                    f"{kind} must survive compaction",
                )

    def test_unknown_kinds_are_preserved_verbatim(self) -> None:
        unknown = _line("future_kind", "crid-x", payload={"nested": [1, 2]})
        compacted = et.compact_entry_trail_lines([_watch_open(), unknown])
        self.assertIn(unknown, compacted)

    def test_malformed_lines_are_preserved_verbatim(self) -> None:
        # Dropping a malformed line would silently clear the fold's
        # fail-closed malformed count — compaction must keep it.
        compacted = et.compact_entry_trail_lines(["{not json", _watch_open()])
        self.assertIn("{not json", compacted)

    def test_growth_bound_output_is_o1_per_crid(self) -> None:
        lines = [_watch_open()]
        lines.extend(_line(et.KIND_TROUGH, trough=10.0 - i * 0.01) for i in range(500))
        compacted = et.compact_entry_trail_lines(lines)
        self.assertLessEqual(
            len(compacted),
            et.COMPACTED_LINES_PER_CRID_BOUND,
            "compacted output per crid must be O(1) regardless of input length",
        )
        self.assertEqual(
            _fold_data(et.fold_entry_trail_lines(lines)),
            _fold_data(et.fold_entry_trail_lines(compacted)),
        )

    def test_min_trough_record_survives_even_when_not_latest(self) -> None:
        lines = [
            _watch_open(),
            _line(et.KIND_TROUGH, trough=9.2),
            _line(et.KIND_TROUGH, trough=9.3),
        ]
        compacted = et.compact_entry_trail_lines(lines)
        self.assertEqual(et.fold_entry_trail_lines(compacted).tiers[_CRID].min_trough, 9.2)

    def test_non_finite_trough_lines_do_not_displace_the_real_min(self) -> None:
        # A NaN trough seen FIRST must not become the compactor's "min"
        # choice (NaN freezes every later <= comparison) and silently drop
        # the record holding the real minimum — fold-equivalence must hold
        # under corrupted trough values too.
        lines = [
            _line(et.KIND_TROUGH, trough=float("nan")),
            _line(et.KIND_TROUGH, trough=9.5),
            _line(et.KIND_TROUGH, trough=11.0),
        ]
        compacted = et.compact_entry_trail_lines(lines)
        self.assertEqual(
            _fold_data(et.fold_entry_trail_lines(compacted)),
            _fold_data(et.fold_entry_trail_lines(lines)),
        )
        self.assertEqual(et.fold_entry_trail_lines(compacted).tiers[_CRID].min_trough, 9.5)

    def test_relative_order_is_preserved(self) -> None:
        # The fold's latest-kind semantics are FILE-ORDER based; compaction
        # must emit kept lines in their original relative order.
        original = _rich_entry_trail_journal()
        compacted = et.compact_entry_trail_lines(original)
        positions = [original.index(line) for line in compacted]
        self.assertEqual(positions, sorted(positions))


class TestCompactionKeepsTheArmedLine(unittest.TestCase):
    """The counterexample the rich-journal fixture LACKS (#1317 review).

    ``_CompactionTracker`` elects the latest NON-TERMINAL record per crid, which
    preserves ``latest_kind`` but NOT the sticky fields a ``trail_armed`` line
    carries. Any non-terminal line landing after the arm therefore evicted the
    arm line, and with it ``armed_order_id`` (which owns the resting broker
    order) and ``armed_ceiling``. The state machine does not produce that
    ordering today — the watch pass drops a tier once it holds a real order id —
    so this was latent, not live. A latent way to lose the id of a resting BUY
    order is still a way to lose it, and the compactor's own contract says the
    output folds IDENTICALLY.
    """

    def test_a_line_after_the_arm_does_not_evict_the_arm(self) -> None:
        lines = [
            _watch_open(),
            _line(et.KIND_TOUCHED),
            _line(et.KIND_TROUGH, trough=9.5),
            _line(et.KIND_TRAIL_ARMED, order_id="O-1", trigger=9.55, ceiling=9.57),
            _line(et.KIND_TROUGH, trough=9.4),  # lands AFTER the arm
        ]
        compacted = et.compact_entry_trail_lines(lines)
        before = et.fold_entry_trail_lines(lines).tiers[_CRID]
        after = et.fold_entry_trail_lines(compacted).tiers[_CRID]
        self.assertEqual(after.armed_order_id, before.armed_order_id)
        self.assertEqual(after.armed_ceiling, before.armed_ceiling)
        self.assertEqual(after.latest_kind, before.latest_kind)
        self.assertEqual(after.min_trough, before.min_trough)

    def test_the_arm_line_costs_at_most_one_extra_kept_line(self) -> None:
        # The growth bound is a contract (memo G4 "growth bound from day one"),
        # so the extra kept record must be counted, not smuggled in.
        lines = [_watch_open()]
        for i in range(30):
            lines.append(_line(et.KIND_TROUGH, trough=9.9 - i * 0.01))
            lines.append(_line(et.KIND_TRAIL_ARMED, order_id=f"O-{i}", trigger=9.5, ceiling=9.52))
            lines.append(_line(et.KIND_TOUCHED))
        self.assertLessEqual(
            len(et.compact_entry_trail_lines(lines)), et.COMPACTED_LINES_PER_CRID_BOUND
        )


class TestCompactEntryTrailJournalFile(unittest.TestCase):
    def test_absent_file_is_noop_never_created(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "entry_trails.jsonl"
            with mock.patch.object(et, "_entry_trail_journal_path", lambda: journal):
                et.compact_entry_trail_journal()
            self.assertFalse(journal.exists())

    def test_empty_file_is_noop_never_truncated(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "entry_trails.jsonl"
            journal.write_text("", encoding="utf-8")
            with mock.patch.object(et, "_entry_trail_journal_path", lambda: journal):
                et.compact_entry_trail_journal()
            self.assertEqual(journal.read_text(encoding="utf-8"), "")

    def test_rewrite_shrinks_and_preserves_the_fold(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "entry_trails.jsonl"
            journal.write_text(
                "".join(line + "\n" for line in _rich_entry_trail_journal()), encoding="utf-8"
            )
            with mock.patch.object(et, "_entry_trail_journal_path", lambda: journal):
                before = _fold_data(et.read_entry_trail_fold())
                size_before = journal.stat().st_size
                et.compact_entry_trail_journal()
                after = _fold_data(et.read_entry_trail_fold())
            self.assertEqual(before, after)
            self.assertLess(journal.stat().st_size, size_before)

    def test_read_entry_trail_fold_missing_file_is_empty(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "entry_trails.jsonl"
            with mock.patch.object(et, "_entry_trail_journal_path", lambda: journal):
                fold = et.read_entry_trail_fold()
        self.assertEqual(fold.tiers, {})
        self.assertEqual(fold.malformed, 0)

    def test_read_entry_trail_fold_unreadable_fails_closed_not_raise(self) -> None:
        # A DIRECTORY at the journal path: .exists() is True, .open() raises
        # IsADirectoryError. _place_pick only catches BrokerError, so an
        # escaping OSError would abort the whole tick BEFORE the protection
        # pass — contain it as a fail-closed malformed=1 fold instead.
        with TemporaryDirectory() as d:
            journal = Path(d) / "entry_trails.jsonl"
            journal.mkdir()
            with mock.patch.object(et, "_entry_trail_journal_path", lambda: journal):
                fold = et.read_entry_trail_fold()
        self.assertEqual(fold.tiers, {})
        self.assertEqual(fold.malformed, 1)


class TestAppendEntryTrailLine(unittest.TestCase):
    """The PR-T1 writer: append-only, round-trips through the fold, and shares
    the ONE journal-path seam the read/fold/compaction primitives use."""

    def test_append_then_fold_round_trips(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "entry_trails.jsonl"
            with mock.patch.object(et, "_entry_trail_journal_path", lambda: journal):
                et.append_entry_trail_line(
                    {"kind": et.KIND_WATCH_OPEN, "crid": _CRID, "limit": 10.0, "qty": 5}
                )
                et.append_entry_trail_line({"kind": et.KIND_TOUCHED, "crid": _CRID})
                fold = et.read_entry_trail_fold()
        self.assertIn(_CRID, fold.tiers)
        self.assertEqual(fold.tiers[_CRID].latest_kind, et.KIND_TOUCHED)
        self.assertIsNotNone(fold.tiers[_CRID].watch_open)

    def test_appends_never_rewrite_prior_lines(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "entry_trails.jsonl"
            with mock.patch.object(et, "_entry_trail_journal_path", lambda: journal):
                et.append_entry_trail_line({"kind": et.KIND_WATCH_OPEN, "crid": _CRID})
                et.append_entry_trail_line({"kind": et.KIND_EXPIRED, "crid": _CRID})
            lines = [line for line in journal.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(lines), 2)

    def test_non_json_native_payload_serializes_via_default_str(self) -> None:
        import datetime as dt

        with TemporaryDirectory() as d:
            journal = Path(d) / "entry_trails.jsonl"
            with mock.patch.object(et, "_entry_trail_journal_path", lambda: journal):
                # A stray datetime must serialize (default=str), never crash the append.
                et.append_entry_trail_line(
                    {"kind": et.KIND_TROUGH, "crid": _CRID, "at": dt.datetime(2026, 8, 12)}
                )
            self.assertIn("2026-08-12", journal.read_text(encoding="utf-8"))


class TestTerminalRecord(unittest.TestCase):
    """#1376: the fold keeps the LATEST terminal record verbatim (the ``fired``
    line carries ``avg_price`` / ``realized_qty``, ``cancelled`` its note) —
    before this the fold reduced every terminal line to its kind marker."""

    def test_terminal_record_is_none_while_non_terminal(self) -> None:
        fold = et.fold_entry_trail_lines([_watch_open(), _line(et.KIND_TOUCHED)])
        self.assertIsNone(fold.tiers[_CRID].terminal_record)

    def test_fired_record_is_kept_verbatim(self) -> None:
        fold = et.fold_entry_trail_lines(
            [
                _watch_open(),
                _line(et.KIND_FIRED, order_id="O-9", realized_qty=42, avg_price=42.5),
            ]
        )
        state = fold.tiers[_CRID]
        self.assertEqual(state.terminal_kind, et.KIND_FIRED)
        self.assertEqual(state.terminal_record["avg_price"], 42.5)
        self.assertEqual(state.terminal_record["realized_qty"], 42)
        self.assertEqual(state.terminal_record["order_id"], "O-9")

    def test_latest_terminal_record_wins(self) -> None:
        fold = et.fold_entry_trail_lines(
            [
                _watch_open(),
                _line(et.KIND_SUSPENDED, trough=9.0),
                _line(et.KIND_FIRED, realized_qty=42),
            ]
        )
        state = fold.tiers[_CRID]
        self.assertEqual(state.terminal_kind, et.KIND_FIRED)
        self.assertEqual(state.terminal_record["realized_qty"], 42)

    def test_terminal_record_survives_compaction(self) -> None:
        # Independent of _fold_data: a compactor that keeps the terminal
        # marker but drops the record's payload must fail HERE too.
        lines = [
            _watch_open(),
            _line(et.KIND_TOUCHED),
            _line(et.KIND_TROUGH, trough=9.4),
            _line(et.KIND_FIRED, order_id="O-9", realized_qty=42, avg_price=42.5),
        ]
        compacted = et.compact_entry_trail_lines(lines)
        for label, source in (("original", lines), ("compacted", compacted)):
            with self.subTest(source=label):
                record = et.fold_entry_trail_lines(source).tiers[_CRID].terminal_record
                self.assertEqual(record["realized_qty"], 42)
                self.assertEqual(record["avg_price"], 42.5)


class TestTierReservationAcct(unittest.TestCase):
    """The per-record valuation behind ``watching_virtual_gross_acct`` (#1376):
    ONE function values a watch_open for the gate and for the ``watches`` row,
    so the CLI's reservation column cannot drift from the money gate."""

    def test_limit_times_qty_same_currency(self) -> None:
        self.assertEqual(et.tier_reservation_acct({"limit": 10.0, "qty": 5}), 50.0)

    def test_fx_rate_divides_into_account_currency(self) -> None:
        self.assertEqual(et.tier_reservation_acct({"limit": 20.0, "qty": 2, "fx_rate": 4.0}), 10.0)

    def test_unvaluable_records_are_none(self) -> None:
        for record in (
            None,
            {},
            {"limit": 10.0},
            {"limit": 0.0, "qty": 5},
            {"limit": -1.0, "qty": 5},
            {"limit": True, "qty": 5},
            {"limit": "abc", "qty": 5},
            {"limit": 10.0, "qty": 5, "fx_rate": 0.0},
            {"limit": 10.0, "qty": 5, "fx_rate": "nan"},
        ):
            with self.subTest(record=record):
                self.assertIsNone(et.tier_reservation_acct(record))

    def test_gate_total_equals_the_sum_of_valued_tiers(self) -> None:
        fold = et.fold_entry_trail_lines(_rich_entry_trail_journal())
        total, _bad = et.watching_virtual_gross_acct(fold)
        valued = [
            et.tier_reservation_acct(s.watch_open)
            for s in fold.tiers.values()
            if s.terminal_kind is None
        ]
        self.assertEqual(total, sum(v for v in valued if v is not None))


def _rows_by_crid(lines: list[str], *, include_terminal: bool = False) -> dict[str, dict]:
    rows = et.tier_rows(et.fold_entry_trail_lines(lines), include_terminal=include_terminal)
    return {row["crid"]: row for row in rows}


class TestTierRows(unittest.TestCase):
    """``tier_rows`` (#1376): one row per tier, the ``stage`` mirroring the
    daemon's OWN sets (``_active_entry_watches`` / ``_resting_armed_tiers``):
    a resting native arm is ``latest_kind == trail_armed`` WITH an order id; the
    null-id write-ahead is ``arming``; a later non-terminal line after an arm
    puts the tier back under the watch pass, whatever id the fold still holds."""

    def _watch(self, crid: str, **extra: Any) -> str:
        return _watch_open(
            crid,
            pick_key="KO:2026-08-12",
            ticker="KO",
            tier_index=0,
            uic=211,
            exchange_mic="XNYS",
            instrument_currency="USD",
            **extra,
        )

    def test_stage_open_touched_trail_armed_arming(self) -> None:
        rows = _rows_by_crid(
            [
                self._watch("KO-2026-08-12-entry-t0"),
                self._watch("KO-2026-08-12-entry-t1"),
                _line(et.KIND_TROUGH, "KO-2026-08-12-entry-t1", trough=8.8),
                self._watch("KO-2026-08-12-entry-t2"),
                _line(
                    et.KIND_TRAIL_ARMED,
                    "KO-2026-08-12-entry-t2",
                    order_id="O-1",
                    trigger=9.2,
                    ceiling=9.3,
                ),
                self._watch("KO-2026-08-12-entry-t3"),
                _line(et.KIND_TRAIL_ARMED, "KO-2026-08-12-entry-t3", order_id=None, trigger=8.5),
            ]
        )
        stages = {crid: row["stage"] for crid, row in rows.items()}
        self.assertEqual(
            stages,
            {
                "KO-2026-08-12-entry-t0": "open",
                "KO-2026-08-12-entry-t1": "touched",
                "KO-2026-08-12-entry-t2": "trail_armed",
                "KO-2026-08-12-entry-t3": "arming",
            },
        )
        # The compacted real-journal shape: NO `touched` line survives, the
        # `trough` after the touch is what marks a touched tier.
        touched = rows["KO-2026-08-12-entry-t1"]
        self.assertEqual(touched["latest_kind"], et.KIND_TROUGH)
        self.assertEqual(touched["min_trough"], 8.8)
        armed = rows["KO-2026-08-12-entry-t2"]
        self.assertEqual(
            (armed["armed_order_id"], armed["armed_trigger"], armed["armed_ceiling"]),
            ("O-1", 9.2, 9.3),
        )
        arming = rows["KO-2026-08-12-entry-t3"]
        self.assertIsNone(arming["armed_order_id"])
        self.assertEqual(arming["armed_trigger"], 8.5)

    def test_a_trough_after_the_arm_is_touched_and_keeps_the_stale_id(self) -> None:
        # The daemon's watch pass drives this tier (it excludes only
        # latest_kind == trail_armed WITH an id) and `disarm` refuses on the
        # id defensively — the row must show BOTH facts, not pick one.
        rows = _rows_by_crid(
            [
                self._watch("KO-2026-08-12-entry-t0"),
                _line(et.KIND_TRAIL_ARMED, "KO-2026-08-12-entry-t0", order_id="O-1", trigger=9.2),
                _line(et.KIND_TROUGH, "KO-2026-08-12-entry-t0", trough=9.0),
            ]
        )
        row = rows["KO-2026-08-12-entry-t0"]
        self.assertEqual(row["stage"], "touched")
        self.assertEqual(row["armed_order_id"], "O-1")

    def test_every_terminal_kind_is_its_own_stage_and_hidden_by_default(self) -> None:
        for kind in sorted(et.ENTRY_TRAIL_TERMINAL_KINDS):
            with self.subTest(kind=kind):
                lines = [
                    self._watch("KO-2026-08-12-entry-t0"),
                    _line(kind, "KO-2026-08-12-entry-t0"),
                ]
                self.assertEqual(_rows_by_crid(lines), {})
                row = _rows_by_crid(lines, include_terminal=True)["KO-2026-08-12-entry-t0"]
                self.assertEqual(row["stage"], kind)
                self.assertEqual(row["terminal_kind"], kind)

    def test_fired_row_carries_avg_price_and_realized_qty(self) -> None:
        rows = _rows_by_crid(
            [
                self._watch("KO-2026-08-12-entry-t0"),
                _line(
                    et.KIND_FIRED,
                    "KO-2026-08-12-entry-t0",
                    order_id="O-1",
                    realized_qty=3,
                    avg_price=42.5,
                ),
                self._watch("KO-2026-08-12-entry-t1"),
                _line(et.KIND_CANCELLED, "KO-2026-08-12-entry-t1", note="operator disarm"),
            ],
            include_terminal=True,
        )
        fired = rows["KO-2026-08-12-entry-t0"]
        self.assertEqual((fired["fired_avg_price"], fired["fired_realized_qty"]), (42.5, 3))
        self.assertIsNone(fired["terminal_note"])
        cancelled = rows["KO-2026-08-12-entry-t1"]
        self.assertEqual(cancelled["terminal_note"], "operator disarm")
        self.assertIsNone(cancelled["fired_avg_price"])

    def test_corrupt_fill_facts_fold_to_none_never_raw(self) -> None:
        # A non-numeric avg_price / realized_qty on a fired line is journal
        # corruption; the row must carry None (rendered as absent), never the
        # raw value a numeric formatter would choke on.
        row = _rows_by_crid(
            [
                self._watch("KO-2026-08-12-entry-t0"),
                _line(
                    et.KIND_FIRED,
                    "KO-2026-08-12-entry-t0",
                    avg_price="oops",
                    realized_qty=[1],
                ),
            ],
            include_terminal=True,
        )["KO-2026-08-12-entry-t0"]
        self.assertEqual(row["stage"], et.KIND_FIRED)
        self.assertIsNone(row["fired_avg_price"])
        self.assertIsNone(row["fired_realized_qty"])

    def test_row_carries_the_watch_open_facts_and_the_reservation(self) -> None:
        row = _rows_by_crid(
            [self._watch("KO-2026-08-12-entry-t0", fx_rate=4.0, limit=20.0, qty=2)]
        )["KO-2026-08-12-entry-t0"]
        self.assertEqual(row["tier"], "E1")
        self.assertEqual(row["pick_key"], "KO:2026-08-12")
        self.assertEqual(row["ticker"], "KO")
        self.assertEqual(row["tier_index"], 0)
        self.assertEqual(row["uic"], 211)
        self.assertEqual(row["exchange_mic"], "XNYS")
        self.assertEqual(row["instrument_currency"], "USD")
        self.assertEqual((row["limit"], row["qty"], row["fx_rate"]), (20.0, 2, 4.0))
        self.assertEqual(row["reservation_acct"], 10.0)
        self.assertEqual(row["window_end"], "2026-08-21")

    def test_generation_crid_labels_the_tier_from_its_index(self) -> None:
        row = _rows_by_crid([_watch_open("ENPH-2026-09-08-g2-entry-t1")])[
            "ENPH-2026-09-08-g2-entry-t1"
        ]
        self.assertEqual(row["tier"], "E2")

    def test_tier_without_watch_open_is_unvaluable_not_absent(self) -> None:
        row = _rows_by_crid([_line(et.KIND_TOUCHED, "KO-2026-08-12-entry-t0")])[
            "KO-2026-08-12-entry-t0"
        ]
        self.assertEqual(row["stage"], "touched")
        self.assertIsNone(row["reservation_acct"])
        self.assertIsNone(row["pick_key"])
        self.assertIsNone(row["limit"])

    def test_rows_sort_by_pick_then_tier_index(self) -> None:
        rows = et.tier_rows(
            et.fold_entry_trail_lines(
                [
                    _watch_open("ZZ-2026-08-12-entry-t1", pick_key="ZZ:2026-08-12", tier_index=1),
                    _watch_open("AA-2026-08-12-entry-t0", pick_key="AA:2026-08-12", tier_index=0),
                    _watch_open("ZZ-2026-08-12-entry-t0", pick_key="ZZ:2026-08-12", tier_index=0),
                ]
            ),
            include_terminal=False,
        )
        self.assertEqual(
            [row["crid"] for row in rows],
            ["AA-2026-08-12-entry-t0", "ZZ-2026-08-12-entry-t0", "ZZ-2026-08-12-entry-t1"],
        )

    def test_reservation_sum_equals_the_gate_total(self) -> None:
        fold = et.fold_entry_trail_lines(_rich_entry_trail_journal())
        rows = et.tier_rows(fold, include_terminal=False)
        total, _bad = et.watching_virtual_gross_acct(fold)
        self.assertEqual(
            sum(r["reservation_acct"] for r in rows if r["reservation_acct"] is not None), total
        )


if __name__ == "__main__":
    unittest.main()
