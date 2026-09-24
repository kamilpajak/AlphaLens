"""CLI tests for `alphalens broker arm` — the document door (#1406, #1468, #1470).

The door takes a ready document, so a producer needs JSON rather than a Typer
command of its own. Since #1468 the author writes only the TRADE; the door
derives identity and labels. What is pinned here, and why each one is not
obvious:

1. **What the door derives, it refuses on input** — `intent_id`, `armed_ts`,
   `r_multiple` — and what it fills (`trade_date`, `generation`, tags) is
   journaled, so the author can see it in the dry run.
2. **Every refusal leaves the inbox untouched** — asserted on the file's bytes,
   not merely on a non-zero exit. A door that refused after appending would be
   worse than no door.
3. **The writability table** (measured 2026-09-11): resubmitting after `disarm`
   RESURRECTED the pick, and resubmitting after the daemon had placed it rewrote
   a queue line nobody would ever act on. Both are refusals, each with a positive
   control beside it. A replace keeps the `armed_ts` of the line it replaces.
4. **Silent losses.** A typo'd key and a duplicate JSON key both used to arrive
   as a pick carrying a value the client never sent.
5. **No "LIVE without rails refuses" test.** Measured: it does not, and it never
   did. Arming is not placing; the rails gate the daemon. The real guard is the
   ambiguous-shell refusal, and that is what is asserted.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from typer.testing import CliRunner

ARMING_MOMENT = dt.datetime(2026, 9, 11, 15, 0, tzinfo=dt.UTC)


def _isolate_home(case: unittest.TestCase) -> Path:
    tmp = TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    home = Path(tmp.name)
    patcher = mock.patch("pathlib.Path.home", return_value=home)
    patcher.start()
    case.addCleanup(patcher.stop)
    return home


def _document(**meta: object) -> dict:
    """The document an author writes: the trade, and nothing the door computes.

    `trade_date` is stated so a test does not depend on the frozen clock unless
    it is about the clock.
    """
    return {
        "instrument": {"ticker": "NVO", "mic": "XNYS"},
        "spec": {
            "entry_tiers": [
                {"limit_price": 72.5, "alloc_pct": 60.0},
                {"limit_price": 70.0, "alloc_pct": 40.0},
            ],
            "disaster_stop": 66.0,
            "tp_tranches": [
                {"price": 80.0, "tranche_pct": 50.0},
                {"price": 82.0, "tranche_pct": 50.0},
            ],
            "size": {"notional_acct": 3000.0, "currency": "USD"},
        },
        "meta": {"source": "manual", "trade_date": "2026-09-11", **meta},
    }


class _DoorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = _isolate_home(self)
        self.clock = mock.patch(
            "alphalens_cli.commands.broker._arming_now", return_value=ARMING_MOMENT
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.inbox = self.home / ".alphalens" / "broker_orders" / "sim" / "picks.jsonl"

    def invoke(self, argv: list[str], stdin: str | None = None):
        from alphalens_cli.commands.broker import broker_app

        return self.runner.invoke(broker_app, argv, input=stdin)

    def write(self, document: object) -> str:
        path = self.home / "intent.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def arm(self, document: object, *extra: str):
        return self.invoke(["arm", self.write(document), *extra])

    def inbox_bytes(self) -> bytes:
        return self.inbox.read_bytes() if self.inbox.exists() else b""

    def failure_of(self, result) -> dict:
        self.assertNotEqual(result.exit_code, 0, result.output)
        return json.loads(result.stderr.strip().splitlines()[-1])

    def assert_refused(self, result, code: str, reason: str | None = None) -> dict:
        failure = self.failure_of(result)
        self.assertEqual(failure["code"], code, failure)
        if reason is not None:
            self.assertEqual(failure["details"].get("reason"), reason, failure)
        return failure

    def move_clock(self, moment: dt.datetime) -> None:
        self.clock.stop()
        self.clock = mock.patch("alphalens_cli.commands.broker._arming_now", return_value=moment)
        self.clock.start()

    def journaled_intent(self) -> dict:
        records = self.fold_records()
        self.assertEqual(len(records), 1)
        return records[0].record["intent"]

    def fold_records(self):
        from alphalens_pipeline.brokers.automanager.picks import read_pick_fold

        return read_pick_fold(path=self.inbox).records


class TheDoorDerivesWhatAnAuthorShouldNotCompute(_DoorCase):
    """#1468 acceptance 1, 2 and 6, end to end."""

    def test_the_journaled_line_carries_every_derived_field(self) -> None:
        result = self.arm(_document())

        self.assertEqual(result.exit_code, 0, result.output)
        intent = self.journaled_intent()
        self.assertEqual(intent["intent_id"], "NVO:2026-09-11:manual")
        self.assertEqual(intent["meta"]["armed_ts"], "2026-09-11T15:00:00+00:00")
        self.assertEqual(intent["meta"]["generation"], 1)
        self.assertEqual([t["tag"] for t in intent["spec"]["entry_tiers"]], ["T1", "T2"])
        self.assertEqual([t["tag"] for t in intent["spec"]["tp_tranches"]], ["TP1", "TP2"])

    def test_r_multiples_match_a_hand_worked_example(self) -> None:
        """Blend = 0.6 * 72.5 + 0.4 * 70 = 71.5, so 1R = 71.5 - 66 = 5.5.
        TP 80 is 8.5 / 5.5 R and TP 82 is 10.5 / 5.5 R."""
        self.assertEqual(self.arm(_document()).exit_code, 0)

        tranches = self.journaled_intent()["spec"]["tp_tranches"]
        self.assertAlmostEqual(tranches[0]["r_multiple"], 8.5 / 5.5)
        self.assertAlmostEqual(tranches[1]["r_multiple"], 10.5 / 5.5)

    def test_a_missing_trade_date_is_the_session_that_has_not_closed(self) -> None:
        """After the New York close the next session, not the closed one."""
        self.move_clock(dt.datetime(2026, 9, 17, 1, 30, tzinfo=dt.UTC))
        document = _document()
        del document["meta"]["trade_date"]

        self.assertEqual(self.arm(document).exit_code, 0)

        self.assertEqual(self.journaled_intent()["meta"]["trade_date"], "2026-09-17")

    def test_each_derived_field_sent_is_refused_and_nothing_changes(self) -> None:
        for label, mutate in (
            ("intent_id", lambda d: d.__setitem__("intent_id", "NVO:2026-09-11:manual")),
            ("armed_ts", lambda d: d["meta"].__setitem__("armed_ts", "2026-09-11T00:00:00Z")),
            ("r_multiple", lambda d: d["spec"]["tp_tranches"][1].__setitem__("r_multiple", 2.0)),
        ):
            with self.subTest(field=label):
                document = _document()
                mutate(document)
                path = self.write(document)
                sent = Path(path).read_bytes()

                result = self.invoke(["arm", path, "--format", "json"])

                failure = self.assert_refused(result, "intent_malformed", "derived_field_supplied")
                self.assertTrue(any(label in p for p in failure["details"]["paths"]))
                self.assertEqual(Path(path).read_bytes(), sent)
                self.assertEqual(self.inbox_bytes(), b"")

    def test_an_envelope_is_not_a_document(self) -> None:
        """The door does not peel envelopes (#1468), so the group's own answer
        sent back in is refused as the wrong shape."""
        envelope = {"schema": "alphalens.broker.arm/v2", "env": "sim", "intent": {}}
        result = self.invoke(["arm", "-", "--format", "json"], stdin=json.dumps(envelope))
        self.assert_refused(result, "intent_malformed", "schema_violation")
        self.assertEqual(self.inbox_bytes(), b"")

    def test_a_document_without_source_is_refused(self) -> None:
        document = _document()
        del document["meta"]["source"]
        failure = self.assert_refused(
            self.arm(document, "--format", "json"), "intent_malformed", "schema_violation"
        )
        self.assertIn("meta", failure["details"]["path"])
        self.assertEqual(self.inbox_bytes(), b"")

    def test_a_brief_document_without_its_date_is_refused(self) -> None:
        document = _document(source="brief")
        del document["meta"]["trade_date"]
        self.assert_refused(
            self.arm(document, "--format", "json"), "intent_malformed", "trade_date_required"
        )
        self.assertEqual(self.inbox_bytes(), b"")


