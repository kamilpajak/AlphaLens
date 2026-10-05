"""The net realized cash of a closed pick, in the account currency (#1689).

`outcome.pnl_cash_acct` is the account-currency cash P&L GROSS of commission
and exchange fee. The venue states the net per execution on a field the tree
reads the row of and did not map: `BookedAmountAccountCurrency`. These tests
pin three things.

**The net is a read, not a derivation.** Every expected number below is
computed here from the raw fixture rows, never from the builder, so a mutation
inside the builder cannot move the expectation with it.

**The FX conversion charge is already inside it.** `ConversionRateAccountCurrency`
is a disclosure of markup already contained in the rate the venue booked the
leg at, not a separate cash movement: the cash book of the captured window
holds no FX row at all. Adding `fees.fx_conversion` into a net therefore
double counts at about 0.25 % of notional per leg, and
:meth:`TheFxChargeIsNotPartOfTheNet.test_adding_fx_conversion_would_double_count`
is the test that fails if someone adds it.

**A disagreement is reported, never absorbed.** The vendor's net and the
derived sum agree on all 13 closed records of the capture. When they do not,
the record carries `net_disagrees_with_fees` rather than a quietly wrong
number.
"""

from __future__ import annotations

import copy
from typing import Any

from alphalens_pipeline.brokers.automanager import trades

from tests.brokers.automanager.test_broker_trades import VST, _codes, _TradesCase
from tests.brokers.automanager.trades_fixture import (
    VENUE,
    FakeFillHistory,
    install_journals,
    trade,
    value,
    venue_without,
)

# The vendor's own numbers, keyed by TradeId, read straight off the capture.
_BOOKED = {str(row["TradeId"]): row["BookedAmountAccountCurrency"] for row in VENUE["trades"]}
_ACCT_BY_TYPE: dict[str, dict[str, float]] = {}
for _row in VENUE["bookings"]:
    _ACCT_BY_TYPE.setdefault(str(_row["RelatedTradeId"]), {})[_row["BkAmountType"]] = _row[
        "AmountAccountCurrency"
    ]

VST_NET_ACCT = 84.25
VST_COMMISSION_ACCT = -7.71
VST_EXCHANGE_FEE_ACCT = -0.08
# Every closed record of the capture, so the identity is checked on 13 shapes
# and not only on the golden one. UBER is the prorated case.
CLOSED_RECORDS = 13


def _legs(record: dict[str, Any]) -> list[tuple[list[str], float]]:
    """(trade ids, attributed fraction) for every leg that carries money."""
    legs: list[tuple[list[str], float]] = [
        ([e["trade_id"] for e in tier["fill"]["executions"]], 1.0)
        for tier in record["entries"]
        if tier.get("fill")
    ]
    for exit_ in record["exits"]:
        total = exit_["qty"]["value"]
        attributed = value(exit_["attributed_qty"])
        legs.append(
            (
                [e["trade_id"] for e in exit_["executions"]],
                attributed / total if total else 0.0,
            )
        )
    return legs


def _vendor_net(record: dict[str, Any]) -> float:
    """What the venue says the record netted, from the trades report alone."""
    return sum(
        fraction * sum(_BOOKED[t] for t in trade_ids) for trade_ids, fraction in _legs(record)
    )


def _acct_fee(record: dict[str, Any], bk_amount_type: str) -> float:
    return sum(
        fraction * sum(_ACCT_BY_TYPE[t].get(bk_amount_type, 0.0) for t in trade_ids)
        for trade_ids, fraction in _legs(record)
    )


