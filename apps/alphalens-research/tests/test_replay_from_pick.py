"""Turning a LIVE pick's own record into intent-replay's two inputs.

`scripts/replay_from_pick.py` exists because assembling a run by hand is where
the errors were. Over two days of doing it manually, three of the seven values
came out wrong: the FX rate taken from a public reference instead of the rate
the daemon recorded, `exit_edge_min_bps` taken as 5.0 from the design memo's
worked example instead of the production constant 50.0, and the calendar asked
for XNYS on three picks that trade on XNAS. Every one of those values is in the
pick's own production record, so the mapping is the thing to pin.

The replay itself stays clean: it learns nothing about `~/.alphalens`. This
module is the laboratory's join, which is why it lives under research.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import pathlib
import unittest
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import state_paths
from alphalens_pipeline.brokers.automanager.trades import TradesFilters, build_trades
from broker_contract.trade_intent.codec import supplied_derived_paths
from intent_replay.bars import check_window_covers, parse_bars, validate_sequence
from intent_replay.config import RunConfig
from scripts.replay_from_pick import (
    BarRefusal,
    ExitTimeUnknownError,
    _served_short_message,
    authored_document,
    bar_cache_root,
    bar_window,
    build_bars,
    deadline_coverage,
    entry_trail_bps_by_pick,
    exit_instant,
    filter_to_grid,
    main,
    modal_spacing_ms,
    replay_inputs,
    run_configuration,
    session_grid,
    stated_facts,
)

from tests.brokers.automanager.trades_fixture import NOW, FakeFillHistory, install_journals

_JOURNAL = (
    pathlib.Path(__file__).parent
    / "brokers/automanager/fixtures/trades/live_2026_10_03/entry_trails.jsonl"
)


def _epoch_ms(moment: dt.datetime) -> int:
    """The epoch the memo's tables and the replay's own configuration quote."""
    return int(moment.timestamp() * 1000)


def _measured_node(value: Any, null_reason: str | None = None) -> dict[str, Any]:
    """One `Measured` node, the shape `broker trades` writes: a value with its
    unit, source, row reference and null reason."""
    return {
        "value": value,
        "unit": None,
        "source": "venue.audit",
        "ref": "order:5448023092",
        "null_reason": null_reason,
    }


def _aggs(
    first: dt.datetime, count: int, *, price: float = 10.0, **extra: Any
) -> list[dict[str, Any]]:
    """`count` consecutive one-minute aggregates in the RAW vendor shape.

    Polygon's own keys: `t` in epoch milliseconds at the bar's START, `o`, `h`,
    `l`, `c`, plus the `v` / `vw` / `n` the published bar shape does not model.
    """
    return [
        {
            "t": _epoch_ms(first + dt.timedelta(minutes=index)),
            "o": price,
            "h": price + 0.5,
            "l": price - 0.5,
            "c": price + 0.1,
            "v": 1000,
            "vw": price,
            "n": 10,
            **extra,
        }
        for index in range(count)
    ]


def _trades() -> dict[str, dict]:
    """The committed LIVE fixture, built once: the real journals of 2026-10-03
    plus the venue rows read that day."""
    with TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        with mock.patch.object(pathlib.Path, "home", staticmethod(lambda: home)):
            install_journals(home)
            report = build_trades(
                "live",
                broker=FakeFillHistory(),
                filters=TradesFilters(pick=None, limit=None),
                now=NOW,
            )
    return {trade["pick_key"]: trade for trade in report.body()["trades"]}


class TheAuthoredDocumentIsThePlanWithoutWhatADoorComputesTest(unittest.TestCase):
    """A journalled pick is the ARMED document: the door has already stamped
    `intent_id`, `meta.armed_ts` and each tranche's `r_multiple` onto it. The
    replay refuses exactly those (`derived_field_supplied`), so a run from a
    real pick has to take them off first.

    Which fields those are is NOT a list repeated here. It comes from
    `broker_contract.trade_intent.codec.supplied_derived_paths`, the same
    function the replay's own door asks, so this cannot drift from the refusal
    it exists to avoid."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.trades = _trades()

    def test_the_result_carries_no_derived_field(self) -> None:
        for key in ("VST:2026-09-21", "ASTS:2026-09-23", "SMMT:2026-09-23"):
            with self.subTest(pick=key):
                plan = self.trades[key]["plan"]
                self.assertTrue(
                    supplied_derived_paths(plan),
                    "the fixture's armed document should carry derived fields",
                )
                self.assertEqual(supplied_derived_paths(authored_document(plan)), [])

    def test_nothing_else_is_removed(self) -> None:
        """The existence control for the test above: stripping everything would
        also leave no derived field. Putting the stamped values back has to
        reproduce the armed document exactly."""
        plan = self.trades["VST:2026-09-21"]["plan"]
        stripped = authored_document(plan)
        restored = {**stripped, "intent_id": plan["intent_id"]}
        restored["meta"] = {**stripped["meta"], "armed_ts": plan["meta"]["armed_ts"]}
        restored["spec"] = {
            **stripped["spec"],
            "tp_tranches": [
                {**tranche, "r_multiple": original["r_multiple"]}
                for tranche, original in zip(
                    stripped["spec"]["tp_tranches"], plan["spec"]["tp_tranches"], strict=True
                )
            ],
        }
        self.assertEqual(restored, plan)

    def test_the_pick_s_own_document_is_not_mutated(self) -> None:
        plan = self.trades["VST:2026-09-21"]["plan"]
        before = supplied_derived_paths(plan)
        authored_document(plan)
        self.assertEqual(supplied_derived_paths(plan), before)


class TheRunConfigurationComesFromTheRecordNotFromMemoryTest(unittest.TestCase):
    """The three values that came out wrong when assembled by hand.

    Each is asserted against its real source rather than against a literal, so
    the test fails the way the mistake failed: a rate copied from elsewhere, a
    threshold copied from a worked example, a calendar asked for the wrong
    venue."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.trades = _trades()
        cls.vst = authored_document(cls.trades["VST:2026-09-21"]["plan"])

    def _config(self, document=None, **over):
        """The five stated facts, defaulting to VST's own recorded ones."""
        kwargs = {
            "fx_rate": 0.2639462605413538,
            "fx_rate_asof": "2026-09-30T13:36:02.123000+00:00",
            "fx_rate_source": "keeper.sizing_fx",
            "instrument_currency": "USD",
            "entry_trail_bps": 50,
        }
        kwargs.update(over)
        return run_configuration(document or self.vst, **kwargs)

    def test_the_cost_block_is_the_daemon_s_own_constants(self) -> None:
        """`exit_edge_min_bps` was taken as 5.0 from the design memo's worked
        example; production is 50.0. Pinning against the import means the
        memo's value cannot come back."""
        from alphalens_pipeline.brokers.automanager import costs

        block = self._config()["costs"]
        self.assertEqual(block["exit_edge_min_bps"]["value"], costs.EXIT_EDGE_MIN_BPS)
        self.assertEqual(block["commission_rate"]["value"], costs.US_FEE_CARD.commission_rate)
        self.assertEqual(block["min_commission"]["value"], costs.US_FEE_CARD.min_commission)
        self.assertNotEqual(block["exit_edge_min_bps"]["value"], 5.0)

    def test_the_fx_cost_and_buffer_are_the_daemon_s_own_constants(self) -> None:
        from alphalens_pipeline.brokers import execution
        from alphalens_pipeline.brokers.automanager import costs

        fx = self._config()["fx"]
        self.assertEqual(fx["round_trip_cost_rate"]["value"], costs.FX_ROUND_TRIP_RATE)
        self.assertEqual(fx["sizing_buffer_pct"]["value"], execution._FX_SIZING_BUFFER_PCT)

    def test_the_calendar_is_asked_for_the_document_s_own_venue(self) -> None:
        """Three of the four comparable picks trade on XNAS and the calendar was
        asked for XNYS. That passed unnoticed because the two share their
        session times, so the discriminator has to be a venue that does not:
        XWAR opens 07:00 UTC where XNYS opens 13:30."""
        import datetime as dt

        from alphalens_pipeline.market.calendar import session_open_utc

        warsaw = json.loads(json.dumps(self.vst))
        warsaw["instrument"]["mic"] = "XWAR"
        trade_date = dt.date.fromisoformat(warsaw["meta"]["trade_date"])
        self.assertEqual(
            self._config(warsaw)["walk_start"]["value"],
            int(session_open_utc(trade_date, exchange="XWAR").timestamp() * 1000),
        )
        self.assertNotEqual(
            self._config(warsaw)["walk_start"]["value"],
            self._config()["walk_start"]["value"],
        )

    def test_the_day_1_anchor_follows_the_document_s_own_provenance(self) -> None:
        """The daemon anchors day 1 on `meta.source`: a manual pick's own
        `trade_date` is day 1, a brief's is the session before day 1
        (`control_loop._day1_anchor`, step 0 against step 1). Both arms are
        here because a brief document still decodes and still arms."""
        import datetime as dt

        from alphalens_pipeline.market.calendar import advance_trading_sessions, session_open_utc

        brief = json.loads(json.dumps(self.vst))
        brief["meta"]["source"] = "brief"
        trade_date = dt.date.fromisoformat(brief["meta"]["trade_date"])
        mic = brief["instrument"]["mic"]
        for document, step in ((self.vst, 0), (brief, 1)):
            with self.subTest(source=document["meta"].get("source")):
                day1 = advance_trading_sessions(trade_date, step, exchange=mic)
                self.assertEqual(
                    self._config(document)["walk_start"]["value"],
                    int(session_open_utc(day1, exchange=mic).timestamp() * 1000),
                )
        self.assertNotEqual(
            self._config(brief)["walk_start"]["value"],
            self._config()["walk_start"]["value"],
        )

    def test_the_entry_deadline_counts_the_document_s_own_ttl_in_sessions(self) -> None:
        """The deadline is when the entry ladder stops resting, so a TTL
        counted in calendar days -- or dropped -- would end the ladder on the
        wrong bar. `order_ttl_days` is a count of SESSIONS, which is why the
        document cannot state the deadline itself."""
        import datetime as dt

        from alphalens_pipeline.market.calendar import advance_trading_sessions, session_close_utc

        trade_date = dt.date.fromisoformat(self.vst["meta"]["trade_date"])
        mic = self.vst["instrument"]["mic"]
        ttl = self.vst["spec"]["order_ttl_days"]
        self.assertGreater(ttl, 0, "the fixture must carry a real TTL for this to discriminate")
        last = advance_trading_sessions(trade_date, ttl, exchange=mic)
        block = self._config()["entry_deadline"]
        self.assertEqual(
            block["value"], int(session_close_utc(last, exchange=mic).timestamp() * 1000)
        )
        self.assertNotEqual(
            block["value"],
            int(
                session_close_utc(
                    advance_trading_sessions(trade_date, 0, exchange=mic), exchange=mic
                ).timestamp()
                * 1000
            ),
        )
        self.assertGreater(block["value"], self._config()["walk_start"]["value"])

    def test_a_stated_zero_ttl_ends_where_the_daemon_ends_it(self) -> None:
        """#1734: 0 is the document's "field absent" value. The daemon reads it
        as the default window (`pick_window`), so a replay that walked zero
        sessions would end the ladder on the trade date itself, seven sessions
        before the keeper's entries stopped resting."""
        import copy
        import datetime as dt

        from alphalens_pipeline.brokers.automanager.pick_window import pick_window

        document = copy.deepcopy(self.vst)
        document["spec"]["order_ttl_days"] = 0
        trade_date = dt.date.fromisoformat(document["meta"]["trade_date"])
        mic = document["instrument"]["mic"]
        expected = pick_window(trade_date, 0, mic).window_end
        self.assertEqual(
            self._config(document)["entry_deadline"]["value"],
            int(expected.timestamp() * 1000),
        )

    def test_both_epochs_land_on_the_venue_s_real_utc_wall_clock(self) -> None:
        """The two epoch values are asserted by READING them back as a UTC wall
        clock, not by recomputing them the way the code computes them.

        Every other epoch assertion here calls the same expression the module
        calls, so all of them would survive a timezone bug together: a naive
        datetime would shift both by this machine's offset and both sides with
        it. This one names the fact a reader can check -- XNYS opens 13:30 UTC
        and closes 20:00 UTC while New York is on EDT.
        """
        import datetime as dt

        block = self._config()
        for key, expected in (
            ("walk_start", "2026-09-21 13:30:00"),
            ("entry_deadline", "2026-09-30 20:00:00"),
        ):
            with self.subTest(key=key):
                moment = dt.datetime.fromtimestamp(block[key]["value"] / 1000, dt.UTC)
                self.assertEqual(moment.strftime("%Y-%m-%d %H:%M:%S"), expected)

    def test_the_fee_card_is_the_venue_s_own_not_the_us_one(self) -> None:
        """Existence control for the cost block: a non-US pick must not be
        priced on the US schedule, which a literal 0.0008 / 1.0 would do."""
        from alphalens_pipeline.brokers.automanager import costs

        warsaw = json.loads(json.dumps(self.vst))
        warsaw["instrument"]["mic"] = "XWAR"
        block = self._config(warsaw, instrument_currency="PLN")["costs"]
        self.assertEqual(block["commission_rate"]["value"], costs.WSE_FEE_CARD.commission_rate)
        self.assertEqual(block["min_commission"]["value"], costs.WSE_FEE_CARD.min_commission)
        self.assertEqual(block["min_commission"]["unit"], "PLN")

    def test_a_same_currency_pick_states_no_magnitude(self) -> None:
        """The engine's fx key set is conditional on the codes: one key when
        they agree, four when they differ. A rate stated on a same-currency run
        is `unknown_key`, so this is not a cosmetic difference."""
        from intent_replay.config import RunConfig

        warsaw = json.loads(json.dumps(self.vst))
        warsaw["instrument"]["mic"] = "XWAR"
        block = self._config(warsaw, instrument_currency="PLN")
        self.assertEqual(list(block["fx"]), ["instrument_currency"])
        self.assertEqual(
            self._config()["fx"]["mid_rate"]["unit"],
            "USD_per_PLN",
        )
        RunConfig.from_jsonable(block, account_currency="PLN")

    def test_the_block_is_one_the_replay_itself_admits(self) -> None:
        """The keystone: not that the block looks right to me, but that the
        engine's own parser takes it."""
        from intent_replay.config import RunConfig

        parsed = RunConfig.from_jsonable(self._config(), account_currency="PLN")
        self.assertEqual(parsed.entry_trail_bps, 50)
        self.assertEqual(parsed.to_jsonable(), self._config())

    def test_the_rate_is_the_one_given_and_the_picks_disagree(self) -> None:
        """Existence control for "the rate comes from the record": the four
        comparable picks carry four different rates, so a hard-coded one would
        be wrong on at least three."""
        rates = {
            key: trade["sizing_fx"]["rate"]["value"]
            for key, trade in self.trades.items()
            if not (trade.get("replay_exclusions") or []) and trade["state"] == "closed"
        }
        self.assertGreater(len(set(rates.values())), 1, rates)
        for rate in rates.values():
            self.assertEqual(self._config(fx_rate=rate)["fx"]["mid_rate"]["value"], rate)