class TheHappyPath(_DoorCase):
    def test_a_document_from_a_file_is_armed(self) -> None:
        result = self.arm(_document())

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual([(r.ticker, r.status) for r in self.fold_records()], [("NVO", "armed")])

    def test_json_mode_answers_with_the_group_envelope(self) -> None:
        result = self.arm(_document(), "--format", "json")

        payload = json.loads(result.stdout.strip())
        self.assertEqual(payload["schema"], "alphalens.broker.arm/v2")
        self.assertEqual(payload["env"], "sim")
        self.assertTrue(payload["armed"])
        self.assertEqual(payload["ticker"], "NVO")
        self.assertEqual(payload["generation"], 1)
        self.assertEqual(payload["intent_id"], "NVO:2026-09-11:manual")
        self.assertEqual(payload["armed_ts"], "2026-09-11T15:00:00+00:00")
        self.assertFalse(payload["replaces"])
        self.assertEqual(payload["tier_amounts"], [1800.0, 1200.0])
        self.assertEqual(payload["intent"], self.journaled_intent())

    def test_a_human_dry_run_shows_what_would_be_journaled(self) -> None:
        result = self.arm(_document(), "--dry-run")

        self.assertEqual(result.exit_code, 0, result.output)
        for fragment in (
            "NVO:2026-09-11:manual",
            "armed_ts 2026-09-11T15:00:00+00:00",
            "1800.00 USD",
            "1.55R",
        ):
            self.assertIn(fragment, result.stdout)
        self.assertEqual(self.inbox_bytes(), b"")

    def test_dry_run_compiles_and_appends_nothing(self) -> None:
        result = self.arm(_document(), "--dry-run", "--format", "json")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(json.loads(result.stdout.strip())["armed"])
        self.assertEqual(self.inbox_bytes(), b"")

    def test_dry_run_still_runs_every_gate(self) -> None:
        """`--dry-run` is not a way past the rules — the README says it runs
        them all, so a document that would be refused is refused."""
        document = _document()
        document["spec"]["size"]["notional_acct"] = 0.0

        result = self.arm(document, "--dry-run", "--format", "json")

        self.assert_refused(result, "intent_invalid")
        self.assertEqual(self.inbox_bytes(), b"")

    def test_an_unrenderable_envelope_refuses_without_arming(self) -> None:
        """Render BEFORE the append, so a payload that
        cannot be serialised strictly reports a failure for something that has
        NOT happened. Rendering afterwards would hand a client the
        `write_outcome_unknown` shape this group exists to keep out."""
        from alphalens_cli.commands import broker as broker_cli

        with mock.patch.object(broker_cli, "_render_json", side_effect=broker_cli._fail("boom")):
            result = self.arm(_document(), "--format", "json")

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(self.inbox_bytes(), b"")

    def test_a_failed_append_reports_a_failure_object_not_a_traceback(self) -> None:
        """#1421: the contract promises exactly one JSON object on stderr in
        JSON mode. A disk-full append used to escape as a traceback, leaving a
        machine caller with exit 1 and nothing to parse. `retryable` is true
        because this command writes only to the queue — no broker order can be
        in flight — and the shared appender repairs a torn predecessor, so a
        retry lands cleanly."""
        from alphalens_pipeline.brokers.journal import JournalWriteError

        with mock.patch(
            "alphalens_pipeline.brokers.automanager.picks.arm_pick",
            side_effect=JournalWriteError("No space left on device"),
        ):
            result = self.arm(_document(), "--format", "json")

        self.assertEqual(result.exit_code, 7, result.output)
        self.assertEqual(result.stdout, "")
        failure = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(failure["code"], "queue_write_failed")
        self.assertTrue(failure["retryable"])

    def test_stdin_is_a_source_like_any_other(self) -> None:
        result = self.invoke(["arm", "-"], stdin=json.dumps(_document()))

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(self.fold_records()), 1)


