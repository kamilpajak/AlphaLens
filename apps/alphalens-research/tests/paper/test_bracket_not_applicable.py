"""Decision guard: the Alpaca single-parent BRACKET order does NOT fix the
naked-protection window, and must not be wired into the harness.

Background: filled paper ENTRY orders sit with no protective stop until the
next 30-min reconcile poll attaches the OCO ladder (exit_manager.py:1037). A
tempting "cheap fix" is to route entries through
``AlpacaClient.submit_bracket_order`` so Alpaca attaches TP+SL atomically when
the parent fills. It was investigated and REJECTED — see
``docs/research/alpaca_trade_updates_ws_daemon_design_2026_06_03.md`` §8. Three
independent kill reasons; this file pins the two that are structural (and so
checkable without live data):

  * §8.3 — TP/SL targets are defined relative to the qty-weighted blended entry
    across ALL tiers, which a per-tier bracket (priced off one tier, before any
    fill) cannot express.
  * §8.3 — the ``BrokerClient`` protocol deliberately keeps OCO/bracket
    mechanics out (broker-agnostic boundary, broker.py:134) so the Saxo
    multi-market endgame stays venue-neutral.

The empirical kill reason (§8.1: 0/37 live plans are single-tier+single-TP) and
the partial-fill mechanic (§8.2) live in the memo, not here.

Re-open conditions (memo §8.5): the bracket question only re-surfaces if BOTH
(a) plan geometry starts producing single-tier+single-TP plans AND (b) entries
move away from partial-fillable limit-GTC orders. A geometry change alone does
not re-open it — so these guards intentionally pin invariants, not data.
"""

import unittest

from alphalens_pipeline.paper.broker import BrokerClient
from alphalens_pipeline.thematic.trade_setup import ladder


class TestTpTargetsAreBlendedEntryRelative(unittest.TestCase):
    """A per-tier bracket cannot express blended-entry-relative TP targets."""

    def test_r_multiple_is_relative_to_blended_entry_not_a_single_price(self):
        # blended_entry (qty-weighted across tiers) is distinct from close so a
        # blended-relative R and a single-price-relative R give different numbers.
        close, atr, stop, blended_entry = 100.0, 5.0, 90.0, 96.0
        resistance = 112.0
        tranches = ladder.build_tp_tranches(close, atr, [resistance], blended_entry, stop)

        self.assertEqual(len(tranches), 1)
        target, r_multiple, _ = tranches[0]
        self.assertEqual(target, resistance)

        blended_relative = (resistance - blended_entry) / (blended_entry - stop)
        self.assertAlmostEqual(r_multiple, blended_relative, places=9)

        # The structural point: R is NOT computed off any single price (e.g. the
        # close, which a submit-time per-tier bracket would have to use). If it
        # were, this would hold instead — and it must not.
        close_relative = (resistance - close) / (close - stop)
        self.assertGreater(abs(r_multiple - close_relative), 0.1)


class TestBrokerProtocolExcludesBracket(unittest.TestCase):
    """The broker-neutral protocol keeps Alpaca BRACKET mechanics out."""

    def test_protocol_does_not_expose_submit_bracket_order(self):
        members = {
            name
            for name in vars(BrokerClient)
            if not name.startswith("_") and callable(getattr(BrokerClient, name))
        }
        # Positive controls: the real broker-neutral surface IS present, so this
        # guard cannot silently rot to trivially-true if introspection breaks.
        self.assertIn("submit_limit_order", members)
        self.assertIn("attach_exit_ladder", members)
        # The boundary itself: bracket mechanics never leak into the protocol.
        self.assertNotIn("submit_bracket_order", members)


if __name__ == "__main__":
    unittest.main()
