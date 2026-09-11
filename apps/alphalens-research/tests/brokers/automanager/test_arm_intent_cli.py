"""CLI tests for `alphalens broker arm-intent` — the raw-document door (#1406).

The last step of #1403. `intent_from_jsonable` had exactly one caller in the
tree, on the DRAIN side; nothing accepted a ready document on the way IN, so
every new producer had to become another Typer command. This door takes the
document itself, so `arm`, `arm-manual` and `arm-intent` become three producers
of one artefact rather than three ways into the daemon.

What is pinned here, and why each one is not obvious:

1. **Round trip across two producers.** What `arm-manual` emits, fed back
   through this door, queues a BYTE-IDENTICAL journal line. Both the envelope
   and the bare intent are accepted, and both give the same bytes.
2. **Every refusal leaves the inbox untouched** — asserted on the file's bytes,
   not merely on a non-zero exit. A door that refused after appending would be
   worse than no door.
3. **The writability table** (measured 2026-09-11, and the reason the first
   draft of this ticket was wrong): resubmitting a document after `disarm`
   RESURRECTED the pick, and resubmitting after the daemon had placed it
   rewrote a queue line nobody would ever act on, because the drain skips keys
   already in `submissions.jsonl`. Both are refusals now, each with a positive
   control beside it.
4. **Silent losses.** A typo'd key and a duplicate JSON key both used to arrive
   as a pick carrying a value the client never sent — the codec drops unknown
   keys with a log line, and `json.loads` keeps the LAST of a repeated key.
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

_ARM_MANUAL_ARGS = [
    "arm-manual",
    "nvo",
    "--tier",
    "72.5:60",
    "--tier",
    "70.0:40",
    "--stop",
    "66.0",
    "--tp",
    "80:50",
    "--tp",
    "2R:50",
    "--size-pct",
    "3",
]


def _isolate_home(case: unittest.TestCase) -> Path:
    tmp = TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    home = Path(tmp.name)
    patcher = mock.patch("pathlib.Path.home", return_value=home)
    patcher.start()
    case.addCleanup(patcher.stop)
    return home


def _document(**overrides: object) -> dict:
    """A real compiled intent, never a hand-authored dict.

    Built through the same compiler `arm-manual` uses, so a field this fixture
    carries is a field the product emits — the failure mode where a test passes
    against a shape no producer creates.
    """
    from alphalens_pipeline.brokers.automanager.manual_intent import build_manual_intent
    from broker_contract.trade_intent.codec import intent_to_jsonable

    intent = build_manual_intent(
        ticker="NVO",
        mic="XNYS",
        tiers_raw=["72.5:60", "70.0:40"],
        stop=66.0,
        tps_raw=["80:50", "2R:50"],
        no_tp=False,
        size_pct=3.0,
        notional=None,
        frame=None,
        ttl_days=None,
        arm_date=dt.date(2026, 9, 11),
        armed_ts="2026-09-11T12:00:00+00:00",
        generation=1,
    )
    document = intent_to_jsonable(intent)
    document.update(overrides)
    return document


class _DoorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = _isolate_home(self)
        self.inbox = self.home / ".alphalens" / "broker_orders" / "sim" / "picks.jsonl"

    def invoke(self, argv: list[str], stdin: str | None = None):
        from alphalens_cli.commands.broker import broker_app

        return self.runner.invoke(broker_app, argv, input=stdin)

    def write(self, document: object) -> str:
        path = self.home / "intent.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def arm(self, document: object, *extra: str):
        return self.invoke(["arm-intent", self.write(document), *extra])

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

    def fold_records(self):
        from alphalens_pipeline.brokers.automanager.picks import read_pick_fold

        return read_pick_fold(path=self.inbox).records


class TheRoundTripIsIdentity(_DoorCase):
    """Acceptance criterion 1, on the SERIALISED form."""

    def _arm_manual_then_clear(self) -> tuple[bytes, dict]:
        result = self.invoke([*_ARM_MANUAL_ARGS, "--format", "json"])
        self.assertEqual(result.exit_code, 0, result.output)
        line = self.inbox.read_bytes()
        self.inbox.unlink()
        return line, json.loads(result.stdout.strip())

    def test_the_envelope_arm_manual_emits_queues_the_same_bytes(self) -> None:
        line, envelope = self._arm_manual_then_clear()

        result = self.invoke(["arm-intent", "-"], stdin=json.dumps(envelope))

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.inbox.read_bytes(), line)

    def test_the_bare_intent_inside_it_queues_the_same_bytes(self) -> None:
        """The published contract is the bare TradeIntent; unwrapping our own
        envelope is a convenience on top, so both roads must end in one place."""
        line, envelope = self._arm_manual_then_clear()

        result = self.invoke(["arm-intent", "-"], stdin=json.dumps(envelope["intent"]))

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.inbox.read_bytes(), line)


class TheHappyPath(_DoorCase):
    def test_a_document_from_a_file_is_armed(self) -> None:
        result = self.arm(_document())

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual([(r.ticker, r.status) for r in self.fold_records()], [("NVO", "armed")])

    def test_json_mode_answers_with_the_group_envelope(self) -> None:
        result = self.arm(_document(), "--format", "json")

        payload = json.loads(result.stdout.strip())
        self.assertEqual(payload["schema"], "alphalens.broker.arm-intent/v1")
        self.assertEqual(payload["env"], "sim")
        self.assertTrue(payload["armed"])
        self.assertEqual(payload["ticker"], "NVO")
        self.assertEqual(payload["generation"], 1)

    def test_dry_run_compiles_and_appends_nothing(self) -> None:
        result = self.arm(_document(), "--dry-run", "--format", "json")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(json.loads(result.stdout.strip())["armed"])
        self.assertEqual(self.inbox_bytes(), b"")

    def test_dry_run_still_runs_every_gate(self) -> None:
        """`--dry-run` is not a way past the rules — the README says it runs
        them all, so a document that would be refused is refused."""
        document = _document()
        document["spec"]["suggested_size_pct"] = 180.0

        result = self.arm(document, "--dry-run", "--format", "json")

        self.assert_refused(result, "intent_invalid")
        self.assertEqual(self.inbox_bytes(), b"")

    def test_an_unrenderable_envelope_refuses_without_arming(self) -> None:
        """The `arm` precedent: render BEFORE the append, so a payload that
        cannot be serialised strictly reports a failure for something that has
        NOT happened. Rendering afterwards would hand a client the
        `write_outcome_unknown` shape this group exists to keep out."""
        from alphalens_cli.commands import broker as broker_cli

        with mock.patch.object(broker_cli, "_render_json", side_effect=broker_cli._fail("boom")):
            result = self.arm(_document(), "--format", "json")

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(self.inbox_bytes(), b"")

    def test_stdin_is_a_source_like_any_other(self) -> None:
        result = self.invoke(["arm-intent", "-"], stdin=json.dumps(_document()))

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
        result = self.invoke(["arm-intent", str(self.home / "nope.json"), "--format", "json"])
        self.assert_untouched(result, "usage")

    def test_text_that_is_not_json_is_refused(self) -> None:
        result = self.invoke(["arm-intent", "-", "--format", "json"], stdin="not json at all")
        self.assert_untouched(result, "intent_malformed", "not_json")

    def test_an_empty_stdin_is_refused(self) -> None:
        result = self.invoke(["arm-intent", "-", "--format", "json"], stdin="")
        self.assert_untouched(result, "intent_malformed", "not_json")

    def test_a_document_that_is_not_an_object_is_refused(self) -> None:
        result = self.invoke(["arm-intent", "-", "--format", "json"], stdin="[1, 2, 3]")
        self.assert_untouched(result, "intent_malformed", "schema_violation")

    def test_a_duplicate_key_is_refused_rather_than_silently_resolved(self) -> None:
        """`json.loads` keeps the LAST of a repeated key without a word, so a
        producer building JSON by concatenation would arm at a price it never
        meant to send. This is the only layer where the duplicate is visible."""
        text = json.dumps(_document()).replace(
            '"limit_price": 72.5', '"limit_price": 72.5, "limit_price": 5.0', 1
        )
        result = self.invoke(["arm-intent", "-", "--format", "json"], stdin=text)

        failure = self.failure_of(result)
        self.assertEqual(failure["details"]["reason"], "duplicate_key")
        self.assertEqual(failure["details"]["keys"], ["limit_price"])
        self.assertEqual(self.inbox_bytes(), b"")

    def test_an_envelope_we_do_not_publish_is_refused_rather_than_guessed(self) -> None:
        envelope = {"schema": "someone.else/v1", "env": "sim", "intent": _document()}
        result = self.invoke(["arm-intent", "-", "--format", "json"], stdin=json.dumps(envelope))
        self.assert_untouched(result, "intent_malformed", "envelope_unknown")

    def test_an_envelope_of_ours_carrying_no_intent_is_refused(self) -> None:
        envelope = {"schema": "alphalens.broker.arm-manual/v1", "env": "sim", "armed": False}
        result = self.invoke(["arm-intent", "-", "--format", "json"], stdin=json.dumps(envelope))
        self.assert_untouched(result, "intent_malformed", "envelope_unknown")

    def test_a_schema_violation_names_where(self) -> None:
        result = self.arm(_document(intent_id=17), "--format", "json")
        failure = self.assert_refused(result, "intent_malformed", "schema_violation")
        self.assertIn("intent_id", failure["details"]["path"])

    def test_a_version_this_door_does_not_speak_is_refused(self) -> None:
        document = _document()
        document["meta"]["schema_version"] = "3"
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
        document = _document()
        document["meta"].pop("schema_version")
        result = self.arm(document)
        self.assertEqual(result.exit_code, 0, result.output)

    def test_a_shape_the_schema_accepts_and_the_codec_refuses_is_still_caught(self) -> None:
        """JSON Schema defines `integer` as any number with zero fractional part,
        so `1.0` passes it; the codec refuses because identity strings are built
        from this field (#1371). The door runs BOTH gates for exactly this."""
        document = _document()
        document["meta"]["generation"] = 1.0
        result = self.arm(document, "--format", "json")
        self.assert_untouched(result, "intent_malformed", "undecodable")

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
        self.assert_refused(self.arm(document, "--format", "json"), "intent_malformed")

    def test_an_explicitly_null_exit_survives(self) -> None:
        """The gate's most plausible false refusal, and it is not one: `asdict`
        never omits a field, so `exit: null` comes back as `exit: null`. Kept as
        a test so nobody "fixes" the gate into refusing a legal document."""
        result = self.arm(_document(exit=None))
        self.assertEqual(result.exit_code, 0, result.output)

    def test_a_document_omitting_every_defaulted_field_survives(self) -> None:
        """Defaults we ADD are not losses — only keys that vanish are."""
        document = _document()
        document.pop("account_id")
        document.pop("exit")
        for key in ("schema_version", "source", "generation"):
            document["meta"].pop(key)
        result = self.arm(document)
        self.assertEqual(result.exit_code, 0, result.output)


class TheDocumentMustAlsoBeCoherentAndTradable(_DoorCase):
    def test_allocations_that_do_not_sum_are_the_validator_s_refusal(self) -> None:
        document = _document()
        document["spec"]["entry_tiers"][0]["alloc_pct"] = 250.0
        failure = self.assert_refused(self.arm(document, "--format", "json"), "intent_invalid")
        self.assertEqual(failure["details"]["reason"], "entry_alloc_sum")
        self.assertEqual(self.inbox_bytes(), b"")

    def test_a_levered_size_is_refused(self) -> None:
        document = _document()
        document["spec"]["suggested_size_pct"] = 180.0
        self.assert_refused(self.arm(document, "--format", "json"), "intent_invalid")

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


class ThePickKeyMustBeWritable(_DoorCase):
    """The table the first draft of this ticket got wrong."""

    def _arm_once(self) -> dict:
        document = _document()
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
        changed = _document()
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

    def test_a_second_live_generation_on_one_instrument_is_refused(self) -> None:
        self._arm_once()
        second = _document()
        second["meta"]["generation"] = 2
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
        second = _document()
        second["meta"]["generation"] = 2

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


if __name__ == "__main__":
    unittest.main()