class TheDocumentMustBeTheRightShape(_DoorCase):
    """Every refusal names its reason, and none of them touches the inbox."""

    def assert_untouched(self, result, code: str, reason: str | None = None) -> None:
        before = self.inbox_bytes()
        self.assert_refused(result, code, reason)
        self.assertEqual(self.inbox_bytes(), before)
        self.assertEqual(result.stdout, "", "a refusal must leave stdout empty")

    def test_a_missing_file_is_a_usage_error(self) -> None:
        result = self.invoke(["arm", str(self.home / "nope.json"), "--format", "json"])
        self.assert_untouched(result, "usage")

    def test_text_that_is_not_json_is_refused(self) -> None:
        result = self.invoke(["arm", "-", "--format", "json"], stdin="not json at all")
        self.assert_untouched(result, "intent_malformed", "not_json")

    def test_an_empty_stdin_is_refused(self) -> None:
        result = self.invoke(["arm", "-", "--format", "json"], stdin="")
        self.assert_untouched(result, "intent_malformed", "not_json")

    def test_a_document_that_is_not_an_object_is_refused(self) -> None:
        result = self.invoke(["arm", "-", "--format", "json"], stdin="[1, 2, 3]")
        self.assert_untouched(result, "intent_malformed", "schema_violation")

    def test_a_duplicate_key_is_refused_rather_than_silently_resolved(self) -> None:
        """`json.loads` keeps the LAST of a repeated key without a word, so a
        producer building JSON by concatenation would arm at a price it never
        meant to send. This is the only layer where the duplicate is visible."""
        text = json.dumps(_document()).replace(
            '"limit_price": 72.5', '"limit_price": 72.5, "limit_price": 5.0', 1
        )
        result = self.invoke(["arm", "-", "--format", "json"], stdin=text)

        failure = self.failure_of(result)
        self.assertEqual(failure["details"]["reason"], "duplicate_key")
        self.assertEqual(failure["details"]["keys"], ["limit_price"])
        self.assertEqual(self.inbox_bytes(), b"")

    def test_every_repeated_key_in_one_object_is_named(self) -> None:
        """Not just the first: a producer fixing its serialiser wants the whole
        list in one pass, the same reason `intent_invalid` carries every
        violation rather than the first."""
        text = (
            json.dumps(_document())
            .replace('"limit_price": 72.5', '"limit_price": 72.5, "limit_price": 5.0', 1)
            .replace('"alloc_pct": 60.0', '"alloc_pct": 60.0, "alloc_pct": 1.0', 1)
        )
        result = self.invoke(["arm", "-", "--format", "json"], stdin=text)

        failure = self.assert_refused(result, "intent_malformed", "duplicate_key")
        self.assertEqual(failure["details"]["keys"], ["alloc_pct", "limit_price"])

    def test_a_schema_violation_names_where(self) -> None:
        document = _document()
        document["instrument"]["mic"] = 17
        failure = self.assert_refused(
            self.arm(document, "--format", "json"), "intent_malformed", "schema_violation"
        )
        self.assertIn("instrument", failure["details"]["path"])

    def test_a_version_this_door_does_not_speak_is_refused(self) -> None:
        document = _document()
        document["meta"]["schema_version"] = "4"
        result = self.arm(document, "--format", "json")
        self.assert_untouched(result, "intent_malformed", "schema_version_unsupported")

    def test_the_retired_version_is_refused_too(self) -> None:
        """Not a regression: no real v1 document can pass the schema anyway (they
        all carry the pre-#1252 date key), so accepting "1" would have published
        a promise about a population of zero. The journal DRAIN still reads v1
        through the codec — a different entry point, deliberately ungated."""
        document = _document()
        document["meta"]["schema_version"] = "1"
        result = self.arm(document, "--format", "json")
        self.assert_untouched(result, "intent_malformed", "schema_version_unsupported")

    def test_an_absent_version_is_the_current_one(self) -> None:
        """Positive control on the two above: the field carries a default, so
        omitting it means "the version this contract is at", not "unknown"."""
        result = self.arm(_document())
        self.assertEqual(result.exit_code, 0, result.output)

    def test_a_shape_the_schema_accepts_and_the_codec_refuses_is_still_caught(self) -> None:
        """JSON Schema defines `integer` as any number with zero fractional part,
        so `1.0` passes it; the codec refuses because identity strings are built
        from this field (#1371). The door runs BOTH gates for exactly this."""
        document = _document(generation=1.0)
        result = self.arm(document, "--format", "json")
        self.assert_untouched(result, "intent_malformed", "undecodable")

    def test_the_schema_gate_is_what_protects_the_validator_from_a_wrong_type(self) -> None:
        """Ordering carries weight here, so it is asserted rather than assumed.

        `validate_intent` is not total: given a string or a boolean where it
        expects a number it raises `TypeError` from `math.isfinite`, not a
        refusal. The schema runs FIRST and catches those, so the door answers
        with a failure object. Run the gates in the other order and the same
        documents produce a traceback.
        """
        for label, mutate in (
            ("a string price", lambda d: d["spec"].__setitem__("disaster_stop", "66.0")),
            ("a boolean size", lambda d: d["spec"]["size"].__setitem__("notional_acct", True)),
            ("a boolean ttl", lambda d: d["spec"].__setitem__("order_ttl_days", True)),
        ):
            with self.subTest(document=label):
                document = _document()
                mutate(document)
                result = self.arm(document, "--format", "json")

                self.assert_refused(result, "intent_malformed", "schema_violation")
                self.assertEqual(self.inbox_bytes(), b"")

    def test_a_trade_date_that_is_not_a_date_is_refused(self) -> None:
        """It would have armed a journal line the queue fold reads as MALFORMED —
        a pick that exists, is never drained, and never says why."""
        document = _document()
        document["meta"]["trade_date"] = "the eleventh"
        result = self.arm(document, "--format", "json")
        self.assert_untouched(result, "intent_malformed", "trade_date_malformed")