class TheVendorStatesTheNetPerExecution(_TradesCase):
    """The assumption the whole field rests on, pinned on the capture itself."""

    def test_booked_amount_equals_share_plus_commission_plus_exchange_fee(self) -> None:
        # 41 of 41 rows, to the cent. If a later capture breaks this, the field
        # is not a net on that account and this test is the one that says so.
        for row in VENUE["trades"]:
            trade_id = str(row["TradeId"])
            legs = _ACCT_BY_TYPE[trade_id]
            with self.subTest(trade=trade_id):
                self.assertAlmostEqual(
                    row["BookedAmountAccountCurrency"],
                    legs.get("Share Amount", 0.0)
                    + legs.get("Commission", 0.0)
                    + legs.get("Exchange Fee", 0.0),
                    places=2,
                )

    def test_it_is_never_the_share_amount_alone(self) -> None:
        # The discriminating half: a field that merely echoed the cash leg
        # would satisfy the test above on a zero-fee account.
        for row in VENUE["trades"]:
            trade_id = str(row["TradeId"])
            with self.subTest(trade=trade_id):
                self.assertNotAlmostEqual(
                    row["BookedAmountAccountCurrency"],
                    _ACCT_BY_TYPE[trade_id].get("Share Amount", 0.0),
                    places=2,
                )

    def test_the_cash_book_holds_no_fx_row(self) -> None:
        # Why the FX charge must be inside the account amount: were the 0.25 %
        # charged on top of a mid-market rate, it would be a cash movement, and
        # the capture would carry a row for it.
        kinds = {row["BkAmountType"] for row in VENUE["bookings"]}
        self.assertEqual(
            kinds,
            {
                "Share Amount",
                "Commission",
                "Exchange Fee",
                "Cash Amount",
                "Corporate Actions - Cash Dividends",
                "Corporate Actions - Withholding Tax",
            },
        )


class GoldenVstNet(_TradesCase):
    """VST:2026-09-21, the golden pick, in the account currency."""

    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home)
        self.record = trade(self.build(pick=VST), VST)
        self.outcome = self.record["outcome"]

    def test_the_net_is_the_vendor_amount(self) -> None:
        self.assertAlmostEqual(value(self.outcome["net_cash_acct"]), VST_NET_ACCT, places=2)

    def test_the_net_carries_the_account_currency_and_its_source(self) -> None:
        measured = self.outcome["net_cash_acct"]
        self.assertEqual(measured["unit"], "PLN")
        self.assertEqual(measured["source"], trades.SOURCE_TRADES_REPORT)

    def test_the_fees_gain_an_account_currency_sibling(self) -> None:
        fees = self.outcome["fees"]
        self.assertAlmostEqual(value(fees["commission_acct"]), VST_COMMISSION_ACCT, places=2)
        self.assertAlmostEqual(value(fees["exchange_fee_acct"]), VST_EXCHANGE_FEE_ACCT, places=2)
        self.assertEqual(fees["commission_acct"]["unit"], "PLN")
        self.assertEqual(fees["exchange_fee_acct"]["unit"], "PLN")

    def test_the_native_fee_members_keep_the_booking_currency(self) -> None:
        # The pair exists because the two are in different currencies. If the
        # native member silently became PLN, the pair would be pointless.
        fees = self.outcome["fees"]
        self.assertEqual(fees["commission"]["unit"], "USD")
        self.assertEqual(fees["exchange_fee"]["unit"], "USD")

    def test_the_net_is_worse_than_the_gross(self) -> None:
        # Direction check: fees are costs, so a net below the gross is the only
        # correct sign. A sign flip in the accumulator passes every sum test
        # above and fails this one.
        self.assertLess(value(self.outcome["net_cash_acct"]), value(self.outcome["pnl_cash_acct"]))

    def test_the_record_carries_no_disagreement_warning(self) -> None:
        self.assertNotIn(trades.W_NET_DISAGREES_WITH_FEES, _codes(self.record))


