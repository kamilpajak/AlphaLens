"""#1236 PR-1: a trailed stop level does not outlive the pick that earned it.

``trailed`` markers are uic-keyed and journal-lifetime, while the level they
record belongs to one position. The separation was a generation reset in
``_fold_trailed_since_latest_plan``: a ``tranche_plan`` line with a new
``pick_key`` cleared the uic's accumulator.

That reset cannot fire for a pick armed ``--no-tp``, which journals no
``tranche_plan`` at all. Measured on the real fold before this change: such a
pick INHERITED the level trailed by an earlier position on the same uic. The CLI
help calls ``--no-tp`` a "trail-only pick", so this is the shape a trailing pick
uses, not a corner case. The consequence is not a bad stop
(``_build_managed_exits`` skips a uic with no tranche plan) but a silently dark
trail: ``_maybe_trail``'s ratchet floor sits at another position's level.

The fix is to let ``planned`` lines drive the same generation reset, since every
pick journals them. The identity itself (``pick_key``) is what ``tranche_plan``
lines have carried all along; this only puts it on the line that is always
written.

Order-safety is worth stating because it is easy to get wrong, and I did get it
wrong first: the reset reads write order, and the compactor rewrites the journal
with ``planned`` lines first and ``trailed`` markers last. That would disarm the
reset — except the ELECTION (``_elect_trailed_lines``) runs on the journal in
write order, before the rewrite, so a marker it drops is simply absent from the
compacted file. The fold and the election share one selection precisely so that
argument cannot rot.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.control_loop import (
    _build_planned_line,
    _compact_standalone_stop_journal_lines,
    _elect_trailed_lines,
    _fold_trailed_since_latest_plan,
)

_UIC = 7777
_LEVEL = 61.5
_A = "KO:2026-09-01"
_B = "KO:2026-09-08"


def _planned(crid: str, *, pick_key: str | None = None, tier: int = 0):
    return _build_planned_line(
        entry_crid=crid,
        uic=_UIC,
        side="SELL",
        stop_price=55.0,
        take_profit=None,
        tier_index=tier,
        pick_key=pick_key,
    )


def _tranche_plan(pick_key: str, *, ts: float):
    return {
        "kind": "tranche_plan",
        "uic": _UIC,
        "ts": ts,
        "pick_key": pick_key,
        "tp_tranches": [],
        "reference_qty": 10.0,
        "stop_price": 55.0,
    }


def _trailed(*, ts: float, level: float = _LEVEL):
    return {"kind": "trailed", "uic": _UIC, "ts": ts, "level": level}


class ThePlannedLineCarriesTheTradeIdentityTest(unittest.TestCase):
    """``tranche_plan`` has carried ``pick_key`` since the entry-trail work. The
    ``planned`` line did not, which is why the only available reset depended on a
    line a ``--no-tp`` pick never writes."""

    def test_a_pick_key_is_stamped_when_given(self):
        self.assertEqual(_planned("crid-1", pick_key=_A)["pick_key"], _A)

    def test_the_key_is_omitted_entirely_when_absent(self):
        # Byte-identical to every line written before this change.
        self.assertNotIn("pick_key", _planned("crid-1"))


class ALevelDoesNotOutliveThePickThatEarnedItTest(unittest.TestCase):
    def test_a_no_tp_pick_does_not_inherit_the_previous_picks_level(self):
        """THE FIX. Pick A trails; A closes; pick B arms `--no-tp` on the same
        uic, so it journals a `planned` line and NO tranche plan. Before this
        change B inherited A's level."""
        lines = [
            _planned("KO-2026-09-01-entry-t0", pick_key=_A),
            _tranche_plan(_A, ts=100.0),
            _trailed(ts=110.0),
            _planned("KO-2026-09-08-entry-t0", pick_key=_B),
        ]
        self.assertEqual(_fold_trailed_since_latest_plan(lines), {})

    def test_positive_control_the_level_survives_while_its_own_pick_governs(self):
        """Without this the test above would pass even if the fold dropped every
        marker unconditionally."""
        lines = [
            _planned("KO-2026-09-01-entry-t0", pick_key=_A),
            _tranche_plan(_A, ts=100.0),
            _trailed(ts=110.0),
        ]
        self.assertEqual(_fold_trailed_since_latest_plan(lines), {_UIC: _LEVEL})

    def test_the_tiers_of_one_pick_do_not_reset_each_other(self):
        """A multi-tier pick writes one `planned` line per tier. They share a
        `pick_key`, so a later tier firing must not drop a level the position has
        already trailed to."""
        lines = [
            _planned("KO-2026-09-01-entry-t0", pick_key=_A, tier=0),
            _trailed(ts=110.0),
            _planned("KO-2026-09-01-entry-t1", pick_key=_A, tier=1),
        ]
        self.assertEqual(_fold_trailed_since_latest_plan(lines), {_UIC: _LEVEL})

    def test_a_retracted_plan_drops_the_level(self):
        lines = [
            _planned("KO-2026-09-01-entry-t0", pick_key=_A),
            _trailed(ts=110.0),
            {
                "kind": "planned_retracted",
                "client_request_id": "KO-2026-09-01-entry-t0",
                "uic": _UIC,
                "note": "watch ended with no fill",
            },
        ]
        self.assertEqual(_fold_trailed_since_latest_plan(lines), {})


class TheElectionRunsBeforeTheCompactorReordersTest(unittest.TestCase):
    """The reset reads write order, and the compactor reorders. The election is
    what makes that safe, so it is asserted rather than assumed."""

    def _superseded(self):
        return [
            _planned("KO-2026-09-01-entry-t0", pick_key=_A),
            _tranche_plan(_A, ts=100.0),
            _trailed(ts=110.0),
            _planned("KO-2026-09-08-entry-t0", pick_key=_B),
        ]

    def test_the_election_drops_a_superseded_marker(self):
        self.assertEqual(_elect_trailed_lines(self._superseded()), [])

    def test_so_the_compacted_file_cannot_resurrect_it(self):
        compacted = _compact_standalone_stop_journal_lines(self._superseded())
        self.assertEqual([line for line in compacted if line.get("kind") == "trailed"], [])
        self.assertEqual(_fold_trailed_since_latest_plan(compacted), {})

    def test_positive_control_a_live_level_survives_compaction(self):
        """Guards the check above against passing because compaction drops every
        marker. This is the #1324 property and it must still hold."""
        lines = [
            _planned("KO-2026-09-01-entry-t0", pick_key=_A),
            _tranche_plan(_A, ts=100.0),
            _trailed(ts=110.0),
        ]
        compacted = _compact_standalone_stop_journal_lines(lines)
        self.assertEqual(_fold_trailed_since_latest_plan(compacted), {_UIC: _LEVEL})

    def test_the_election_and_the_fold_cannot_disagree(self):
        """They share one selection since #1236. Asserted directly so a future
        split shows up here rather than as a stop placed at the wrong level."""
        for lines in (self._superseded(), [_planned("c", pick_key=_A), _trailed(ts=1.0)]):
            with self.subTest(lines=len(lines)):
                elected = {line["uic"] for line in _elect_trailed_lines(lines)}
                self.assertEqual(elected, set(_fold_trailed_since_latest_plan(lines)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