class NothingTheClientSentMayBeDiscarded(_DoorCase):
    """The fixed point: decode then re-render must give back what arrived."""

    def test_a_typo_in_a_key_is_refused_instead_of_dropped(self) -> None:
        document = _document()
        document["spec"]["entry_tiers"][0]["limit_pirce"] = 999.0
        result = self.arm(document, "--format", "json")

        failure = self.assert_refused(result, "intent_malformed", "key_discarded")
        self.assertEqual(failure["details"]["paths"], ["spec.entry_tiers[0].limit_pirce"])
        self.assertEqual(self.inbox_bytes(), b"")

    def test_an_unknown_key_anywhere_is_refused(self) -> None:
        document = _document()
        document["meta"]["armed_by"] = "a bot"
        self.assert_refused(
            self.arm(document, "--format", "json"), "intent_malformed", "key_discarded"
        )

    def test_the_legacy_date_key_is_refused_at_the_door(self) -> None:
        """The codec migrates it for the DRAIN, which reads history. A new
        producer sending it would watch its own key silently become another."""
        document = _document()
        document["meta"]["brief_date"] = document["meta"].pop("trade_date")
        self.assert_refused(
            self.arm(document, "--format", "json"), "intent_malformed", "key_discarded"
        )

    def test_an_explicitly_null_exit_survives(self) -> None:
        """The gate's most plausible false refusal, and it is not one: `asdict`
        never omits a field, so `exit: null` comes back as `exit: null`. Kept as
        a test so nobody "fixes" the gate into refusing a legal document."""
        result = self.arm({**_document(), "exit": None})
        self.assertEqual(result.exit_code, 0, result.output)

    def test_a_document_omitting_every_defaulted_field_survives(self) -> None:
        """Defaults and derived fields we ADD are not losses — only keys that
        vanish are. The fixture omits every optional field."""
        result = self.arm(_document())
        self.assertEqual(result.exit_code, 0, result.output)