class TheNetAgreesOnEveryClosedRecord(_TradesCase):
    """The identity on all 13 closed shapes of the capture, prorated included."""

    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home)
        self.closed = [r for r in self.build().trades if r["state"] == "closed"]

    def test_the_capture_still_holds_every_closed_shape(self) -> None:
        self.assertEqual(len(self.closed), CLOSED_RECORDS)

    def test_the_net_equals_the_vendor_sum_over_the_legs(self) -> None:
        for record in self.closed:
            with self.subTest(pick=record["pick_key"]):
                self.assertAlmostEqual(
                    value(record["outcome"]["net_cash_acct"]),
                    _vendor_net(record),
                    places=2,
                )

    def test_the_net_equals_the_gross_plus_the_account_currency_fees(self) -> None:
        for record in self.closed:
            outcome = record["outcome"]
            with self.subTest(pick=record["pick_key"]):
                self.assertAlmostEqual(
                    value(outcome["net_cash_acct"]),
                    value(outcome["pnl_cash_acct"])
                    + value(outcome["fees"]["commission_acct"])
                    + value(outcome["fees"]["exchange_fee_acct"]),
                    places=2,
                )

    def test_the_account_currency_fees_are_the_vendor_rows(self) -> None:
        for record in self.closed:
            fees = record["outcome"]["fees"]
            with self.subTest(pick=record["pick_key"]):
                self.assertAlmostEqual(
                    value(fees["commission_acct"]), _acct_fee(record, "Commission"), places=2
                )
                self.assertAlmostEqual(
                    value(fees["exchange_fee_acct"]), _acct_fee(record, "Exchange Fee"), places=2
                )

    def test_a_whole_leg_record_reads_the_trades_report(self) -> None:
        whole = [r for r in self.closed if all(f == 1.0 for _, f in _legs(r))]
        self.assertTrue(whole, "the capture no longer carries an unprorated close")
        for record in whole:
            with self.subTest(pick=record["pick_key"]):
                self.assertEqual(
                    record["outcome"]["net_cash_acct"]["source"], trades.SOURCE_TRADES_REPORT
                )

    def test_a_prorated_record_says_derived_instead(self) -> None:
        # The repo's rule: an allocation rule makes the output derived. This is
        # what realized_fx.conversion_rate already does when it has to weight.
        for record in self.closed:
            if all(f == 1.0 for _, f in _legs(record)):
                continue
            with self.subTest(pick=record["pick_key"]):
                self.assertEqual(
                    record["outcome"]["net_cash_acct"]["source"], trades.SOURCE_DERIVED
                )

    def test_the_prorated_record_is_among_them(self) -> None:
        # UBER closes half the position inside the window, so its fee members
        # are fractional. Without it the identity would only be checked on
        # whole legs, where proration cannot go wrong.
        prorated = [
            r
            for r in self.closed
            if any(fraction < 1.0 for _, fraction in _legs(r) if fraction is not None)
        ]
        self.assertTrue(prorated, "the capture no longer carries a prorated close")


class TheFxChargeIsNotPartOfTheNet(_TradesCase):
    """The one arithmetic mistake this field makes easy, pinned shut."""

    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home)
        self.outcome = trade(self.build(pick=VST), VST)["outcome"]

    def test_the_fx_charge_is_reported_and_is_not_zero(self) -> None:
        # Without this, the test below would pass on a record where fx is 0.0
        # and prove nothing.
        self.assertLess(value(self.outcome["fees"]["fx_conversion"]), -1.0)

    def test_adding_fx_conversion_would_double_count(self) -> None:
        fees = self.outcome["fees"]
        with_fx = (
            value(self.outcome["pnl_cash_acct"])
            + value(fees["commission_acct"])
            + value(fees["exchange_fee_acct"])
            + value(fees["fx_conversion"])
        )
        self.assertNotAlmostEqual(value(self.outcome["net_cash_acct"]), with_fx, places=2)