class TheStatedFactsAreReadFromTheRecordNotSuppliedByHandTest(unittest.TestCase):
    """The error this half exists to stop: the FX rate was taken from a public
    reference (NBP, 0.2636366) where the daemon had sized on 0.2639463 — 0.12%
    away, with the right rate sitting in the pick's own record all along."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.trades = _trades()

    def test_every_fact_is_the_value_the_record_carries(self) -> None:
        for key in ("VST:2026-09-21", "ASTS:2026-09-23", "SMMT:2026-09-23", "EWTX:2026-09-25"):
            with self.subTest(pick=key):
                trade = self.trades[key]
                facts = stated_facts(trade)
                self.assertEqual(facts.fx_rate, trade["sizing_fx"]["rate"]["value"])
                self.assertEqual(facts.fx_rate_asof, trade["sizing_fx"]["asof"]["value"])
                self.assertEqual(facts.fx_rate_source, trade["sizing_fx"]["source"]["value"])
                self.assertEqual(
                    facts.instrument_currency,
                    trade["instrument"]["instrument_currency"]["value"],
                )

    def test_a_record_the_replay_cannot_compare_is_refused(self) -> None:
        """`replay_exclusions` is the record's own verdict on comparability. A
        pick closed by hand, or still open, replays to a number that looks like
        a divergence and is not one."""
        excluded = {
            key: trade["replay_exclusions"]
            for key, trade in self.trades.items()
            if trade["replay_exclusions"]
        }
        self.assertTrue(excluded, "the fixture must carry excluded picks for this to discriminate")
        for key, reasons in excluded.items():
            with self.subTest(pick=key):
                with self.assertRaises(ValueError) as caught:
                    replay_inputs(self.trades[key], entry_trail_bps=50)
                for reason in reasons:
                    self.assertIn(reason, str(caught.exception))

    def test_a_comparable_record_is_not_refused(self) -> None:
        """Existence control for the test above."""
        comparable = [key for key, trade in self.trades.items() if not trade["replay_exclusions"]]
        self.assertGreater(len(comparable), 1, comparable)
        for key in comparable:
            with self.subTest(pick=key):
                inputs = replay_inputs(self.trades[key], entry_trail_bps=50)
                self.assertEqual(supplied_derived_paths(inputs.document), [])

    def test_the_configuration_carries_the_record_s_own_rate(self) -> None:
        """The whole point of the join, asserted where the replay reads it."""
        from intent_replay.config import RunConfig

        trade = self.trades["VST:2026-09-21"]
        inputs = replay_inputs(trade, entry_trail_bps=50)
        parsed = RunConfig.from_jsonable(
            inputs.configuration,
            account_currency=inputs.document["spec"]["size"]["currency"],
        )
        self.assertEqual(parsed.costs.fx.rate, trade["sizing_fx"]["rate"]["value"])
        self.assertEqual(parsed.entry_trail_bps, 50)
        # The rate is the one value in the block nothing in the run can check,
        # so its source and its as-of time ARE the audit (spec 5.2.1).
        mid_rate = inputs.configuration["fx"]["mid_rate"]
        self.assertEqual(mid_rate["source"], trade["sizing_fx"]["source"]["value"])
        self.assertIn(trade["sizing_fx"]["asof"]["value"], mid_rate["formula"])

    def test_an_absent_rate_is_refused_rather_than_filled_in(self) -> None:
        """A record whose sizing rate was never journalled cannot be priced.
        The refusal names the record's own reason for the hole."""
        trade = json.loads(json.dumps(self.trades["VST:2026-09-21"]))
        trade["sizing_fx"]["rate"] = {"value": None, "null_reason": "not_journaled"}
        with self.assertRaises(ValueError) as caught:
            stated_facts(trade)
        self.assertIn("not_journaled", str(caught.exception))

    def test_a_fact_of_the_wrong_type_is_refused_here(self) -> None:
        """A rate stated as text would be written into a configuration file and
        refused only when the replay read it back, so the command would report
        success on a run that cannot start."""
        for value in ("0.2639462605413538", float("nan"), True):
            with self.subTest(rate=value):
                trade = json.loads(json.dumps(self.trades["VST:2026-09-21"]))
                trade["sizing_fx"]["rate"] = {"value": value}
                with self.assertRaises(ValueError) as caught:
                    stated_facts(trade)
                self.assertIn("sizing_fx.rate", str(caught.exception))

        # A currency code is worse than a wrong number: it decides the fx key
        # set, so a non-string would build a cross-currency block for a pick
        # that may not be one.
        for value in (840, "", ["USD"]):
            with self.subTest(currency=value):
                trade = json.loads(json.dumps(self.trades["VST:2026-09-21"]))
                trade["instrument"]["instrument_currency"] = {"value": value}
                with self.assertRaises(ValueError) as caught:
                    stated_facts(trade)
                self.assertIn("instrument.instrument_currency", str(caught.exception))

    def test_a_same_currency_record_needs_no_rate_at_all(self) -> None:
        """On this account every pick is cross-currency: the budget is PLN and
        the instruments settle in USD. A same-currency pick would journal no
        rate, and demanding one would refuse a run that needs none."""
        trade = json.loads(json.dumps(self.trades["VST:2026-09-21"]))
        trade["instrument"]["instrument_currency"]["value"] = "PLN"
        trade["sizing_fx"]["rate"] = {"value": None, "null_reason": "not_journaled"}
        trade["sizing_fx"]["asof"] = {"value": None, "null_reason": "not_journaled"}
        trade["sizing_fx"]["source"] = {"value": None, "null_reason": "not_journaled"}
        inputs = replay_inputs(trade, entry_trail_bps=50)
        self.assertEqual(list(inputs.configuration["fx"]), ["instrument_currency"])


class TheTrailDistanceComesFromTheDrainSOwnLineTest(unittest.TestCase):
    """`entry_trail_bps` is the one configuration value the trades record does
    NOT carry. It is on the entry-trail journal's `watch_open` line, frozen at
    DRAIN time (`entry_trails.KEY_DISTANCE`'s docstring), so it is read there
    rather than reconstructed by dividing an absolute distance by a price."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.lines = (
            (
                pathlib.Path(__file__).parent
                / "brokers/automanager/fixtures/trades/live_2026_10_03/entry_trails.jsonl"
            )
            .read_text()
            .splitlines()
        )

    def test_each_pick_gets_the_distance_its_own_line_journalled(self) -> None:
        journalled = {}
        for line in self.lines:
            record = json.loads(line)
            if record.get("kind") == "watch_open":
                journalled.setdefault(record["pick_key"], record["d_bps"])
        self.assertTrue(journalled)
        self.assertEqual(entry_trail_bps_by_pick(self.lines), journalled)

        # Every line of the committed journal carries the same distance, so
        # the assertion above also holds for a function that answers 50 and
        # never reads the line. One pick on a different distance is what makes
        # it a read.
        other = dict(
            json.loads(self.lines[0]), crid="synthetic-1", pick_key="XYZ:2026-09-21", d_bps=37
        )
        self.assertEqual(
            entry_trail_bps_by_pick([*self.lines, json.dumps(other)]),
            journalled | {"XYZ:2026-09-21": 37},
        )

    def test_a_corrupt_watch_open_line_is_named_not_skipped(self) -> None:
        """Skipping it reported the pick as having NO line, which sends a
        reader to look for something that is there and wrong. The daemon's own
        gross-cap consumer fails closed on a record it cannot attribute; this
        does the same."""
        rows = [json.loads(line) for line in self.lines]
        first = next(r for r in rows if r.get("kind") == "watch_open")
        for broken in (
            dict(first, crid="corrupt-1", d_bps="50"),
            dict(first, crid="corrupt-2", d_bps=None),
            {k: v for k, v in dict(first, crid="corrupt-3").items() if k != "pick_key"},
        ):
            with self.subTest(d_bps=broken.get("d_bps"), pick=broken.get("pick_key")):
                with self.assertRaises(ValueError) as caught:
                    entry_trail_bps_by_pick([*self.lines, json.dumps(broken)])
                self.assertIn(broken["crid"], str(caught.exception))

    def test_tiers_that_disagree_on_the_distance_are_refused(self) -> None:
        """One drain resolves one distance for the whole ladder, so two tiers
        of one pick cannot differ. If they do, no single stated value is the
        run's, and picking either would be a guess."""
        rows = [json.loads(line) for line in self.lines]
        first = next(r for r in rows if r.get("kind") == "watch_open")
        clash = dict(first, crid=first["crid"] + "-x", d_bps=first["d_bps"] + 10)
        with self.assertRaises(ValueError) as caught:
            entry_trail_bps_by_pick([*self.lines, json.dumps(clash)])
        self.assertIn(first["pick_key"], str(caught.exception))


