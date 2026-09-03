"""Unit tests for ``brokers/automanager/manual_intent.py`` — the pure builder
behind `alphalens broker arm-manual` (#1235).

The builder compiles operator vocabularies (tier/TP mini-DSL, R-multiples,
account-currency notional) into the one normal form the wire contract already
speaks: absolute prices, ``alloc_pct``/``tranche_pct`` percentages and a
``suggested_size_pct``. Every refusal is a :class:`ManualIntentError` with an
operator-readable message — this command arms real money, so a typo must
explode, never be silently normalized.
"""

from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.brokers.automanager.manual_intent import (
    ManualIntentError,
    build_manual_intent,
    parse_entry_tiers,
    parse_tp_tranches,
    planned_blended_entry_of,
    resolve_size_pct,
)

_BLEND = 100.0
_STOP = 90.0
_ARM_DATE = dt.date(2026, 9, 2)
_ARMED_TS = "2026-09-02T12:00:00+00:00"


def _build(**overrides):
    kwargs = {
        "ticker": "nvo",
        "mic": "XNYS",
        "tiers_raw": ["72.5:60", "70.0:40"],
        "stop": 66.0,
        "tps_raw": ["80:50", "2R:50"],
        "no_tp": False,
        "size_pct": None,
        "notional": 10000.0,
        "frame": 15000.0,
        "ttl_days": None,
        "arm_date": _ARM_DATE,
        "armed_ts": _ARMED_TS,
    }
    kwargs.update(overrides)
    return build_manual_intent(**kwargs)


class ParseEntryTiersTest(unittest.TestCase):
    def test_single_bare_tier_implies_full_allocation(self) -> None:
        tiers = parse_entry_tiers(["72.5"])
        self.assertEqual(len(tiers), 1)
        self.assertEqual(tiers[0].limit_price, 72.5)
        self.assertEqual(tiers[0].alloc_pct, 100.0)
        self.assertEqual(tiers[0].tag, "T1")

    def test_multi_tier_keeps_order_and_tags(self) -> None:
        tiers = parse_entry_tiers(["72.5:60", "70:40"])
        self.assertEqual([t.limit_price for t in tiers], [72.5, 70.0])
        self.assertEqual([t.alloc_pct for t in tiers], [60.0, 40.0])
        self.assertEqual([t.tag for t in tiers], ["T1", "T2"])

    def test_float_alloc_sum_within_tolerance_passes(self) -> None:
        tiers = parse_entry_tiers(["100:33.3", "99:33.3", "98:33.4"])
        self.assertEqual(len(tiers), 3)

    def test_all_bare_tiers_split_equally(self) -> None:
        # The WhatsApp signal format: "t1:GME@17.90 t2:GME@17.00 t3:GME@16.20"
        # carries prices only. All-bare ladders split the allocation equally;
        # the compiled-intent echo surfaces the split for verification.
        tiers = parse_entry_tiers(["17.90", "17.00", "16.20"])
        self.assertEqual([t.limit_price for t in tiers], [17.9, 17.0, 16.2])
        for tier in tiers:
            self.assertAlmostEqual(tier.alloc_pct, 100.0 / 3)

    def test_no_tiers_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "at least one --tier"):
            parse_entry_tiers([])

    def test_mixed_bare_and_allocated_tiers_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "either every --tier"):
            parse_entry_tiers(["72.5:60", "70"])

    def test_alloc_sum_off_100_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "sum to 100"):
            parse_entry_tiers(["72.5:60", "70:30"])

    def test_single_tier_with_partial_alloc_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "sum to 100"):
            parse_entry_tiers(["72.5:60"])

    def test_non_positive_price_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "price must be positive"):
            parse_entry_tiers(["0:100"])

    def test_non_positive_alloc_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "alloc_pct must be positive"):
            parse_entry_tiers(["72.5:100", "70:0"])

    def test_garbage_tier_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "cannot parse --tier"):
            parse_entry_tiers(["seventy:100"])

    def test_too_many_colons_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "cannot parse --tier"):
            parse_entry_tiers(["72.5:60:1"])

    def test_duplicate_tier_price_refuses(self) -> None:
        # The same price twice is almost certainly a pasted-twice typo: the
        # deeper rung of such a ladder can never fill separately.
        with self.assertRaisesRegex(ManualIntentError, "duplicate --tier price"):
            parse_entry_tiers(["72.5:60", "72.5:40"])

    def test_now_tier_with_alloc_parses(self) -> None:
        tiers = parse_entry_tiers(["now@43.00:40", "41:60"])
        self.assertEqual(tiers[0].limit_price, 43.0)
        self.assertEqual(tiers[0].alloc_pct, 40.0)
        self.assertEqual(tiers[0].entry_mode, "immediate")
        self.assertEqual(tiers[0].tag, "T1")
        self.assertEqual(tiers[1].entry_mode, "pullback")

    def test_bare_now_tier_joins_equal_split(self) -> None:
        tiers = parse_entry_tiers(["now@43.00", "41", "40"])
        self.assertEqual(tiers[0].entry_mode, "immediate")
        for tier in tiers:
            self.assertAlmostEqual(tier.alloc_pct, 100.0 / 3)

    def test_second_now_tier_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "at most one now"):
            parse_entry_tiers(["now@43:50", "now@42:50"])

    def test_non_first_now_tier_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "listed first"):
            parse_entry_tiers(["43:60", "now@42:40"])

    def test_now_without_cap_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "needs a cap price"):
            parse_entry_tiers(["now@:100"])
        with self.assertRaisesRegex(ManualIntentError, "needs a cap price"):
            parse_entry_tiers(["now@"])

    def test_now_cap_non_positive_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "must be positive"):
            parse_entry_tiers(["now@0:100"])

    def test_now_cap_equal_to_pullback_price_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "duplicate --tier price"):
            parse_entry_tiers(["now@43:50", "43:50"])

    def test_garbage_after_now_prefix_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "cannot parse"):
            parse_entry_tiers(["now@abc:100"])