class TheNetIsNullRatherThanPartial(_TradesCase):
    """A money field says why it is absent; it never reports a partial sum."""

    def test_an_open_pick_reports_not_closed(self) -> None:
        install_journals(self.home)
        record = trade(self.build(pick="NESR:2026-09-30"), "NESR:2026-09-30")
        measured = record["outcome"]["net_cash_acct"]
        self.assertIsNone(measured["value"])
        self.assertEqual(measured["null_reason"], trades.NULL_NOT_CLOSED)

    def test_sim_reports_make_it_null_naming_sim_and_not_not_closed(self) -> None:
        # The record is unresolved on SIM, so the reason names the cause the
        # operator can act on rather than the state it produced.
        install_journals(self.home)
        report = self.build(FakeFillHistory(sim=True), pick=VST)
        measured = trade(report, VST)["outcome"]["net_cash_acct"]
        self.assertIsNone(measured["value"])
        self.assertEqual(measured["null_reason"], trades.NULL_SIM_REPORTS)

    def test_a_missing_booked_amount_nulls_the_whole_net(self) -> None:
        # All or none: one execution whose row carries no net must not leave a
        # sum over the others, which would read as the record's net.
        venue = copy.deepcopy(VENUE)
        for row in venue["trades"]:
            if str(row["TradeId"]) == "6885451891":  # the VST exit
                row["BookedAmountAccountCurrency"] = None
        install_journals(self.home)
        record = trade(self.build(FakeFillHistory(venue), pick=VST), VST)
        measured = record["outcome"]["net_cash_acct"]
        self.assertIsNone(measured["value"])
        self.assertIsNotNone(measured["null_reason"])

    def test_a_pick_with_no_report_row_reports_the_report_reason(self) -> None:
        venue = venue_without(trades={"6885451891"})
        install_journals(self.home)
        record = trade(self.build(FakeFillHistory(venue), pick=VST), VST)
        self.assertIsNone(record["outcome"]["net_cash_acct"]["value"])


class ADisagreementIsReported(_TradesCase):
    """The vendor's net and the derived sum must agree, or the record says so."""

    def _with_booked(self, trade_id: str, amount: float) -> dict[str, Any]:
        venue = copy.deepcopy(VENUE)
        for row in venue["trades"]:
            if str(row["TradeId"]) == trade_id:
                row["BookedAmountAccountCurrency"] = amount
        return venue

    def test_a_vendor_net_that_disagrees_raises_the_warning(self) -> None:
        install_journals(self.home)
        tampered = self._with_booked("6885451891", _BOOKED["6885451891"] + 5.0)
        record = trade(self.build(FakeFillHistory(tampered), pick=VST), VST)
        self.assertIn(trades.W_NET_DISAGREES_WITH_FEES, _codes(record))

    def test_the_disagreeing_net_is_still_the_vendor_number(self) -> None:
        # The warning says the two disagree. It does not license replacing the
        # venue's figure with ours.
        install_journals(self.home)
        tampered = self._with_booked("6885451891", _BOOKED["6885451891"] + 5.0)
        record = trade(self.build(FakeFillHistory(tampered), pick=VST), VST)
        self.assertAlmostEqual(
            value(record["outcome"]["net_cash_acct"]), VST_NET_ACCT + 5.0, places=2
        )

    def test_a_difference_inside_the_tolerance_is_silent(self) -> None:
        # Both sides are vendor cents, so only float error should ever differ.
        # A tolerance that caught rounding would warn on every record.
        install_journals(self.home)
        nudged = self._with_booked(
            "6885451891", _BOOKED["6885451891"] + trades.NET_FEE_TOLERANCE_ACCT / 2
        )
        record = trade(self.build(FakeFillHistory(nudged), pick=VST), VST)
        self.assertNotIn(trades.W_NET_DISAGREES_WITH_FEES, _codes(record))

    def test_the_tolerance_is_smaller_than_one_cent_of_either_fee(self) -> None:
        # A tolerance wide enough to swallow a whole fee would make the check
        # unable to refute anything.
        self.assertLess(trades.NET_FEE_TOLERANCE_ACCT, abs(VST_EXCHANGE_FEE_ACCT))


class TheVocabularyIsPublished(_TradesCase):
    """A new warning code is in the builder's tuple, so the README gate sees it."""

    def test_the_warning_is_in_the_published_tuple(self) -> None:
        self.assertIn(trades.W_NET_DISAGREES_WITH_FEES, trades.WARNING_CODES)

    def test_the_fee_vocabulary_no_longer_claims_fx_is_excluded(self) -> None:
        # `fees_not_included` is the record's own statement of what it cannot
        # account for. FX is accounted for, inside the account amount.
        self.assertNotIn("fx", " ".join(trades.FEES_NOT_INCLUDED))