class TheJournalledDistanceIsStatedNotInheritedTest(unittest.TestCase):
    """A `d_bps` of 0 is the live flag's way of saying OFF (the three-limit
    ladder). The configuration states OFF as `null` and refuses a 0, so the
    translation happens once, here, and not at each call site."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.lines = _JOURNAL.read_text().splitlines()

    def test_a_zero_distance_is_off_not_a_zero_bps_trail(self) -> None:
        off = dict(
            json.loads(self.lines[0]), crid="synthetic-off", pick_key="OFF:2026-09-21", d_bps=0
        )
        distances = entry_trail_bps_by_pick([*self.lines, json.dumps(off)])
        self.assertIsNone(distances["OFF:2026-09-21"])
        self.assertIn("OFF:2026-09-21", distances)


class TheExitInstantIsReadFromTheRecordNotGuessedTest(unittest.TestCase):
    """The window has to reach the pick's own exit, and the record is the only
    place that instant exists. Guessing it from the entry deadline truncates a
    pick that exited after its ladder expired."""

    def test_a_pick_that_never_filled_has_no_exit_instant(self) -> None:
        """Two thirds of the comparable fixture records carry no exit at all,
        so an absent exit is the common case and not an error."""
        self.assertIsNone(exit_instant({"pick_key": "BE:2026-09-29", "exits": []}))

    def test_the_instant_read_is_the_last_exit_s_not_the_first_s(self) -> None:
        """Exits arrive in ascending fill order, and the fixture's own two-exit
        record (`ALB:2026-09-08-g2`) fills 19 ms apart. Reading `exits[0]`
        anchors the window on the first partial."""
        record = {
            "pick_key": "ALB:2026-09-08-g2",
            "exits": [
                {"venue_time": _measured_node("2026-09-14T13:59:41.231Z")},
                {"venue_time": _measured_node("2026-09-14T13:59:41.250Z")},
            ],
        }

        self.assertEqual(
            exit_instant(record),
            dt.datetime(2026, 9, 14, 13, 59, 41, 250000, tzinfo=dt.UTC),
        )

    def test_a_null_venue_time_is_refused_naming_the_record_s_own_reason(self) -> None:
        """A pick that never filled and a record that does not say when it
        filled are different facts, and only the first is a window anchored on the
        deadline alone. No fixture record exercises this -- every exit of all 35
        carries an instant -- so the case is constructed.

        The refusal's own `reason` is the MODE and the record's `null_reason`
        rides beside it, so a reader learns which hole it was without the code
        growing one top-level reason per journal gap."""
        record = {
            "pick_key": "SMMT:2026-09-23",
            "exits": [{"venue_time": _measured_node(None, "ambiguous_attribution")}],
        }

        with self.assertRaises(ExitTimeUnknownError) as caught:
            exit_instant(record)

        self.assertEqual(caught.exception.reason, "not_journaled")
        self.assertEqual(caught.exception.null_reason, "ambiguous_attribution")
        self.assertIn("ambiguous_attribution", str(caught.exception))

    def test_a_venue_time_that_is_not_an_instant_is_refused_too(self) -> None:
        """A naive moment and a number are both refused, because comparing
        either against the aware entry deadline raises on the money path
        instead of refusing: the first with `TypeError`, the second inside
        `fromisoformat`."""
        for value in ("2026-09-29T13:30:58.853", 1790171458853, True):
            with self.subTest(value=value):
                record = {
                    "pick_key": "SMMT:2026-09-23",
                    "exits": [{"venue_time": _measured_node(value)}],
                }

                with self.assertRaises(ExitTimeUnknownError) as caught:
                    exit_instant(record)

                self.assertEqual(caught.exception.reason, "non_finite")
                self.assertIn(repr(value), str(caught.exception))


class TheWindowReachesTheLadderSExpiryTest(unittest.TestCase):
    """The window's right edge is a requirement on the SERIES, not a clamp.

    The session grid is half-open at the close, so no bar carries the entry
    deadline instant and the window has to reach the session AFTER the deadline
    session. A window that stops earlier publishes `no_fill` with an empty
    trace, which is also what a ladder that genuinely expired publishes -- the
    two then say the same word.
    """

    def test_the_worked_example_s_epochs_are_the_ones_the_memo_published(self) -> None:
        """`SMMT:2026-09-23` at now=2026-10-09T08:14:03Z, every epoch taken
        from the design memo's own worked table, so a reader can check the
        arithmetic against the published example rather than against this
        code."""
        window = bar_window(
            walk_start=dt.datetime(2026, 9, 23, 13, 30, tzinfo=dt.UTC),
            entry_deadline=dt.datetime(2026, 10, 2, 20, 0, tzinfo=dt.UTC),
            exit_instant=dt.datetime(2026, 9, 29, 13, 30, 58, 853000, tzinfo=dt.UTC),
            mic="XNAS",
            now=dt.datetime(2026, 10, 9, 8, 14, 3, tzinfo=dt.UTC),
        )

        self.assertEqual(_epoch_ms(window.start), 1790170200000)
        self.assertEqual(_epoch_ms(window.required_end), 1791230400000)
        self.assertEqual(_epoch_ms(window.end), 1791316800000)
        self.assertEqual(window.entry_deadline_session, dt.date(2026, 10, 2))
        self.assertEqual(window.expiry_observable_session, dt.date(2026, 10, 5))
        self.assertEqual(window.last_servable_session, dt.date(2026, 10, 8))
        self.assertEqual((window.margin_sessions_requested, window.margin_sessions_served), (2, 2))

    def test_the_margin_is_anchored_on_the_exit_when_the_exit_is_the_later(self) -> None:
        """`VST:2026-09-21` exited at 2026-10-01T13:59:21Z, AFTER its own entry
        deadline of 2026-09-30T20:00Z, where the other three comparable closed
        picks exited before theirs. Taking the deadline alone truncates VST
        while its position was still open; taking the exit alone truncates the
        other three while their ladder was still live."""
        exit_at = dt.datetime(2026, 10, 1, 13, 59, 21, 248000, tzinfo=dt.UTC)

        window = bar_window(
            walk_start=dt.datetime(2026, 9, 21, 13, 30, tzinfo=dt.UTC),
            entry_deadline=dt.datetime(2026, 9, 30, 20, 0, tzinfo=dt.UTC),
            exit_instant=exit_at,
            mic="XNYS",
            now=dt.datetime(2026, 10, 9, 8, 14, 3, tzinfo=dt.UTC),
        )

        self.assertEqual(window.anchor, exit_at)
        # Two XNYS sessions after the exit's own session 2026-10-01 (a
        # Thursday): 10-02, then 10-05. The deadline's own session would have
        # ended the window two sessions earlier, at 10-02T20:00Z.
        self.assertEqual(window.end, dt.datetime(2026, 10, 5, 20, 0, tzinfo=dt.UTC))

    def test_a_margin_the_vendor_cannot_serve_is_served_short_rather_than_refused(self) -> None:
        """The same SMMT window run three days earlier, when the vendor could
        serve through 2026-10-05 only. The requirement is met exactly, so the
        run proceeds with one of its two margin sessions -- and says so, rather
        than leaving a reader to infer it from the last bar."""
        window = bar_window(
            walk_start=dt.datetime(2026, 9, 23, 13, 30, tzinfo=dt.UTC),
            entry_deadline=dt.datetime(2026, 10, 2, 20, 0, tzinfo=dt.UTC),
            exit_instant=dt.datetime(2026, 9, 29, 13, 30, 58, 853000, tzinfo=dt.UTC),
            mic="XNAS",
            now=dt.datetime(2026, 10, 6, 8, 0, tzinfo=dt.UTC),
        )

        self.assertEqual(window.end, dt.datetime(2026, 10, 5, 20, 0, tzinfo=dt.UTC))
        self.assertEqual(window.end, window.servable_end)
        self.assertEqual((window.margin_sessions_requested, window.margin_sessions_served), (2, 1))

    def test_a_ladder_whose_expiry_needs_the_next_session_is_not_servable_yet(self) -> None:
        """`PLTR:2026-09-29` is the case the half-open grid moved from run to
        refuse: the vendor can already serve the whole of its deadline session
        2026-10-08, and the run still has to wait, because no bar carries the
        deadline instant and the expiry is first observable on 2026-10-09. Six
        of the twelve comparable records refuse under this rule where three did
        under an inclusive close."""
        window = bar_window(
            walk_start=dt.datetime(2026, 9, 29, 13, 30, tzinfo=dt.UTC),
            entry_deadline=dt.datetime(2026, 10, 8, 20, 0, tzinfo=dt.UTC),
            exit_instant=None,
            mic="XNAS",
            now=dt.datetime(2026, 10, 9, 8, 14, 3, tzinfo=dt.UTC),
        )

        self.assertEqual(window.servable_end, dt.datetime(2026, 10, 8, 20, 0, tzinfo=dt.UTC))
        self.assertEqual(window.required_end, dt.datetime(2026, 10, 9, 20, 0, tzinfo=dt.UTC))
        self.assertFalse(window.servable)

    def test_the_date_to_re_run_on_is_the_day_after_the_expiry_s_own_session(self) -> None:
        """`SYM:2026-09-30`, the memo's own example: a deadline on Friday
        2026-10-09 is first observable on Monday 2026-10-12, and the vendor
        serves a calendar day once it is past, so the earliest run is
        2026-10-13. A refusal that cannot say when to come back teaches the
        operator to re-run it blindly."""
        window = bar_window(
            walk_start=dt.datetime(2026, 9, 30, 13, 30, tzinfo=dt.UTC),
            entry_deadline=dt.datetime(2026, 10, 9, 20, 0, tzinfo=dt.UTC),
            exit_instant=None,
            mic="XNAS",
            now=dt.datetime(2026, 10, 9, 8, 14, 3, tzinfo=dt.UTC),
        )

        self.assertFalse(window.servable)
        self.assertEqual(window.expiry_observable_session, dt.date(2026, 10, 12))
        self.assertEqual(window.servable_from, dt.date(2026, 10, 13))


class TheRightEdgeIsAskedOfTheSeriesNotTheWindowEndTest(unittest.TestCase):
    """The pre-fetch cap PREDICTS the vendor's day boundary, and the design
    states plainly that the boundary's timezone is not measured. Predicting is
    a claim; reading the series is a measurement, so the same requirement is
    asked again after the fetch.

    Phrased on the series rather than on the window end, it is also the one
    line of the window rule the half-open grid did not touch -- and on the
    `--bars` path it is the guard a supplied file cannot talk its way past.
    """

    _DEADLINE = dt.datetime(2026, 10, 9, 20, 0, tzinfo=dt.UTC)

    def _bars(self, *instants: dt.datetime) -> list[dict[str, Any]]:
        """A kept series in the published bar shape, one bar per instant."""
        return [
            {"t": _epoch_ms(moment), "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}
            for moment in instants
        ]

    def test_a_bar_stamped_at_the_deadline_itself_reaches_it(self) -> None:
        """At OR after, because the engine expires the ladder on the first bar
        whose `t` is not BELOW the deadline. Under the half-open grid no kept
        bar carries that instant, so this case pins the predicate against the
        engine's own condition rather than against a series the fetch path can
        produce."""
        coverage = deadline_coverage(self._bars(self._DEADLINE), entry_deadline=self._DEADLINE)

        self.assertTrue(coverage.reaches)
        self.assertEqual(coverage.last_bar_t, _epoch_ms(self._DEADLINE))

    def test_a_series_that_stops_before_the_deadline_names_the_last_bar_it_got(self) -> None:
        """The series the vendor served through 2026-10-08 only, for a ladder
        live through 2026-10-09: refused, with the instant it did reach, so a
        reader sees how short it fell without re-reading the file."""
        last = dt.datetime(2026, 10, 8, 19, 59, tzinfo=dt.UTC)

        coverage = deadline_coverage(
            self._bars(dt.datetime(2026, 10, 8, 19, 58, tzinfo=dt.UTC), last),
            entry_deadline=self._DEADLINE,
        )

        self.assertFalse(coverage.reaches)
        self.assertEqual(coverage.last_bar_t, _epoch_ms(last))

    def test_an_empty_series_reaches_nothing_and_names_no_last_bar(self) -> None:
        """An empty kept series is refused earlier, as an empty window, but the
        check has to be total: raising here would turn a refusal into a
        traceback if the two steps are ever reordered."""
        coverage = deadline_coverage([], entry_deadline=self._DEADLINE)

        self.assertFalse(coverage.reaches)
        self.assertIsNone(coverage.last_bar_t)

    def test_the_right_edge_is_judged_by_the_greatest_t_not_the_final_element(self) -> None:
        """Ordering is the engine's own refusal (`bars_unordered`) and it runs
        AFTER this check, so a mis-ordered file must not be able to hide a
        short right edge behind its last element -- or the other way round."""
        reaching = dt.datetime(2026, 10, 12, 13, 30, tzinfo=dt.UTC)
        earlier = dt.datetime(2026, 10, 8, 19, 59, tzinfo=dt.UTC)

        coverage = deadline_coverage(self._bars(reaching, earlier), entry_deadline=self._DEADLINE)

        self.assertTrue(coverage.reaches)
        self.assertEqual(coverage.last_bar_t, _epoch_ms(reaching))

    def test_the_refusal_it_feeds_names_the_last_bar_beside_the_deadline(self) -> None:
        """A short series is only readable against the instant it had to
        reach, so both go in one sentence. The prose names no vendor, because
        the same refusal fires on a supplied `--bars` file."""
        window = bar_window(
            walk_start=dt.datetime(2026, 9, 30, 13, 30, tzinfo=dt.UTC),
            entry_deadline=self._DEADLINE,
            exit_instant=None,
            mic="XNAS",
            now=dt.datetime(2026, 10, 15, 8, 0, tzinfo=dt.UTC),
        )
        coverage = deadline_coverage(
            self._bars(dt.datetime(2026, 10, 8, 19, 59, tzinfo=dt.UTC)),
            entry_deadline=self._DEADLINE,
        )

        message = _served_short_message("SYM:2026-09-30", window, coverage)

        self.assertIn("SYM:2026-09-30", message)
        self.assertIn("2026-10-08T19:59:00.000Z", message)
        self.assertIn("2026-10-09T20:00:00.000Z", message)
        self.assertIn("2026-10-12", message)


def _bars_at(*instants: dt.datetime) -> list[dict[str, Any]]:
    """Bars in the published shape at the stated instants, prices aside."""
    return [
        {"t": _epoch_ms(moment), "open": 1.0, "high": 1.5, "low": 0.5, "close": 1.1}
        for moment in instants
    ]


class TheSessionGridIsTheVenueSOwnTest(unittest.TestCase):
    """Every bound is READ from the calendar, per session.

    Composing this repository's `open + 390 minutes` idiom instead is wrong on
    21 of 29 venues in BOTH directions: XWAR is 90 minutes short and XETR 120,
    while XSHG runs 60 minutes PAST the real close and XTAI 120. The long
    direction is the one that keeps a bar the session never traded, which is
    the incident's own shape. Every instant below is a literal the memo
    measured, so a wrong grid cannot be inherited from the code under test.
    """

    def _filter(
        self, mic: str, start: dt.datetime, end: dt.datetime, *instants: dt.datetime
    ) -> tuple[list[int], Any]:
        gridded = filter_to_grid(_bars_at(*instants), session_grid(start, end, mic=mic), mic=mic)
        return [bar["t"] for bar in gridded.kept], gridded

    def test_a_bar_stamped_at_the_session_open_is_kept(self) -> None:
        """The lower bound is inclusive: 99.56% of real pairs carry a bar
        stamped exactly at the open, and `walk_start` IS that instant, so a grid
        exclusive there would make every run refuse itself."""
        opened = dt.datetime(2026, 10, 2, 13, 30, tzinfo=dt.UTC)

        kept, _ = self._filter("XNAS", opened, opened + dt.timedelta(hours=6), opened)

        self.assertEqual(kept, [_epoch_ms(opened)])

    def test_the_grid_is_the_venue_s_own_hours_not_a_390_minute_assumption(self) -> None:
        """XWAR 2026-10-02 runs 07:00:00Z-15:00:00Z, 480 minutes. `open + 390`
        would close it at 13:30:00Z and drop the last 90 minutes of every
        session."""
        inside = dt.datetime(2026, 10, 2, 14, 30, tzinfo=dt.UTC)

        kept, gridded = self._filter(
            "XWAR",
            dt.datetime(2026, 10, 2, 7, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 10, 2, 15, 0, tzinfo=dt.UTC),
            inside,
        )

        self.assertEqual(kept, [_epoch_ms(inside)])
        self.assertEqual(gridded.off_grid, ())

    def test_a_venue_whose_real_close_precedes_open_plus_390_drops_the_extra_bar(self) -> None:
        """XSHG 2026-10-12 runs 01:30:00Z-07:00:00Z. `open + 390` would close it
        at 08:00:00Z, 60 minutes PAST the real close, so a 07:30:00Z bar would
        be kept as part of a session that had ended."""
        start = dt.datetime(2026, 10, 12, 1, 30, tzinfo=dt.UTC)
        inside = dt.datetime(2026, 10, 12, 6, 30, tzinfo=dt.UTC)
        past_the_close = dt.datetime(2026, 10, 12, 7, 30, tzinfo=dt.UTC)

        kept, gridded = self._filter(
            "XSHG", start, dt.datetime(2026, 10, 12, 8, 0, tzinfo=dt.UTC), inside, past_the_close
        )

        self.assertEqual(kept, [_epoch_ms(inside)])
        self.assertEqual([off.reason for off in gridded.off_grid], ["after_close"])
        offender = gridded.off_grid[0]
        self.assertEqual(offender.session, dt.date(2026, 10, 12))
        self.assertEqual(offender.session_open_t, _epoch_ms(start))
        self.assertEqual(
            offender.session_close_t,
            _epoch_ms(dt.datetime(2026, 10, 12, 7, 0, tzinfo=dt.UTC)),
        )

    def test_a_half_day_grid_ends_at_the_early_close(self) -> None:
        """XNAS 2026-11-27 runs 14:30:00Z-18:00:00Z, 210 minutes.
        `exchange_calendars` stores the actual per-session close, so this needs
        no special case -- and a constant full-session close would keep three
        hours of post-market prints."""
        start = dt.datetime(2026, 11, 27, 14, 30, tzinfo=dt.UTC)
        last = dt.datetime(2026, 11, 27, 17, 59, tzinfo=dt.UTC)
        at_the_close = dt.datetime(2026, 11, 27, 18, 0, tzinfo=dt.UTC)
        after = dt.datetime(2026, 11, 27, 18, 1, tzinfo=dt.UTC)

        kept, gridded = self._filter(
            "XNAS",
            start,
            dt.datetime(2026, 11, 27, 21, 0, tzinfo=dt.UTC),
            last,
            at_the_close,
            after,
        )

        self.assertEqual(kept, [_epoch_ms(last)])
        self.assertEqual([drop.t for drop in gridded.dropped_at_close], [_epoch_ms(at_the_close)])
        self.assertEqual([off.t for off in gridded.off_grid], [_epoch_ms(after)])
        self.assertTrue(gridded.sessions[0].half_day)

    def test_a_window_spanning_the_dst_change_grids_each_session_on_its_own_hours(self) -> None:
        """XNAS 2026-10-30 grids 13:30:00Z-20:00:00Z and 2026-11-02
        14:30:00Z-21:00:00Z. One open/close pair reused for the whole window
        would keep a 13:30:00Z bar on 11-02, an hour before that session
        opened, and drop its last hour."""
        before = dt.datetime(2026, 10, 30, 19, 59, tzinfo=dt.UTC)
        too_early = dt.datetime(2026, 11, 2, 13, 30, tzinfo=dt.UTC)
        after_the_change = dt.datetime(2026, 11, 2, 20, 30, tzinfo=dt.UTC)

        kept, gridded = self._filter(
            "XNAS",
            dt.datetime(2026, 10, 30, 13, 30, tzinfo=dt.UTC),
            dt.datetime(2026, 11, 2, 21, 0, tzinfo=dt.UTC),
            before,
            too_early,
            after_the_change,
        )

        self.assertEqual(kept, [_epoch_ms(before), _epoch_ms(after_the_change)])
        self.assertEqual(
            [(off.t, off.reason) for off in gridded.off_grid],
            [(_epoch_ms(too_early), "before_open")],
        )

    def test_a_bar_inside_no_session_interval_is_off_the_grid(self) -> None:
        """A Saturday bar -- the real KRP 2026-08-15 shape, two pairs in the
        production cache. Bucketing by UTC date without asking the calendar
        would place it on a session that does not exist."""
        saturday = dt.datetime(2026, 8, 15, 14, 0, tzinfo=dt.UTC)

        kept, gridded = self._filter(
            "XNAS",
            dt.datetime(2026, 8, 14, 13, 30, tzinfo=dt.UTC),
            dt.datetime(2026, 8, 17, 20, 0, tzinfo=dt.UTC),
            saturday,
        )

        self.assertEqual(kept, [])
        self.assertEqual(
            [(off.reason, off.session) for off in gridded.off_grid], [("not_a_session", None)]
        )

    def test_a_bar_in_the_first_hour_of_an_xasx_session_is_kept(self) -> None:
        """XASX's 2026-10-12 session opens 2026-10-11T23:00:00Z, and 2026-10-11
        is a Sunday the calendar answers False for. The membership test is on
        INTERVALS, so bucketing by UTC date would drop the first hour of every
        XASX session."""
        inside = dt.datetime(2026, 10, 11, 23, 30, tzinfo=dt.UTC)

        kept, gridded = self._filter(
            "XASX",
            dt.datetime(2026, 10, 11, 23, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 10, 12, 5, 0, tzinfo=dt.UTC),
            inside,
        )

        self.assertEqual(kept, [_epoch_ms(inside)])
        self.assertEqual(gridded.off_grid, ())
        self.assertEqual([bounds.session for bounds in gridded.sessions], [dt.date(2026, 10, 12)])


class _CommandTestCase(unittest.TestCase):
    """The command, run in process over the fixture report.

    ``_run`` forwards ``main``'s three keyword-only seams (``bar_fetch``,
    ``actions_lookup``, ``now``). Without that forwarding no test can reach
    them, and the production defaults are the only thing that ever would -- one
    of which calls a metered vendor.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.trades = _trades()

    def _report(self, directory: pathlib.Path) -> pathlib.Path:
        path = directory / "trades.json"
        path.write_text(json.dumps({"env": "live", "trades": list(self.trades.values())}))
        return path

    def _run(self, *args: str, **seams: Any) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(list(args), **seams)
        return status, out.getvalue(), err.getvalue()

    # The `now` every window measurement in the memo's section 6.7 table was
    # taken at. VST:2026-09-21's window is servable there, so a run of that
    # pick exercises the gates that come AFTER the window.
    NOW = dt.datetime(2026, 10, 9, 8, 14, 3, tzinfo=dt.UTC)
    # VST:2026-09-21's own instants: its day-1 session open, and the session
    # AFTER its entry deadline of 2026-09-30T20:00Z -- under the half-open grid
    # no bar carries the deadline instant itself, so reaching it needs the next
    # session.
    WALK_START = dt.datetime(2026, 9, 21, 13, 30, tzinfo=dt.UTC)
    AFTER_DEADLINE = dt.datetime(2026, 10, 1, 13, 30, tzinfo=dt.UTC)

    def _tape(self, **extra: Any) -> list[dict[str, Any]]:
        """A vendor payload that clears every gate for VST:2026-09-21: it
        covers `walk_start`, it reaches past the entry deadline, and its modal
        `t`-spacing is one minute."""
        return _aggs(self.WALK_START, 3, **extra) + _aggs(
            self.AFTER_DEADLINE, 3, price=11.0, **extra
        )

    def _seams(self, payload: list[dict[str, Any]] | None = None, **over: Any) -> dict[str, Any]:
        """The seams a run that reaches the price vendor needs: a fake fetch
        and a stated clock. Without them the production default would call
        Polygon, which the no-live-network guard refuses."""
        series = self._tape() if payload is None else payload
        seams: dict[str, Any] = {
            "bar_fetch": lambda ticker, start, end: series,
            "now": lambda: self.NOW,
        }
        seams.update(over)
        return seams


