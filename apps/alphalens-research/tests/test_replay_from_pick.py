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
import io
import json
import pathlib
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.brokers.automanager.trades import TradesFilters, build_trades
from broker_contract.trade_intent.codec import supplied_derived_paths
from intent_replay.config import RunConfig
from scripts.replay_from_pick import (
    authored_document,
    entry_trail_bps_by_pick,
    main,
    replay_inputs,
    run_configuration,
    stated_facts,
)

from tests.brokers.automanager.trades_fixture import NOW, FakeFillHistory, install_journals

_JOURNAL = (
    pathlib.Path(__file__).parent
    / "brokers/automanager/fixtures/trades/live_2026_10_03/entry_trails.jsonl"
)


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


class TheCommandWritesBothInputsAndSaysHowToRunThemTest(unittest.TestCase):
    """The entry point exists because hand-assembling the run is the error this
    module is about: a driver written per pick is where the wrong rate, the
    wrong threshold and the wrong calendar each came from."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.trades = _trades()

    def _report(self, directory: pathlib.Path) -> pathlib.Path:
        path = directory / "trades.json"
        path.write_text(json.dumps({"env": "live", "trades": list(self.trades.values())}))
        return path

    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(list(args))
        return status, out.getvalue(), err.getvalue()

    def test_it_writes_the_two_files_the_replay_takes(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            status, out, _ = self._run(
                str(self._report(directory)),
                "VST:2026-09-21",
                "--out",
                str(directory),
                "--entry-trails",
                str(_JOURNAL),
                "--format",
                "json",
            )
            self.assertEqual(status, 0)
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
            status, out, _ = self._run(
                str(self._report(directory)),
                "VST:2026-09-21",
                "--out",
                str(directory),
                "--entry-trails",
                str(_JOURNAL),
                "--format",
                "json",
            )
            self.assertEqual(status, 0)
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

            status, out, _ = self._run(
                str(self._report(directory)),
                "VST:2026-09-21",
                "--out",
                str(directory),
                "--entry-trails",
                str(empty),
                "--entry-trail-bps",
                "0",
                "--format",
                "json",
            )
            self.assertEqual(status, 0)
            self.assertIsNone(json.loads(out)["configuration"]["entry_trail_bps"])


if __name__ == "__main__":
    unittest.main()
