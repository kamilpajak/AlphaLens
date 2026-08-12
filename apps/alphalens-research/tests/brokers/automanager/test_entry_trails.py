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

    def test_latest_watch_open_record_wins(self) -> None:
        fold = et.fold_entry_trail_lines([_watch_open(qty=100), _watch_open(qty=40)])
        watch = fold.tiers[_CRID].watch_open
        self.assertIsNotNone(watch)
        assert watch is not None
        self.assertEqual(watch["qty"], 40)

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


def _rich_entry_trail_journal() -> list[str]:
    """Every whitelisted kind, two crids, redundant intermediates the
    compaction must fold away, plus an unknown kind and a malformed line."""
    return [
        _watch_open("crid-a", limit=10.0, qty=100),
        _line(et.KIND_TOUCHED, "crid-a"),
        _line(et.KIND_TROUGH, "crid-a", trough=9.5),
        _line(et.KIND_TROUGH, "crid-a", trough=9.2),
        _line(et.KIND_TROUGH, "crid-a", trough=9.3),
        _line(et.KIND_TRAIL_ARMED, "crid-a", order_id="O-1", trigger=9.25),
        _watch_open("crid-b", limit=5.0, qty=40),
        _line(et.KIND_TOUCHED, "crid-b"),
        _line(et.KIND_EXPIRED, "crid-b"),
        _line("future_kind", "crid-a", payload="opaque"),
        "{not json",
    ]


def _fold_data(fold: et.EntryTrailFold) -> tuple[Any, int]:
    return (
        {
            crid: (s.crid, s.watch_open, s.latest_kind, s.min_trough, s.terminal_kind)
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

    def test_relative_order_is_preserved(self) -> None:
        # The fold's latest-kind semantics are FILE-ORDER based; compaction
        # must emit kept lines in their original relative order.
        original = _rich_entry_trail_journal()
        compacted = et.compact_entry_trail_lines(original)
        positions = [original.index(line) for line in compacted]
        self.assertEqual(positions, sorted(positions))


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


if __name__ == "__main__":
    unittest.main()