class TheDocumentMustAlsoBeCoherentAndTradable(_DoorCase):
    def test_allocations_that_do_not_sum_are_the_validator_s_refusal(self) -> None:
        document = _document()
        document["spec"]["entry_tiers"][0]["alloc_pct"] = 250.0
        failure = self.assert_refused(self.arm(document, "--format", "json"), "intent_invalid")
        self.assertEqual(failure["details"]["reason"], "entry_alloc_sum")
        self.assertEqual(self.inbox_bytes(), b"")

    def test_a_non_positive_amount_is_refused(self) -> None:
        document = _document()
        document["spec"]["size"]["notional_acct"] = -1.0
        failure = self.assert_refused(self.arm(document, "--format", "json"), "intent_invalid")
        self.assertEqual(failure["details"]["reason"], "size_notional_not_positive")

    def test_a_pre_1467_percent_document_is_refused_and_nothing_arms(self) -> None:
        """A v2 document sizes by percent. With no version stated it reaches the
        schema, which has no `suggested_size_pct` and requires `size`."""
        document = _document()
        del document["spec"]["size"]
        document["spec"]["suggested_size_pct"] = 3.0
        document["spec"].pop("schema_version", None)
        document["meta"].pop("schema_version", None)
        self.assert_refused(self.arm(document, "--format", "json"), "intent_malformed")
        self.assertEqual(self.inbox_bytes(), b"")

    def test_a_document_stating_version_2_is_refused_by_the_version_gate(self) -> None:
        document = _document()
        document["meta"]["schema_version"] = "2"
        failure = self.assert_refused(self.arm(document, "--format", "json"), "intent_malformed")
        self.assertEqual(failure["details"]["reason"], "schema_version_unsupported")

    def test_a_short_is_refused(self) -> None:
        document = _document()
        document["spec"]["side"] = "short"
        self.assert_refused(self.arm(document, "--format", "json"), "intent_malformed")

    def test_a_venue_this_deployment_does_not_trade_is_its_own_code(self) -> None:
        """Caught by NO other gate, measured: the schema keeps `mic` a free
        string by decision, and the validator deliberately does not know the
        venue list. The door imports it rather than copying it."""
        document = _document()
        document["instrument"]["mic"] = "XAMS"
        failure = self.assert_refused(self.arm(document, "--format", "json"), "venue_unsupported")
        self.assertEqual(failure["details"]["mic"], "XAMS")
        self.assertEqual(self.inbox_bytes(), b"")


class APaddedTickerCannotSlipPastTheKey(_DoorCase):
    """`arm-manual` stripped the ticker; a hand-edited document may not. The fold
    upper-cases without stripping, so `" NVO "` armed beside `NVO` would be a
    second live pick on one instrument (#1470)."""

    def test_a_padded_ticker_is_refused_and_nothing_arms(self) -> None:
        document = _document()
        document["instrument"]["ticker"] = " NVO "

        self.assert_refused(
            self.arm(document, "--format", "json"), "intent_invalid", "ticker_not_trimmed"
        )
        self.assertEqual(self.inbox_bytes(), b"")

    def test_a_lower_case_ticker_still_arms_upper_cased(self) -> None:
        document = _document()
        document["instrument"]["ticker"] = "nvo"

        result = self.arm(document)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual([r.ticker for r in self.fold_records()], ["NVO"])