class ParseTpTranchesTest(unittest.TestCase):
    def test_price_form_computes_r_multiple(self) -> None:
        tranches = parse_tp_tranches(["110:50"], blend=_BLEND, stop=_STOP)
        self.assertEqual(tranches[0].price, 110.0)
        self.assertEqual(tranches[0].tranche_pct, 50.0)
        self.assertAlmostEqual(tranches[0].r_multiple, 1.0)
        self.assertEqual(tranches[0].tag, "TP1")

    def test_r_form_computes_price(self) -> None:
        tranches = parse_tp_tranches(["2R:50"], blend=_BLEND, stop=_STOP)
        self.assertAlmostEqual(tranches[0].price, 120.0)
        self.assertEqual(tranches[0].r_multiple, 2.0)

    def test_lowercase_r_form_accepted(self) -> None:
        tranches = parse_tp_tranches(["1.5r:25"], blend=_BLEND, stop=_STOP)
        self.assertAlmostEqual(tranches[0].price, 115.0)

    def test_mixed_forms_keep_order_and_tags(self) -> None:
        tranches = parse_tp_tranches(["110:50", "3R:50"], blend=_BLEND, stop=_STOP)
        self.assertEqual([t.tag for t in tranches], ["TP1", "TP2"])
        self.assertAlmostEqual(tranches[1].price, 130.0)

    def test_price_at_or_below_blend_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "above the planned blend"):
            parse_tp_tranches(["100:50"], blend=_BLEND, stop=_STOP)

    def test_non_positive_r_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "R-multiple must be positive"):
            parse_tp_tranches(["0R:50"], blend=_BLEND, stop=_STOP)

    def test_missing_tranche_pct_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "cannot parse --tp"):
            parse_tp_tranches(["110"], blend=_BLEND, stop=_STOP)

    def test_tranche_pct_sum_over_100_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "exceed 100"):
            parse_tp_tranches(["110:60", "120:50"], blend=_BLEND, stop=_STOP)

    def test_non_positive_tranche_pct_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "tranche_pct must be positive"):
            parse_tp_tranches(["110:0"], blend=_BLEND, stop=_STOP)

    def test_garbage_tp_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "cannot parse --tp"):
            parse_tp_tranches(["2X:50"], blend=_BLEND, stop=_STOP)

    def test_duplicate_tp_price_refuses(self) -> None:
        # Two tranches at one target are one bigger tranche at best and a
        # pasted-twice typo at worst — refuse either way (fail-loud doctrine).
        with self.assertRaisesRegex(ManualIntentError, "duplicate --tp price"):
            parse_tp_tranches(["110:50", "1R:50"], blend=_BLEND, stop=_STOP)


class ResolveSizePctTest(unittest.TestCase):
    def test_size_pct_passes_through(self) -> None:
        self.assertEqual(resolve_size_pct(size_pct=67.0, notional=None, frame=None), 67.0)

    def test_notional_divides_by_frame(self) -> None:
        self.assertAlmostEqual(
            resolve_size_pct(size_pct=None, notional=10000.0, frame=15000.0), 100.0 * 10000 / 15000
        )

    def test_both_modes_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "exactly one of"):
            resolve_size_pct(size_pct=67.0, notional=10000.0, frame=15000.0)

    def test_neither_mode_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "exactly one of"):
            resolve_size_pct(size_pct=None, notional=None, frame=None)

    def test_notional_without_frame_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "needs the declared frame"):
            resolve_size_pct(size_pct=None, notional=10000.0, frame=None)

    def test_size_pct_over_100_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "0 < size_pct <= 100"):
            resolve_size_pct(size_pct=101.0, notional=None, frame=None)

    def test_notional_over_frame_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "0 < size_pct <= 100"):
            resolve_size_pct(size_pct=None, notional=16000.0, frame=15000.0)

    def test_non_positive_size_pct_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "0 < size_pct <= 100"):
            resolve_size_pct(size_pct=0.0, notional=None, frame=None)

    def test_non_positive_notional_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "notional must be positive"):
            resolve_size_pct(size_pct=None, notional=-5.0, frame=15000.0)

    def test_non_positive_frame_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "frame must be positive"):
            resolve_size_pct(size_pct=None, notional=10000.0, frame=0.0)