class TheCommandWritesBothInputsAndSaysHowToRunThemTest(_CommandTestCase):
    """The entry point exists because hand-assembling the run is the error this
    module is about: a driver written per pick is where the wrong rate, the
    wrong threshold and the wrong calendar each came from."""

    def test_it_writes_the_two_files_the_replay_takes(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            status, out, err = self._run(
                str(self._report(directory)),
                "VST:2026-09-21",
                "--out",
                str(directory),
                "--entry-trails",
                str(_JOURNAL),
                "--bar-cache",
                str(directory / "cache"),
                "--format",
                "json",
                **self._seams(),
            )
            self.assertEqual(status, 0, err)
            answer = json.loads(out)
            document = json.loads(pathlib.Path(answer["document_path"]).read_text())
            config = json.loads(pathlib.Path(answer["config_path"]).read_text())
            self.assertEqual(supplied_derived_paths(document), [])
            RunConfig.from_jsonable(config, account_currency=document["spec"]["size"]["currency"])
            self.assertEqual(config["entry_trail_bps"], 50)
            self.assertEqual(
                config["fx"]["mid_rate"]["value"],
                self.trades["VST:2026-09-21"]["sizing_fx"]["rate"]["value"],
            )

    def test_the_next_command_it_prints_is_runnable(self) -> None:
        """An argv array, not a sentence: the convention is that the caller can
        run what the tool suggests without parsing prose."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            status, out, err = self._run(
                str(self._report(directory)),
                "VST:2026-09-21",
                "--out",
                str(directory),
                "--entry-trails",
                str(_JOURNAL),
                "--bar-cache",
                str(directory / "cache"),
                "--format",
                "json",
                **self._seams(),
            )
            self.assertEqual(status, 0, err)
            argv = json.loads(out)["replay_argv"]
            self.assertIsInstance(argv, list)
            self.assertEqual(argv[1:4], ["-m", "intent_replay", "run"])
            self.assertIn(json.loads(out)["document_path"], argv)
            self.assertIn(json.loads(out)["config_path"], argv)

    def test_a_report_that_is_not_one_exits_2_without_a_traceback(self) -> None:
        """Usage, not a crash: a tool owes a refusal where its input is wrong.
        A hand-trimmed report is the realistic case -- rows survive, the key
        the join needs does not."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            for body in ({"env": "live"}, {"env": "live", "trades": [{"state": "closed"}]}):
                with self.subTest(body=body):
                    path = directory / "broken.json"
                    path.write_text(json.dumps(body))
                    status, out, err = self._run(
                        str(path), "VST:2026-09-21", "--out", str(directory)
                    )
                    self.assertEqual(status, 2)
                    self.assertEqual(out, "")
                    self.assertIn("replay_from_pick:", err)

    def test_a_negative_stated_distance_is_a_usage_error(self) -> None:
        """A distance below 1 is refused by the configuration parser as well,
        but by then the command has already written both files and reported
        success."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            status, out, err = self._run(
                str(self._report(directory)),
                "VST:2026-09-21",
                "--out",
                str(directory),
                "--entry-trail-bps",
                "-5",
            )
            self.assertEqual(status, 2)
            self.assertEqual(out, "")
            self.assertIn("--entry-trail-bps", err)

    def test_a_pick_the_report_does_not_hold_exits_4(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            status, out, err = self._run(
                str(self._report(directory)),
                "NOPE:2026-09-21",
                "--out",
                str(directory),
                "--entry-trails",
                str(_JOURNAL),
            )
            self.assertEqual(status, 4)
            self.assertEqual(out, "")
            self.assertIn("NOPE:2026-09-21", err)

    def test_a_pick_the_replay_cannot_compare_exits_1_naming_the_reasons(self) -> None:
        excluded = next(key for key, trade in self.trades.items() if trade["replay_exclusions"])
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            status, out, err = self._run(
                str(self._report(directory)),
                excluded,
                "--out",
                str(directory),
                "--entry-trails",
                str(_JOURNAL),
            )
            self.assertEqual(status, 1)
            self.assertEqual(out, "")
            for reason in self.trades[excluded]["replay_exclusions"]:
                self.assertIn(reason, err)

            empty = directory / "no-trails.jsonl"
            empty.write_text("")
            status, _, err = self._run(
                str(self._report(directory)),
                excluded,
                "--out",
                str(directory),
                "--entry-trails",
                str(empty),
            )
            self.assertEqual(status, 1)
            self.assertIn(self.trades[excluded]["replay_exclusions"][0], err)
            self.assertNotIn("--entry-trail-bps", err)

    def test_an_unknown_distance_is_refused_rather_than_run_with_the_trail_off(self) -> None:
        """A missing journal line is not evidence that entry trailing was off,
        and a run with the wrong entry policy answers a different question."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            empty = directory / "empty.jsonl"
            empty.write_text("")
            status, out, err = self._run(
                str(self._report(directory)),
                "VST:2026-09-21",
                "--out",
                str(directory),
                "--entry-trails",
                str(empty),
            )
            self.assertEqual(status, 1)
            self.assertEqual(out, "")
            self.assertIn("--entry-trail-bps", err)

            status, out, err = self._run(
                str(self._report(directory)),
                "VST:2026-09-21",
                "--out",
                str(directory),
                "--entry-trails",
                str(empty),
                "--entry-trail-bps",
                "0",
                "--bar-cache",
                str(directory / "cache"),
                "--format",
                "json",
                **self._seams(),
            )
            self.assertEqual(status, 0, err)
            self.assertIsNone(json.loads(out)["configuration"]["entry_trail_bps"])


class EveryRefusalCarriesOneEnvelopeTest(_CommandTestCase):
    """Four refusal sites printed four differently shaped sentences and nothing
    at all on stdout in JSON mode, so a caller had only prose to branch on and
    a `--format json` caller had nothing to read."""

    def _refusals(self, directory: pathlib.Path) -> tuple[tuple[str, int, tuple[str, ...]], ...]:
        """One invocation per published code, with the status it owes.

        Every code whose verdict is a fact about the pick. `bars_window_unservable`
        is deliberately not here: its verdict moves with the calendar, so it
        needs a stated clock and lives in a class that can inject one --
        putting it here would pass today and refuse nothing in a fortnight.
        """
        report = str(self._report(directory))
        null_exit = directory / "null-exit.json"
        doctored = json.loads(json.dumps(self.trades["SMMT:2026-09-23"]))
        doctored["exits"] = [{"venue_time": _measured_node(None, "not_journaled")}]
        null_exit.write_text(json.dumps({"env": "live", "trades": [doctored]}))
        broken = directory / "broken.json"
        broken.write_text(json.dumps({"env": "live"}))
        empty = directory / "no-trails.jsonl"
        empty.write_text("")
        excluded = next(key for key, trade in self.trades.items() if trade["replay_exclusions"])
        journal = str(_JOURNAL)
        return (
            ("usage", 2, (report, "VST:2026-09-21", "--entry-trail-bps", "-5")),
            ("report_unreadable", 2, (str(broken), "VST:2026-09-21")),
            ("pick_not_in_report", 4, (report, "NOPE:2026-09-21", "--entry-trails", journal)),
            ("not_comparable", 1, (report, excluded, "--entry-trails", journal)),
            ("entry_trail_unknown", 1, (report, "VST:2026-09-21", "--entry-trails", str(empty))),
            (
                "exit_time_unknown",
                1,
                (
                    str(null_exit),
                    "SMMT:2026-09-23",
                    "--entry-trails",
                    journal,
                    "--entry-trail-bps",
                    "50",
                ),
            ),
        )

    def test_every_refusal_prefixes_its_code_to_the_human_message(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            for code, status, argv in self._refusals(directory):
                with self.subTest(code=code):
                    got, out, err = self._run(*argv, "--out", str(directory))

                    self.assertEqual(got, status)
                    self.assertEqual(out, "")
                    self.assertIn(f"replay_from_pick: {code}: ", err)

    def test_a_refusal_in_json_is_one_object_on_the_last_line_of_stderr(self) -> None:
        """The machine rule the broker CLI publishes: stdout stays empty and
        exactly one stderr line is a JSON object, the last one."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            for code, status, argv in self._refusals(directory):
                with self.subTest(code=code):
                    got, out, err = self._run(*argv, "--out", str(directory), "--format", "json")

                    self.assertEqual(got, status)
                    self.assertEqual(out, "")
                    envelope = json.loads(err.strip().splitlines()[-1])
                    self.assertEqual(
                        sorted(envelope),
                        [
                            "code",
                            "details",
                            "message",
                            "reason",
                            "retryable",
                            "schema",
                            "suggestions",
                        ],
                    )
                    self.assertEqual(
                        envelope["schema"], "alphalens.research.replay_from_pick.error/v1"
                    )
                    self.assertEqual(envelope["code"], code)
                    self.assertTrue(envelope["reason"])
                    self.assertTrue(envelope["message"])
                    self.assertFalse(envelope["retryable"])


class TheBarCacheRootIsStatedOrDerivedNeverJoinedTest(_CommandTestCase):
    """Polygon is metered at 5 requests a minute, so a replay re-run must read
    a cache rather than re-pay for it -- and a test must never read or write
    the operator's own."""

    def test_the_bar_cache_flag_is_on_the_parser(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, _, err = self._run(
                str(self._report(directory)),
                "VST:2026-09-21",
                "--out",
                str(directory),
                "--entry-trails",
                str(_JOURNAL),
                "--bar-cache",
                str(directory / "cache"),
                **self._seams(),
            )

            self.assertEqual(status, 0, err)

    def test_the_bar_cache_root_defaults_under_the_alphalens_home(self) -> None:
        """Through ``state_paths._alphalens_home()``, not a ``Path.home()`` join
        of its own: that seam is what the #1696 operator-state guard wraps, so a
        broker-tier test running this command without an override is refused
        loudly instead of reading the live cache."""
        with TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "alphalens-home"
            with mock.patch.object(state_paths, "_alphalens_home", return_value=home):
                derived = bar_cache_root(None)

            self.assertEqual(derived, home / "replay_bars")

    def test_a_stated_bar_cache_root_wins(self) -> None:
        with TemporaryDirectory() as tmp:
            stated = pathlib.Path(tmp) / "elsewhere"

            self.assertEqual(bar_cache_root(str(stated)), stated)


class TheUnservableWindowRefusesBeforeAnyVendorCallTest(_CommandTestCase):
    """A window whose right edge the price vendor cannot serve yet is refused
    with the transient status, BEFORE any metered call.

    The alternative is publishing `no_fill` with an empty trace for a window
    that could not observe the ladder's expiry -- which is also what a run that
    simply ran out of tape publishes, so the two become indistinguishable.
    """

    # The `now` every measurement in the memo's section 6.7 table was taken at.
    _NOW = dt.datetime(2026, 10, 9, 8, 14, 3, tzinfo=dt.UTC)

    def _comparable(self) -> list[str]:
        """The fixture records the report does not exclude -- the only ones the
        command will replay at all."""
        return sorted(k for k, t in self.trades.items() if not t.get("replay_exclusions"))

    def _replay(
        self,
        directory: pathlib.Path,
        pick: str,
        *,
        now: dt.datetime | None = None,
        fmt: str = "human",
    ) -> tuple[int, str, str, list[tuple[Any, ...]]]:
        """One run of `pick` with a recording fake fetch and a stated clock."""
        calls: list[tuple[Any, ...]] = []
        status, out, err = self._run(
            str(self._report(directory)),
            pick,
            "--out",
            str(directory),
            # Both stated, because `main` resolves the journal's own default
            # path through `state_paths` before it asks whether it needs it,
            # and the #1696 operator-state guard refuses that resolution.
            "--entry-trails",
            str(_JOURNAL),
            "--entry-trail-bps",
            "50",
            "--bar-cache",
            str(directory / "cache"),
            "--format",
            fmt,
            bar_fetch=lambda ticker, start, end: calls.append((ticker, start, end)) or [],
            now=lambda: now or self._NOW,
        )
        return status, out, err, calls

    def test_the_refusal_fires_on_six_of_the_twelve_comparable_records(self) -> None:
        """The measured cost of the half-open grid on the window, at the memo's
        own `now`: six of the twelve comparable fixture records cannot be served
        on 2026-10-09, where three could not under an inclusive close. Each of
        the six clears within two further calendar days with no code change.

        Counted by CODE and not by a non-zero status: the recording fake hands
        every run an empty tape, so the six servable picks go on to refuse
        `bars_unusable/empty_window`, which is a fact about this fake rather
        than about the window.
        """
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            refused = {}
            for pick in self._comparable():
                status, _, err, _ = self._replay(directory, pick)
                if "bars_window_unservable" in err:
                    refused[pick] = (status, err)

            self.assertEqual(len(self._comparable()), 12)
            self.assertEqual(
                sorted(refused),
                [
                    "BE:2026-09-29",
                    "BE:2026-09-30",
                    "OSCR:2026-09-30",
                    "PLTR:2026-09-29",
                    "SYM:2026-09-30",
                    "VCYT:2026-09-29",
                ],
            )
            for pick, (status, err) in refused.items():
                with self.subTest(pick=pick):
                    self.assertEqual(status, 7)
                    self.assertIn("bars_window_unservable", err)

    def test_the_servable_cap_does_not_move_after_the_session_close(self) -> None:
        """The cap is the vendor's CALENDAR DAY, not the last closed session:
        `session_not_closed` answers 2026-10-12 at 21:00Z, which would read the
        cap as 2026-10-09 and run a window the vendor does not serve. So the
        verdict has to be the same at 08:14Z and at 21:00Z."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, out, err, _ = self._replay(
                directory,
                "SYM:2026-09-30",
                now=dt.datetime(2026, 10, 9, 21, 0, tzinfo=dt.UTC),
                fmt="json",
            )

            self.assertEqual(status, 7)
            self.assertEqual(out, "")
            envelope = json.loads(err.strip().splitlines()[-1])
            self.assertTrue(envelope["retryable"])
            self.assertEqual(envelope["details"]["last_servable_session"], "2026-10-08")
            self.assertEqual(envelope["details"]["last_servable_end_t"], 1791489600000)

    def test_the_refusal_names_both_sessions_and_the_date_to_re_run_on(self) -> None:
        """Naming the deadline session alone reads as "the vendor is behind",
        where the reason is that the expiry is a session later. So the prose
        carries the deadline session 2026-10-09, the session the expiry is
        observable on 2026-10-12, and the earliest run 2026-10-13."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, out, err, calls = self._replay(directory, "SYM:2026-09-30")

            self.assertEqual(status, 7)
            self.assertEqual(out, "")
            for date in ("2026-10-09", "2026-10-12", "2026-10-13"):
                with self.subTest(date=date):
                    self.assertIn(date, err)
            self.assertEqual(calls, [], "an unanswerable window spends no vendor quota")
            self.assertEqual(sorted(p.name for p in directory.glob("SYM_*")), [])


class _FetchingTestCase(_CommandTestCase):
    """One run of VST:2026-09-21 over a stated vendor tape.

    VST is the pick whose window is servable at the memo's own `now`, so what
    these classes exercise is every gate AFTER the window: the grid, the
    granularity, the two edges and the file that gets written.
    """

    _PICK = "VST:2026-09-21"

    def _replay(
        self,
        directory: pathlib.Path,
        *,
        payload: list[dict[str, Any]] | None = None,
        raises: Exception | None = None,
        pick: str | None = None,
        fmt: str = "json",
        now: dt.datetime | None = None,
        cache: pathlib.Path | None = None,
    ) -> tuple[int, str, str, list[tuple[Any, ...]]]:
        """One run with a recording fake fetch, a stated clock and a cache root
        inside the temporary directory."""
        calls: list[tuple[Any, ...]] = []
        series = self._tape() if payload is None else payload

        def fetch(ticker: str, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
            calls.append((ticker, start, end))
            if raises is not None:
                raise raises
            return series

        status, out, err = self._run(
            str(self._report(directory)),
            pick or self._PICK,
            "--out",
            str(directory),
            # Stated because `main` resolves the journal's own default path
            # before it asks whether it needs it, and the #1696 operator-state
            # guard refuses that resolution.
            "--entry-trails",
            str(_JOURNAL),
            "--entry-trail-bps",
            "50",
            "--bar-cache",
            str(cache or directory / "cache"),
            "--format",
            fmt,
            bar_fetch=fetch,
            now=lambda: now or self.NOW,
        )
        return status, out, err, calls

    def _written(self, out: str) -> list[dict[str, Any]]:
        """The bars file the run wrote, parsed."""
        return json.loads(pathlib.Path(json.loads(out)["bars_path"]).read_text())

    def _stamps(self, out: str) -> list[int]:
        return [bar["t"] for bar in self._written(out)]


class TheBarsAreFetchedNotHandBuiltTest(_FetchingTestCase):
    """The bars were the one input this command did not build, so the operator
    built the file by hand -- and that file carried a bar ninety minutes after
    the session close, fired a take-profit on it, and was then lost, which is
    why the published 1.05% cannot be audited even now that it is known wrong.
    """

    def test_it_writes_a_bars_file_the_replay_argv_points_at(self) -> None:
        """The series the walk reads is the FILTERED one, so the command has to
        write it and point at it; `<BARS.json>` was a placeholder the operator
        filled in by hand."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, out, err, _ = self._replay(directory)

            self.assertEqual(status, 0, err)
            answer = json.loads(out)
            bars_path = pathlib.Path(answer["bars_path"])
            self.assertEqual(bars_path, directory / "VST_2026-09-21.bars.json")
            self.assertTrue(bars_path.exists())
            argv = answer["replay_argv"]
            self.assertEqual(argv[argv.index("--bars") + 1], str(bars_path))
            self.assertNotIn("<BARS.json>", argv)

    def test_the_bars_block_is_top_level_and_not_inside_the_configuration(self) -> None:
        """`configuration` is the engine's own run configuration and
        `RunConfig.from_jsonable` refuses a key it does not model, so a `bars`
        block written inside it would make every run refuse itself."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, out, err, _ = self._replay(directory)

            self.assertEqual(status, 0, err)
            answer = json.loads(out)
            self.assertEqual(answer["bars"]["path"], answer["bars_path"])
            self.assertEqual(answer["bars"]["counts"]["kept"], len(self._written(out)))
            self.assertNotIn("bars", answer["configuration"])
            RunConfig.from_jsonable(
                answer["configuration"],
                account_currency=json.loads(pathlib.Path(answer["document_path"]).read_text())[
                    "spec"
                ]["size"]["currency"],
            )

    def test_the_fetch_window_starts_at_walk_start(self) -> None:
        """The left edge is the configuration's own `walk_start`. The engine
        refuses a series that begins after it (`begins_after_walk_start`), so a
        fetch anchored on the trade date's midnight refuses itself."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, out, err, calls = self._replay(directory)

            self.assertEqual(status, 0, err)
            config = json.loads(pathlib.Path(json.loads(out)["config_path"]).read_text())
            self.assertEqual(len(calls), 1)
            self.assertEqual(_epoch_ms(calls[0][1]), config["walk_start"]["value"])

    def test_the_ticker_comes_from_the_document_not_from_the_record(self) -> None:
        """`document["instrument"]` is `{mic, ticker}`, so the venue and the
        symbol travel together from one source. The trades record's own
        instrument block carries a uic and no symbol at all."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, _, err, calls = self._replay(directory)

            self.assertEqual(status, 0, err)
            self.assertEqual(calls[0][0], "VST")

    def test_the_vendor_payload_is_translated_to_the_published_bar_shape(self) -> None:
        """The vendor sends `o/h/l/c` plus `v/vw/n`; the published shape is the
        closed key set `t, open, high, low, close`, and every other key is
        refused as `unknown_key` by the engine that reads the file."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, out, err, _ = self._replay(directory)

            self.assertEqual(status, 0, err)
            written = json.loads(pathlib.Path(json.loads(out)["bars_path"]).read_text())
            for bar in written:
                self.assertEqual(sorted(bar), ["close", "high", "low", "open", "t"])
                self.assertIsInstance(bar["t"], int)
            series = validate_sequence(parse_bars(written))
            config = json.loads(pathlib.Path(json.loads(out)["config_path"]).read_text())
            check_window_covers(series, config["walk_start"]["value"])

    def test_an_unmodelled_vendor_key_cannot_reach_the_bars_file(self) -> None:
        """Polygon sends `otc: true` on an OTC ticker. A converter that deleted
        the keys it knows about would pass that one straight through into an
        `unknown_key` refusal the operator cannot act on."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, out, err, _ = self._replay(directory, payload=self._tape(otc=True))

            self.assertEqual(status, 0, err)
            written = json.loads(pathlib.Path(json.loads(out)["bars_path"]).read_text())
            self.assertTrue(written)
            for bar in written:
                self.assertNotIn("otc", bar)

    def test_a_vendor_failure_exits_7_and_says_it_is_retryable(self) -> None:
        """A Polygon timeout used to fall into the catch-all and report 1, "the
        pick cannot be replayed" -- a false statement about the pick. The
        suggestion names the offline route rather than a sentence to parse."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, out, err, _ = self._replay(directory, raises=OSError("connection reset"))

            self.assertEqual(status, 7)
            self.assertEqual(out, "")
            envelope = json.loads(err.strip().splitlines()[-1])
            self.assertEqual(envelope["code"], "bars_fetch_failed")
            self.assertEqual(envelope["reason"], "vendor_error")
            self.assertTrue(envelope["retryable"])
            self.assertEqual(envelope["details"]["ticker"], "VST")
            self.assertIn("connection reset", envelope["details"]["vendor_message"])
            self.assertIn("--bars", envelope["suggestions"][0]["argv"])
            self.assertEqual(sorted(p.name for p in directory.glob("VST_*")), [])

    def test_an_empty_window_is_refused_and_nothing_is_written(self) -> None:
        """Writing the empty list and leaving `bars_empty` to the engine moves
        the refusal one tool downstream, where it can no longer say that the
        window this command asked the vendor for came back with nothing."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, out, err, _ = self._replay(directory, payload=[])

            self.assertEqual(status, 1)
            self.assertEqual(out, "")
            envelope = json.loads(err.strip().splitlines()[-1])
            self.assertEqual(envelope["code"], "bars_unusable")
            self.assertEqual(envelope["reason"], "empty_window")
            self.assertFalse(envelope["retryable"])
            self.assertEqual(sorted(p.name for p in directory.glob("VST_*")), [])

    def test_a_series_with_no_bar_at_walk_start_is_refused_here(self) -> None:
        """75 of 17 163 real (file, session) pairs carry no bar at the session
        open, and in 44 of them a pre-market bar does. The grid drops that bar,
        so the refusal has to say the venue printed no opening bar rather than
        hand the operator the engine's own `begins_after_walk_start`."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            late = _aggs(self.WALK_START + dt.timedelta(minutes=1), 3) + _aggs(
                self.AFTER_DEADLINE, 3, price=11.0
            )

            status, out, err, _ = self._replay(directory, payload=late)

            self.assertEqual(status, 1)
            self.assertEqual(out, "")
            envelope = json.loads(err.strip().splitlines()[-1])
            self.assertEqual(envelope["code"], "bars_unusable")
            self.assertEqual(envelope["reason"], "walk_start_uncovered")
            self.assertEqual(envelope["details"]["walk_start"], _epoch_ms(self.WALK_START))
            self.assertEqual(
                envelope["details"]["first_t"],
                _epoch_ms(self.WALK_START + dt.timedelta(minutes=1)),
            )
            self.assertEqual(sorted(p.name for p in directory.glob("VST_*")), [])

    def test_the_cache_holds_the_raw_payload_not_the_filtered_series(self) -> None:
        """A cached file has to stay a faithful record of what the vendor sent,
        so that a later change to the session grid re-derives from it instead of
        inheriting this run's filter."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            cache = directory / "cache"

            status, _, err, _ = self._replay(directory, cache=cache)

            self.assertEqual(status, 0, err)
            files = sorted(cache.glob("bars/*.json"))
            self.assertEqual(len(files), 1, files)
            self.assertIn("VST_", files[0].name)
            self.assertIn("_1m_raw.json", files[0].name)
            cached = json.loads(files[0].read_text())["bars"]
            self.assertEqual(cached, self._tape())
            for raw in cached:
                self.assertEqual(sorted(raw), ["c", "h", "l", "n", "o", "t", "v", "vw"])

    def test_a_cached_run_does_not_claim_a_fresh_fetch(self) -> None:
        """`fetched_at` is when the VENDOR answered, so it comes out of the
        cache with the payload. Stamping `now()` on every run would date a
        two-week-old payload to today."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            cache = directory / "cache"

            first = json.loads(self._replay(directory, cache=cache)[1])["bars"]
            second = json.loads(self._replay(directory, cache=cache)[1])["bars"]

            self.assertFalse(first["cache"]["hit"])
            self.assertTrue(second["cache"]["hit"])
            self.assertEqual(first["cache"]["path"], second["cache"]["path"])
            self.assertIn("VST_", pathlib.Path(first["cache"]["path"]).name)
            self.assertEqual(first["fetched_at"], second["fetched_at"])

    def test_the_human_renderer_names_the_bars_file_it_wrote(self) -> None:
        """The operator reads the human renderer, and the bars file is now one
        of the inputs the next command takes."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, out, err, _ = self._replay(directory, fmt="human")

            self.assertEqual(status, 0, err)
            self.assertIn(str(directory / "VST_2026-09-21.bars.json"), out.splitlines()[0])

    def test_a_corrupt_cache_file_is_refetched_with_a_warning_not_a_crash(self) -> None:
        """A half-written cache is never a reason for a command to fail: the
        vendor can be asked again. The shape is checked on the way in, so a
        file that parses as JSON but is not a payload re-fetches too rather
        than surfacing later as an error about a vendor nobody called."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            cache = directory / "cache"
            self._replay(directory, cache=cache)
            cached = next(iter(cache.glob("bars/*.json")))
            for corruption in ("{not json at all", '{"bars": [{"t": 1}]}', "[]"):
                with self.subTest(corruption=corruption):
                    cached.write_text(corruption)

                    status, _, err, calls = self._replay(directory, cache=cache)

                    self.assertEqual(status, 0, err)
                    self.assertEqual(len(calls), 1, "the corrupt cache must be re-fetched")
                    self.assertIn("unreadable bar cache", err)

    def test_an_empty_fetch_is_not_cached(self) -> None:
        """Only a SUCCESS is written, so a later run re-pays the vendor instead
        of inheriting a hole."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            cache = directory / "cache"

            status, _, _, _ = self._replay(directory, payload=[], cache=cache)

            self.assertEqual(status, 1)
            self.assertEqual(sorted(cache.glob("bars/*.json")), [])

    def test_a_second_run_of_the_same_pick_asks_the_vendor_nothing(self) -> None:
        """Polygon's free tier is metered at five requests a minute and
        re-running a replay is normal work. The window ends at a session the
        vendor has already published, so presence in the cache means complete
        and there is no TTL to get wrong."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            cache = directory / "cache"

            first = self._replay(directory, cache=cache)
            second = self._replay(directory, cache=cache)

            self.assertEqual((first[0], second[0]), (0, 0), first[2] + second[2])
            self.assertEqual(len(first[3]), 1)
            self.assertEqual(second[3], [], "the second run must spend no vendor quota")
            self.assertEqual(
                json.loads(pathlib.Path(json.loads(first[1])["bars_path"]).read_text()),
                json.loads(pathlib.Path(json.loads(second[1])["bars_path"]).read_text()),
            )


class TheCloseStampedBarIsNotPartOfTheSessionTest(_FetchingTestCase):
    """The minute stamped at the venue close spans close..close+59s, and the
    session ends AT the close, so that minute's high and low can carry trading
    that belongs to no session.

    Measured against the official daily `[low, high]`: keeping the whole bar
    leaves the real range on 164 of 15 677 real sessions (1.046%, worst 1457.5
    bps) where dropping it leaves 9 (0.057%, worst 34.7 bps) -- and on CRSR
    2026-08-06 keeping it flipped a real outcome from `open` to `closed_tp`.
    """

    # XNYS's own close on 2026-09-21, read from the calendar, not open + 390.
    _CLOSE = dt.datetime(2026, 9, 21, 20, 0, tzinfo=dt.UTC)

    def _tape_through_the_close(self, **extra: Any) -> list[dict[str, Any]]:
        """VST's own tape with the last two minutes of 2026-09-21 added: the
        one at 19:59:00Z, inside the session, and the one stamped exactly at the
        20:00:00Z close."""
        return (
            _aggs(self.WALK_START, 3)
            + _aggs(self._CLOSE - dt.timedelta(minutes=1), 2, price=20.0, **extra)
            + _aggs(self.AFTER_DEADLINE, 3, price=11.0)
        )

    def test_the_close_stamped_bar_is_dropped(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, out, err, _ = self._replay(directory, payload=self._tape_through_the_close())

            self.assertEqual(status, 0, err)
            stamps = self._stamps(out)
            self.assertIn(_epoch_ms(self._CLOSE - dt.timedelta(minutes=1)), stamps)
            self.assertNotIn(_epoch_ms(self._CLOSE), stamps)

    def test_only_the_bar_at_the_session_close_is_dropped(self) -> None:
        """Dropping by POSITION -- the last bar of the series -- instead of by
        `t == close_ms` deletes a real intraday bar, which is the same defect
        pointing the other way."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, out, err, _ = self._replay(directory, payload=self._tape_through_the_close())

            self.assertEqual(status, 0, err)
            survivor = next(
                bar
                for bar in self._written(out)
                if bar["t"] == _epoch_ms(self._CLOSE - dt.timedelta(minutes=1))
            )
            self.assertEqual(
                survivor,
                {"t": survivor["t"], "open": 20.0, "high": 20.5, "low": 19.5, "close": 20.1},
            )

    def test_the_last_bar_of_the_series_is_not_special(self) -> None:
        """This tape's final session contributes no close-stamped bar, so the
        drop count is one short of the session count and the final bar stays."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, out, err, _ = self._replay(directory, payload=self._tape_through_the_close())

            self.assertEqual(status, 0, err)
            counts = json.loads(out)["bars"]["counts"]
            self.assertEqual(counts["dropped_at_close"], 1)
            self.assertIn(
                _epoch_ms(self.AFTER_DEADLINE + dt.timedelta(minutes=2)), self._stamps(out)
            )

    def test_no_written_bar_carries_a_price_the_vendor_did_not_send(self) -> None:
        """Nothing in this design rewrites a bar's content. The retired
        alternative -- reducing the close bar to its own open -- also made every
        level between that bar's low and its open unreachable, because the walk
        tests a rung with `bar.low <= limit`."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            payload = self._tape_through_the_close()

            status, out, err, _ = self._replay(directory, payload=payload)

            self.assertEqual(status, 0, err)
            sent = {raw["t"]: (raw["o"], raw["h"], raw["l"], raw["c"]) for raw in payload}
            for bar in self._written(out):
                with self.subTest(t=bar["t"]):
                    self.assertEqual(
                        (bar["open"], bar["high"], bar["low"], bar["close"]), sent[bar["t"]]
                    )

    def test_the_two_drop_counts_are_kept_apart(self) -> None:
        """A bar the grid EXCLUDES and a bar it cannot PLACE are different
        facts: the first is one per session on 91.34% of real pairs, the second
        is the incident's own shape. One count for both would hide either."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            # ONE bar at a close against TWO the grid cannot place -- a
            # pre-market print, and the incident's own bar ninety minutes after
            # the close. Equal counts would not tell the two reads apart.
            payload = (
                _aggs(self.WALK_START - dt.timedelta(minutes=30), 1, price=9.0)
                + _aggs(self.WALK_START, 3)
                + _aggs(self._CLOSE - dt.timedelta(minutes=1), 2, price=20.0)
                + _aggs(self._CLOSE + dt.timedelta(minutes=90), 1, price=30.0)
                + _aggs(self.AFTER_DEADLINE, 3, price=11.0)
            )

            status, out, err, _ = self._replay(directory, payload=payload)

            self.assertEqual(status, 0, err)
            counts = json.loads(out)["bars"]["counts"]
            self.assertEqual(counts["dropped_at_close"], 1)
            self.assertEqual(counts["dropped_off_grid"], 2)
            self.assertNotIn(
                _epoch_ms(self.WALK_START - dt.timedelta(minutes=30)), self._stamps(out)
            )
            self.assertNotIn(_epoch_ms(self._CLOSE + dt.timedelta(minutes=90)), self._stamps(out))

    def test_the_counts_account_for_every_bar_the_vendor_sent(self) -> None:
        """`fetched` is what the vendor sent, and the three it splits into have
        to add up to it: a payload is 12.08% off-grid bars on average, so a
        reader who cannot see the whole cannot tell a filter from a loss."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            payload = (
                _aggs(self.WALK_START - dt.timedelta(minutes=30), 1, price=9.0)
                + _aggs(self.WALK_START, 3)
                + _aggs(self._CLOSE - dt.timedelta(minutes=1), 2, price=20.0)
                + _aggs(self.AFTER_DEADLINE, 3, price=11.0)
            )

            status, out, err, _ = self._replay(directory, payload=payload)

            self.assertEqual(status, 0, err)
            counts = json.loads(out)["bars"]["counts"]
            self.assertEqual(counts["fetched"], len(payload))
            self.assertEqual(
                counts["fetched"],
                counts["kept"] + counts["dropped_at_close"] + counts["dropped_off_grid"],
            )

    def test_the_dropped_bar_is_recoverable_with_its_own_quadruple(self) -> None:
        """The session series -- the bars inside the grid plus the one per
        session the grid excludes -- is the written file plus this list. The
        SMMT file's own close-bar treatment can no longer be reconstructed at
        all, and that is the larger half of the incident's cost.

        `range_narrowed` is the discriminating number, not the drop count: a
        close-stamped bar exists on 91.34% of real sessions, while its range
        leaves the rest of its session on 1.161% of them.
        """
        window = bar_window(
            walk_start=self.WALK_START,
            entry_deadline=dt.datetime(2026, 9, 30, 20, 0, tzinfo=dt.UTC),
            exit_instant=dt.datetime(2026, 10, 1, 13, 59, 21, tzinfo=dt.UTC),
            mic="XNYS",
            now=self.NOW,
        )
        # The real CRSR 2026-08-06 quadruple, whose own LOW is the closing
        # auction print and whose high ran 1457.5 bps past the official one.
        crsr = {
            "t": _epoch_ms(self._CLOSE),
            "o": 10.89,
            "h": 12.80,
            "l": 10.61,
            "c": 12.79,
            "v": 1,
            "vw": 11.0,
            "n": 1,
        }
        payload = (
            _aggs(self.WALK_START, 3)
            + _aggs(self._CLOSE - dt.timedelta(minutes=1), 1, price=11.0)
            + [crsr]
            + _aggs(self.AFTER_DEADLINE, 3, price=11.0)
        )
        with TemporaryDirectory() as tmp:
            built = build_bars(
                pick=self._PICK,
                ticker="VST",
                mic="XNYS",
                window=window,
                bar_fetch=lambda ticker, start, end: payload,
                cache_root=pathlib.Path(tmp),
                now=self.NOW,
                offline_argv=[],
            )

        self.assertNotIsInstance(built, BarRefusal)
        dropped = built.grid.dropped_at_close
        self.assertEqual(len(dropped), built.grid.counts()["dropped_at_close"])
        self.assertEqual([drop.t for drop in dropped], [_epoch_ms(self._CLOSE)])
        self.assertEqual(dropped[0].session, dt.date(2026, 9, 21))
        self.assertEqual(
            dropped[0].bar, {"open": 10.89, "high": 12.80, "low": 10.61, "close": 12.79}
        )
        self.assertTrue(dropped[0].outside_rest_of_session)

    def test_a_dropped_bar_inside_its_session_s_own_range_is_not_counted_as_narrowing(self) -> None:
        """The close-stamped bar is near-constant at one per session, so only
        the bars whose range left their session's range carry signal."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            calm = (
                _aggs(self.WALK_START, 3)
                + _aggs(self._CLOSE - dt.timedelta(minutes=2), 2, price=50.0)
                # The close-stamped bar, inside the range the session traded.
                + [
                    {
                        "t": _epoch_ms(self._CLOSE),
                        "o": 50.0,
                        "h": 50.1,
                        "l": 49.9,
                        "c": 50.0,
                        "v": 1,
                        "vw": 50.0,
                        "n": 1,
                    }
                ]
                + _aggs(self.AFTER_DEADLINE, 3, price=11.0)
            )
            window = bar_window(
                walk_start=self.WALK_START,
                entry_deadline=dt.datetime(2026, 9, 30, 20, 0, tzinfo=dt.UTC),
                exit_instant=None,
                mic="XNYS",
                now=self.NOW,
            )
            built = build_bars(
                pick=self._PICK,
                ticker="VST",
                mic="XNYS",
                window=window,
                bar_fetch=lambda ticker, start, end: calm,
                cache_root=pathlib.Path(tmp) / "cache",
                now=self.NOW,
                offline_argv=[],
            )

            self.assertNotIsInstance(built, BarRefusal)
            self.assertEqual(len(built.grid.dropped_at_close), 1)
            self.assertFalse(built.grid.dropped_at_close[0].outside_rest_of_session)

    def test_dropping_it_never_empties_a_session(self) -> None:
        """A pin on the measurement: 0 of 17 163 real pairs fall below two kept
        bars under this grid, and the bars-per-pair floor is 13 under both
        grids. It turns red if the drop is ever applied before the membership
        filter."""
        sessions = session_grid(
            dt.datetime(2026, 9, 21, 13, 30, tzinfo=dt.UTC),
            dt.datetime(2026, 9, 22, 20, 0, tzinfo=dt.UTC),
            mic="XNYS",
        )
        bars = _bars_at(
            *(
                dt.datetime(2026, 9, 21, 13, 30, tzinfo=dt.UTC) + dt.timedelta(minutes=step)
                for step in range(13)
            ),
            dt.datetime(2026, 9, 21, 20, 0, tzinfo=dt.UTC),
            *(
                dt.datetime(2026, 9, 22, 13, 30, tzinfo=dt.UTC) + dt.timedelta(minutes=step)
                for step in range(13)
            ),
            dt.datetime(2026, 9, 22, 20, 0, tzinfo=dt.UTC),
        )

        gridded = filter_to_grid(bars, sessions, mic="XNYS")

        self.assertEqual(len(gridded.dropped_at_close), 2)
        for bounds in gridded.sessions:
            with self.subTest(session=bounds.session):
                on_it = [bar for bar in gridded.kept if bounds.member(bar["t"])]
                self.assertEqual(len(on_it), 13)


class TheGranularityGateReadsTheWholeKeptSeriesTest(_FetchingTestCase):
    """D2's reason -- one low per session carries no path -- applies to every
    coarse granularity, not only to one bar per session, so the gate is phrased
    on the modal `t`-spacing of the whole kept series.

    The grid cannot double as that check: a daily series stamped at each
    session's OPEN passes the grid intact (10 of 10 measured), while one
    stamped at each CLOSE grids to nothing. Those two shapes refuse for two
    different reasons and the operator has to be told which.
    """

    # XNYS's own sessions over VST:2026-09-21's window. Every one opens
    # 13:30:00Z and closes 20:00:00Z -- all EDT, no half-day -- and the dates
    # are literals so the test cannot inherit a wrong grid from the code it
    # measures.
    _SESSIONS = (
        dt.date(2026, 9, 21),
        dt.date(2026, 9, 22),
        dt.date(2026, 9, 23),
        dt.date(2026, 9, 24),
        dt.date(2026, 9, 25),
        dt.date(2026, 9, 28),
        dt.date(2026, 9, 29),
        dt.date(2026, 9, 30),
        dt.date(2026, 10, 1),
        dt.date(2026, 10, 2),
        dt.date(2026, 10, 5),
    )

    def _one_per_session(self, hour: int, minute: int) -> list[dict[str, Any]]:
        """One bar per session of the window, stamped at the stated UTC time."""
        return [
            _aggs(dt.datetime.combine(day, dt.time(hour, minute), tzinfo=dt.UTC), 1)[0]
            for day in self._SESSIONS
        ]

    def test_a_daily_series_stamped_at_each_session_open_is_refused_by_granularity(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, out, err, _ = self._replay(directory, payload=self._one_per_session(13, 30))

            self.assertEqual(status, 1)
            self.assertEqual(out, "")
            envelope = json.loads(err.strip().splitlines()[-1])
            self.assertEqual(envelope["code"], "bars_wrong_granularity")
            self.assertEqual(envelope["reason"], "not_one_minute")
            self.assertEqual(envelope["details"]["modal_spacing_ms"], 86_400_000)
            self.assertEqual(envelope["details"]["bars"], len(self._SESSIONS))
            self.assertEqual(sorted(p.name for p in directory.glob("VST_*")), [])

    def test_a_daily_series_stamped_at_each_session_close_is_refused_as_empty(self) -> None:
        """What this repository's own daily store holds -- 492 of 497 files are
        stamped exactly at the close. The grid keeps none of it, so reporting
        `not_one_minute` would send the operator to fix the granularity of a
        series that has no bars to measure."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)

            status, _, err, _ = self._replay(directory, payload=self._one_per_session(20, 0))

            self.assertEqual(status, 1)
            envelope = json.loads(err.strip().splitlines()[-1])
            self.assertEqual(envelope["code"], "bars_unusable")
            self.assertEqual(envelope["reason"], "empty_window")
            self.assertNotIn("not_one_minute", err)

    def test_an_hourly_series_is_refused_too(self) -> None:
        """A one-bar-per-session rule is silent on hourly, 30-minute and
        5-minute series, each of which the grid keeps almost whole."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            hourly = [
                _aggs(
                    dt.datetime.combine(day, dt.time(13, 30), tzinfo=dt.UTC)
                    + dt.timedelta(hours=step),
                    1,
                )[0]
                for day in self._SESSIONS
                for step in range(6)
            ]

            status, _, err, _ = self._replay(directory, payload=hourly)

            self.assertEqual(status, 1)
            envelope = json.loads(err.strip().splitlines()[-1])
            self.assertEqual(envelope["code"], "bars_wrong_granularity")
            self.assertEqual(envelope["details"]["modal_spacing_ms"], 3_600_000)

    def test_a_sparse_session_whose_commonest_gap_is_two_minutes_is_not_refused(self) -> None:
        """Phrasing the gate per session would refuse 26 of 17 163 real pairs
        whose commonest gap is 2, 3, 4 or 9 minutes, where the whole-series rule
        fires on 0 of 881 real windows."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            sparse = [
                _aggs(self.WALK_START + dt.timedelta(minutes=2 * step), 1)[0] for step in range(3)
            ] + _aggs(self.AFTER_DEADLINE, 6, price=11.0)

            status, out, err, _ = self._replay(directory, payload=sparse)

            self.assertEqual(status, 0, err)
            self.assertEqual(json.loads(out)["bars"]["modal_spacing_ms"], 60_000)

    def test_a_one_bar_series_is_not_refused_as_coarse(self) -> None:
        """Section 5.7's one-bar-at-`walk_start` case: it has no spacing, so the
        granularity gate is exempt by construction.

        What refuses such a run is the RIGHT EDGE -- one bar cannot reach the
        ladder's expiry -- and the operator has to be told that rather than told
        the bars are too coarse. (The memo's class B row expects status 0 here;
        under section 6.6 no single-bar series can pass the right edge.)
        """
        window = bar_window(
            walk_start=self.WALK_START,
            entry_deadline=dt.datetime(2026, 9, 30, 20, 0, tzinfo=dt.UTC),
            exit_instant=None,
            mic="XNYS",
            now=self.NOW,
        )
        with TemporaryDirectory() as tmp:
            built = build_bars(
                pick="VST:2026-09-21",
                ticker="VST",
                mic="XNYS",
                window=window,
                bar_fetch=lambda ticker, start, end: _aggs(self.WALK_START, 1),
                cache_root=pathlib.Path(tmp),
                now=self.NOW,
                offline_argv=[],
            )

        self.assertIsInstance(built, BarRefusal)
        self.assertEqual(built.code, "bars_window_unservable")
        self.assertEqual(built.reason, "vendor_served_short")

    def test_a_series_of_one_bar_has_no_spacing_to_measure(self) -> None:
        """Which is what keeps the one-bar-at-`walk_start` case legal: a gate
        that read a spacing off it would have to invent one."""
        self.assertIsNone(modal_spacing_ms([{"t": 1}]))
        self.assertIsNone(modal_spacing_ms([]))
        self.assertEqual(modal_spacing_ms([{"t": 0}, {"t": 60_000}, {"t": 120_000}]), 60_000)


class TheRightEdgeIsCheckedOnTheSeriesAfterTheFetchTest(_FetchingTestCase):
    """Predicting the vendor's day boundary is a claim; checking the series is a
    measurement, so the requirement is asked again of the bars that came back.

    A run whose tape stops before the ladder's expiry is observable publishes
    outcome `no_fill` with an empty trace -- which is also what a ladder that
    genuinely expired publishes, so a reader cannot tell the two apart.
    """

    # VST:2026-09-21's entry deadline: the close of its last live session.
    _DEADLINE = dt.datetime(2026, 9, 30, 20, 0, tzinfo=dt.UTC)

    def test_a_series_that_stops_before_the_deadline_is_refused_after_the_fetch(self) -> None:
        """The window's stated end (2026-10-05T20:00Z) is past the deadline
        here, and the series is not: phrasing the check on `window_end` would
        run it."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            short = _aggs(self.WALK_START, 3) + _aggs(
                self._DEADLINE - dt.timedelta(hours=6), 3, price=11.0
            )

            status, out, err, _ = self._replay(directory, payload=short)

            self.assertEqual(status, 7)
            self.assertEqual(out, "")
            envelope = json.loads(err.strip().splitlines()[-1])
            self.assertEqual(envelope["code"], "bars_window_unservable")
            self.assertEqual(envelope["reason"], "vendor_served_short")
            self.assertTrue(envelope["retryable"])
            self.assertEqual(
                envelope["details"]["last_bar_t"],
                _epoch_ms(self._DEADLINE - dt.timedelta(hours=6) + dt.timedelta(minutes=2)),
            )
            self.assertEqual(envelope["details"]["entry_deadline_t"], _epoch_ms(self._DEADLINE))
            self.assertEqual(sorted(p.name for p in directory.glob("VST_*")), [])

    def test_a_bar_stamped_at_the_deadline_itself_does_not_satisfy_the_right_edge(self) -> None:
        """The deadline IS a session close, so under the half-open grid that bar
        is dropped and the expiry stays unobservable: observing it needs a bar
        from the NEXT session. A grid inclusive at the close would run this."""
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            up_to_the_close = _aggs(self.WALK_START, 3) + _aggs(
                self._DEADLINE - dt.timedelta(minutes=2), 3, price=11.0
            )

            status, _, err, _ = self._replay(directory, payload=up_to_the_close)

            self.assertEqual(status, 7)
            envelope = json.loads(err.strip().splitlines()[-1])
            self.assertEqual(envelope["reason"], "vendor_served_short")
            self.assertEqual(
                envelope["details"]["last_bar_t"],
                _epoch_ms(self._DEADLINE - dt.timedelta(minutes=1)),
            )
            self.assertIn("2026-10-01", err)


class TheHelpTextIsThePublishedSurfaceTest(unittest.TestCase):
    """``usage=argparse.SUPPRESS`` with a raw-description formatter means
    argparse generates no option list, so a flag the hand-written help text
    omits is invisible and a status it omits is undocumented."""

    def _help(self) -> str:
        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit),
        ):
            main(["--help"])
        return out.getvalue()

    def test_the_exit_codes_block_lists_every_status_the_command_returns(self) -> None:
        block = self._help().split("EXIT CODES", 1)[1].split("EXAMPLES", 1)[0]

        for status in ("0", "1", "2", "4", "7"):
            with self.subTest(status=status):
                self.assertRegex(block, rf"\b{status}\b")

    def test_the_output_block_names_every_path_the_answer_carries(self) -> None:
        """The answer gained `bars_path` and a `bars` block, and the written set
        went from two files to three. A published block that lists neither tells
        the operator the command does less than it does."""
        block = self._help().split("OUTPUT", 1)[1].split("EXIT CODES", 1)[0]

        for key in ("document_path", "config_path", "bars_path", "bars"):
            with self.subTest(key=key):
                self.assertIn(key, block)

    def test_the_options_block_names_the_bar_cache_flag(self) -> None:
        block = self._help().split("OPTIONS", 1)[1].split("OUTPUT", 1)[0]

        self.assertIn("--bar-cache", block)


class TheSeamsAreReachableFromATestTest(_CommandTestCase):
    """``_run`` forwarded no keywords, so no test could state the clock, the bar
    fetch or the corporate-action lookup -- and the only thing that ever would
    was a production default that calls a metered vendor over the network.

    A silently dropped keyword is the failure to catch: ``main`` ignores an
    argument it is never handed, so an end-to-end run stays green either way.
    """

    def test_run_forwards_all_three_seams_to_the_command(self) -> None:
        seams: dict[str, Any] = {
            "bar_fetch": object(),
            "actions_lookup": object(),
            "now": object(),
        }

        with mock.patch("tests.test_replay_from_pick.main", return_value=0) as spy:
            self._run("trades.json", "VST:2026-09-21", **seams)

        self.assertEqual(spy.call_args.kwargs, seams)
        self.assertEqual(spy.call_args.args, (["trades.json", "VST:2026-09-21"],))


if __name__ == "__main__":
    unittest.main()