class ThePickKeyMustBeWritable(_DoorCase):
    """The table the first draft of this ticket got wrong."""

    def _arm_once(self) -> dict:
        document = _document(generation=1)
        self.assertEqual(self.arm(document).exit_code, 0)
        return document

    def test_resubmitting_the_same_document_replaces_rather_than_duplicates(self) -> None:
        """Criterion 6, in the sense the journal actually supports: the file is
        append-only, so a retry leaves TWO lines and ONE folded record."""
        document = self._arm_once()
        result = self.arm(document)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(self.inbox.read_text().strip().splitlines()), 2)
        self.assertEqual(len(self.fold_records()), 1)

    def test_a_changed_document_under_the_same_key_wins(self) -> None:
        self._arm_once()
        changed = _document(generation=1)
        changed["spec"]["disaster_stop"] = 60.0

        self.assertEqual(self.arm(changed).exit_code, 0)

        record = self.fold_records()[0]
        self.assertEqual(record.record["intent"]["spec"]["disaster_stop"], 60.0)

    def test_a_disarmed_generation_never_comes_back(self) -> None:
        """Measured 2026-09-11: without this the retry-after-timeout path
        silently RESURRECTED a pick the operator had cancelled."""
        from alphalens_pipeline.brokers.automanager.picks import mark_disarmed

        document = self._arm_once()
        mark_disarmed("NVO", dt.date(2026, 9, 11), note="cancelled", path=self.inbox)
        before = self.inbox_bytes()

        result = self.arm(document, "--format", "json")

        self.assert_refused(result, "pick_not_writable", "generation_spent")
        self.assertEqual(self.inbox_bytes(), before)
        self.assertEqual(self.fold_records()[0].status, "disarmed")

    def test_a_pick_the_daemon_has_placed_is_not_rewritten(self) -> None:
        """The drain skips keys already in submissions.jsonl, so a "replacement"
        would change the queue and never the market."""
        document = self._arm_once()
        submissions = self.inbox.with_name("submissions.jsonl")
        submissions.write_text(
            json.dumps({"ticker": "NVO", "trade_date": "2026-09-11", "order_id": "O-1"}) + "\n",
            encoding="utf-8",
        )
        before = self.inbox_bytes()

        result = self.arm(document, "--format", "json")

        self.assert_refused(result, "pick_not_writable", "already_placed")
        self.assertEqual(self.inbox_bytes(), before)

    def test_a_pick_whose_now_half_rests_at_the_broker_is_not_rewritten(self) -> None:
        """The hole the first version of this table had.

        An immediate-entry tier is placed as its own submission record, and that
        record deliberately does NOT retire the pick (#1247) — the pullback
        half's does. So `submitted_pick_keys`, which answers the drain's
        question, reports nothing for the key while a real order rests at the
        broker. Measured 2026-09-11: the SIM journal holds a key in exactly that
        state. The door asks a different question and must use the helper that
        answers it.
        """
        document = self._arm_once()
        submissions = self.inbox.with_name("submissions.jsonl")
        submissions.write_text(
            json.dumps(
                {
                    "ticker": "NVO",
                    "trade_date": "2026-09-11",
                    "tranche": "now",
                    "tranche_meta": {"outcome": "placed"},
                    "brackets": [{"entry_order_id": "O-77"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        before = self.inbox_bytes()

        result = self.arm(document, "--format", "json")

        self.assert_refused(result, "pick_not_writable", "already_placed")
        self.assertEqual(self.inbox_bytes(), before)

    def test_a_now_half_REFUSED_above_its_cap_stays_replaceable(self) -> None:
        """Positive control for the refusal above.

        When the ask is above the operator's cap the daemon journals a terminal
        refusal and places nothing, so the line is still unplaced and a replace
        is allowed. The replace keeps `armed_ts`, so the refused now half stays
        done — see the test below; a new cap needs `disarm` and a new document.
        """
        document = self._arm_once()
        self.inbox.with_name("submissions.jsonl").write_text(
            json.dumps(
                {
                    "ticker": "NVO",
                    "trade_date": "2026-09-11",
                    "tranche": "now",
                    "tranche_meta": {"outcome": "refused_cap"},
                    "brackets": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = self.arm(document)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(self.fold_records()), 1)

    def test_a_replace_keeps_armed_ts_so_a_refused_now_half_is_not_re_sent(self) -> None:
        """#1468 acceptance 4, read through the drain's own idempotency check,
        not through the field: a raised cap on a replace must not re-open the
        immediate tier."""
        from alphalens_pipeline.brokers.automanager.control_loop import _now_already_done
        from alphalens_pipeline.brokers.submission_log import iter_submission_records
        from broker_contract.trade_intent.codec import intent_from_jsonable

        document = _document(generation=1)
        document["spec"]["entry_tiers"][0]["entry_mode"] = "immediate"
        self.assertEqual(self.arm(document).exit_code, 0)
        first_armed_ts = self.journaled_intent()["meta"]["armed_ts"]
        submissions = self.inbox.with_name("submissions.jsonl")
        submissions.write_text(
            json.dumps(
                {
                    "ticker": "NVO",
                    "trade_date": "2026-09-11",
                    "tranche": "now",
                    "tranche_meta": {"outcome": "refused_cap", "armed_ts": first_armed_ts},
                    "brackets": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.move_clock(ARMING_MOMENT + dt.timedelta(hours=1))
        raised = _document(generation=1)
        raised["spec"]["entry_tiers"][0].update(entry_mode="immediate", limit_price=73.5)

        self.assertEqual(self.arm(raised).exit_code, 0)

        intent = intent_from_jsonable(self.journaled_intent())
        self.assertEqual(intent.meta.armed_ts, first_armed_ts)
        self.assertEqual(intent.spec.entry_tiers[0].limit_price, 73.5)
        records = list(iter_submission_records(submissions))
        self.assertTrue(_now_already_done(records, "NVO", intent))

    def test_a_retry_without_generation_is_refused_while_the_pick_is_armed(self) -> None:
        """#1468 acceptance 3: omitting `generation` asks for a NEW pick."""
        self._arm_once()
        before = self.inbox_bytes()

        result = self.arm(_document(), "--format", "json")

        self.assert_refused(result, "pick_already_armed")
        self.assertEqual(self.inbox_bytes(), before)
        self.assertEqual(len(self.fold_records()), 1)

    def test_the_same_retry_on_a_later_derived_date_is_refused_too(self) -> None:
        document = _document()
        del document["meta"]["trade_date"]
        self.assertEqual(self.arm(document).exit_code, 0)
        self.move_clock(dt.datetime(2026, 9, 12, 1, 30, tzinfo=dt.UTC))
        before = self.inbox_bytes()

        result = self.arm(document, "--format", "json")

        failure = self.assert_refused(result, "pick_already_armed")
        self.assertEqual(failure["details"]["armed_trade_date"], "2026-09-11")
        self.assertEqual(self.inbox_bytes(), before)

    def test_a_manual_nasdaq_pick_beside_an_armed_nyse_one_is_refused(self) -> None:
        self._arm_once()
        other = _document(trade_date="2026-09-14")
        other["instrument"]["mic"] = "XNAS"
        self.assert_refused(self.arm(other, "--format", "json"), "pick_already_armed")

    def test_a_second_live_generation_on_one_instrument_is_refused(self) -> None:
        self._arm_once()
        second = _document(generation=2)
        before = self.inbox_bytes()

        result = self.arm(second, "--format", "json")

        self.assert_refused(result, "pick_already_armed")
        self.assertEqual(self.inbox_bytes(), before)

    def test_a_new_generation_after_a_disarm_is_allowed(self) -> None:
        """Positive control for the two refusals above: the operator's own
        correction path must stay open."""
        from alphalens_pipeline.brokers.automanager.picks import mark_disarmed

        self._arm_once()
        mark_disarmed("NVO", dt.date(2026, 9, 11), note="cancelled", path=self.inbox)
        second = _document(generation=2)

        self.assertEqual(self.arm(second).exit_code, 0)
        self.assertEqual(
            sorted((r.token, r.status) for r in self.fold_records()),
            [("2026-09-11", "disarmed"), ("2026-09-11-g2", "armed")],
        )


class TheInstanceIsNeverAmbiguous(_DoorCase):
    def test_env_live_writes_to_the_live_inbox(self) -> None:
        result = self.arm(_document(), "--env", "live")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue((self.home / ".alphalens/broker_orders/live/picks.jsonl").exists())
        self.assertEqual(self.inbox_bytes(), b"")

    def test_a_live_shell_without_an_explicit_env_refuses(self) -> None:
        """The guard that DOES exist. There is no "LIVE rails absent" refusal on
        the arming commands — measured — because arming is not placing."""
        with mock.patch.dict("os.environ", {"ALPHALENS_BROKER_ENVIRONMENT": "live"}):
            result = self.arm(_document(), "--format", "json")

        self.assert_refused(result, "env_ambiguous")
        self.assertEqual(self.inbox_bytes(), b"")


class TheLegacyLayoutGuardRunsFirst(_DoorCase):
    """ADR 0016 D4: on a pre-ADR-0016 flat layout the door refuses before it
    persists anything (moved from the deleted `test_arm_cli.py`, #1469)."""

    def test_a_legacy_layout_refuses_and_creates_no_inbox(self) -> None:
        from tests.brokers.automanager.cli_isolation import _seed_legacy_flat_state

        _seed_legacy_flat_state(self.home)

        result = self.arm(_document())

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("legacy flat broker state", result.output)
        self.assertIn("Migrate into the per-environment layout", result.output)
        self.assertFalse(self.inbox.exists())


class TheDefaultInstanceMirrorsTheSeam(unittest.TestCase):
    """`_DEFAULT_ARM_ENV` is a literal copy of `state_paths.ENV_SIM`, kept literal
    so the option default needs no module-scope import (lazy-CLI doctrine)."""

    def test_default_arm_env_matches_the_seam_sim_constant(self) -> None:
        from alphalens_cli.commands import broker
        from alphalens_pipeline.brokers.automanager import state_paths

        self.assertEqual(broker._DEFAULT_ARM_ENV, state_paths.ENV_SIM)


class AMissingDocumentPointsAtTheTemplates(_DoorCase):
    """#1552: there is no brief producer any more. Every pick is a hand-written
    document, so a path the door cannot read points at the templates, and the
    old brief form (`arm TICKER --date D ...`) is an ordinary usage error."""

    TEMPLATE_ARGV = ["alphalens", "broker", "arm", "<FILE>", "--dry-run"]

    def test_help_lists_arm_and_no_other_arming_command(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        names = {command.name for command in broker_app.registered_commands}
        self.assertIn("arm", names)
        self.assertNotIn("arm-intent", names)
        self.assertNotIn("arm-manual", names)

    def test_a_missing_path_suggests_starting_from_a_template(self) -> None:
        for source in ("KO", str(self.home / "my-pick.json"), "picks/KO"):
            with self.subTest(source=source):
                result = self.invoke(["arm", source, "--format", "json"])

                failure = self.assert_refused(result, "usage")
                self.assertEqual(result.stdout, "")
                (suggestion,) = failure["suggestions"]
                self.assertEqual(suggestion["argv"], self.TEMPLATE_ARGV)
                self.assertIn("examples/manual-pick", suggestion["why"])

    def test_the_old_brief_form_is_a_usage_error_that_arms_nothing(self) -> None:
        result = self.invoke(
            ["arm", "KO", "--date", "2026-09-16", "--frame", "24000", "--currency", "PLN"]
        )

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.inbox.exists())

    def test_no_output_names_the_removed_producer(self) -> None:
        result = self.invoke(["arm", "KO"])

        self.assertNotIn("thematic intent", result.output)


class TheImmediateTierRulesHoldAtTheDoor(_DoorCase):
    """`arm-manual` compiled `now@` tiers and refused a bad ladder before the
    document existed. The rules live in `validate_intent`; these pin that the
    door reaches them (#1470)."""

    @staticmethod
    def _with_tiers(*tiers: dict) -> dict:
        document = _document()
        document["spec"]["entry_tiers"] = list(tiers)
        return document

    def test_two_immediate_tiers_are_refused(self) -> None:
        document = self._with_tiers(
            {"limit_price": 73.0, "alloc_pct": 50.0, "entry_mode": "immediate"},
            {"limit_price": 72.0, "alloc_pct": 50.0, "entry_mode": "immediate"},
        )
        self.assert_refused(
            self.arm(document, "--format", "json"), "intent_invalid", "immediate_tier_count"
        )
        self.assertEqual(self.inbox_bytes(), b"")

    def test_an_immediate_tier_after_a_pullback_is_refused(self) -> None:
        document = self._with_tiers(
            {"limit_price": 72.0, "alloc_pct": 50.0},
            {"limit_price": 73.0, "alloc_pct": 50.0, "entry_mode": "immediate"},
        )
        self.assert_refused(
            self.arm(document, "--format", "json"), "intent_invalid", "immediate_tier_not_first"
        )
        self.assertEqual(self.inbox_bytes(), b"")


class TheEchoNamesAnUncoveredPosition(_DoorCase):
    """Ported from `arm-manual` (#1470): a take-profit ladder that covers less than
    the whole position leaves the rest to the stop policy, and the human echo says so."""

    def test_under_100_percent_is_called_out(self) -> None:
        document = _document()
        document["spec"]["tp_tranches"] = [{"price": 80.0, "tranche_pct": 60.0}]

        result = self.arm(document, "--dry-run")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("40% of the position has no TP", result.stdout)

    def test_full_coverage_says_nothing(self) -> None:
        result = self.arm(_document(), "--dry-run")

        self.assertNotIn("has no TP", result.stdout)

    def test_no_warning_when_the_document_places_its_own_levels(self) -> None:
        """With `exit.initial_levels` the daemon places those levels, not the
        tranches (#1414), so a tranche sum says nothing about coverage."""
        document = _document()
        document["spec"]["tp_tranches"] = [{"price": 80.0, "tranche_pct": 60.0}]
        document["exit"] = {
            "initial_levels": {"stop": 66.0, "tp": 80.0},
            "reaction_plan": [{"kind": "trailing_stop", "arm_trigger_r": 0.5, "trail_frac": 0.6}],
        }

        result = self.arm(document, "--dry-run")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("has no TP", result.stdout)


if __name__ == "__main__":
    unittest.main()