class BuildManualIntentTest(unittest.TestCase):
    def test_full_intent_assembly(self) -> None:
        intent = _build()

        self.assertEqual(intent.intent_id, "NVO:2026-09-02:manual")
        self.assertEqual(intent.instrument.ticker, "NVO")
        self.assertEqual(intent.instrument.mic, "XNYS")
        self.assertEqual(intent.meta.source, "manual")
        self.assertEqual(intent.meta.trade_date, "2026-09-02")
        self.assertEqual(intent.meta.armed_ts, _ARMED_TS)
        self.assertIsNone(intent.exit)
        self.assertEqual(intent.spec.disaster_stop, 66.0)
        self.assertEqual(len(intent.spec.entry_tiers), 2)
        self.assertEqual(len(intent.spec.tp_tranches), 2)
        self.assertAlmostEqual(intent.spec.suggested_size_pct, 100.0 * 10000 / 15000)
        self.assertEqual(intent.spec.side, "long")

    def test_r_form_tp_uses_manual_blend_and_stop(self) -> None:
        # blend = 72.5*0.6 + 70*0.4 = 71.5; R = 71.5 - 66 = 5.5; 2R => 82.5
        intent = _build()
        self.assertAlmostEqual(intent.spec.tp_tranches[1].price, 82.5)

    def test_no_tp_yields_empty_tranches(self) -> None:
        intent = _build(tps_raw=[], no_tp=True)
        self.assertEqual(intent.spec.tp_tranches, ())

    def test_no_tp_with_tps_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "--no-tp together with --tp"):
            _build(no_tp=True)

    def test_missing_tp_decision_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "either --tp or --no-tp"):
            _build(tps_raw=[])

    def test_stop_at_or_above_lowest_tier_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "below every entry tier"):
            _build(stop=70.0)

    def test_now_only_cap_must_exceed_stop(self) -> None:
        # A now-only pick's cap IS its lowest tier — the existing invariant
        # covers the memo's "cap must exceed --stop" rule for free.
        with self.assertRaisesRegex(ManualIntentError, "below every entry tier"):
            _build(tiers_raw=["now@43.00:100"], stop=43.0, tps_raw=["2R:100"])

    def test_planned_blend_includes_now_cap(self) -> None:
        # Memo D2: the immediate cap participates in the alloc-weighted planned
        # blend as that allocation's worst-case planned entry.
        intent = _build(tiers_raw=["now@43.00:40", "41:60"], stop=39.0, tps_raw=["2R:100"])
        blend = planned_blended_entry_of(
            intent.spec.entry_tiers, disaster_stop=intent.spec.disaster_stop
        )
        self.assertAlmostEqual(blend, 0.4 * 43.0 + 0.6 * 41.0)

    def test_non_positive_stop_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "stop must be positive"):
            _build(stop=0.0)

    def test_xwar_is_accepted(self) -> None:
        # #1238 PR 7: GPW opens after the venue arc (routing, day-1 gate,
        # cost gates, gross cap, stream window) and the SIM first-fill
        # experiment.
        intent = _build(mic="XWAR")
        self.assertEqual(intent.instrument.mic, "XWAR")

    def test_xetr_is_accepted(self) -> None:
        # #1271 PR 4: Xetra opens after the arc's fee card (MIC-keyed,
        # 0.08% min EUR 3), venue map + RHM alias, and the tracked stream
        # venue window landed.
        intent = _build(mic="XETR")
        self.assertEqual(intent.instrument.mic, "XETR")

    def test_unsupported_mic_refuses_naming_the_supported_set(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "XAMS"):
            _build(mic="XAMS")

    def test_ttl_default_is_contract_default(self) -> None:
        from broker_contract.constants import DEFAULT_ORDER_TTL_DAYS

        intent = _build()
        self.assertEqual(intent.spec.order_ttl_days, DEFAULT_ORDER_TTL_DAYS)

    def test_ttl_override_carries_through(self) -> None:
        intent = _build(ttl_days=3)
        self.assertEqual(intent.spec.order_ttl_days, 3)

    def test_non_positive_ttl_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "ttl_days must be positive"):
            _build(ttl_days=0)

    def test_blank_ticker_refuses(self) -> None:
        with self.assertRaisesRegex(ManualIntentError, "ticker must be non-empty"):
            _build(ticker="  ")


if __name__ == "__main__":
    unittest.main()
