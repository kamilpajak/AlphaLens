"""Hermetic tests for the append-only pick queue.

PR-7 (memo section 5): ``arm_pick`` persists a FULL ``TradeIntent`` per
armed line; ``iter_picks`` decodes it back. Mirrors submission_log.py: one
JSON line per arm, file never rewritten, malformed/undated lines skipped not
fatal, missing file yields nothing. No back-compat for old bare
(ticker, date) lines (solo-project doctrine — re-arm).
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alphalens_pipeline.brokers.automanager import picks as picks_module
from alphalens_pipeline.brokers.automanager.picks import (
    STATUS_ARMED,
    STATUS_DISARMED,
    STATUS_EXPIRED,
    STATUS_REFUSED,
    arm_pick,
    generation_of,
    identity_token,
    iter_picks,
    keys_with_any_submission,
    mark_disarmed,
    mark_expired,
    mark_refused,
    next_generation,
    pick_key,
    pick_key_str,
    read_pick_fold,
    submitted_pick_keys,
)
from broker_contract.trade_intent.schema import (
    EntryTierSpec,
    ExitGeometrySpec,
    InitialLevels,
    InstrumentHint,
    IntentMeta,
    PickSize,
    ReanchorOnFill,
    TpTrancheSpec,
    TradeIntent,
    TradeSpec,
)


def _intent(ticker: str = "KO", trade_date: str = "2026-07-20", generation: int = 1) -> TradeIntent:
    spec = TradeSpec(
        entry_tiers=(EntryTierSpec(limit_price=100.0, alloc_pct=50.0, tag="T1"),),
        disaster_stop=90.0,
        tp_tranches=(TpTrancheSpec(price=110.0, tranche_pct=100.0, r_multiple=2.0, tag="TP1"),),
        size=PickSize(notional_acct=2000.0, currency="USD"),
    )
    exit_spec = ExitGeometrySpec(
        initial_levels=InitialLevels(stop=90.0, tp=110.0),
        reaction_plan=(ReanchorOnFill(k_atr=1.5, atr=2.0),),
    )
    return TradeIntent(
        intent_id=f"{ticker}:{trade_date}",
        instrument=InstrumentHint(ticker=ticker.upper(), mic="XNYS"),
        spec=spec,
        meta=IntentMeta(
            armed_ts=f"{trade_date}T14:00:00+00:00",
            trade_date=trade_date,
            generation=generation,
        ),
        exit=exit_spec,
    )


class ArmPickTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "picks.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_arm_pick_appends_one_armed_line_carrying_the_intent(self) -> None:
        arm_pick(_intent("ko", "2026-07-20"), path=self.path)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["ticker"], "KO")
        self.assertEqual(record["date"], "2026-07-20")
        self.assertEqual(record["status"], STATUS_ARMED)
        self.assertTrue(record["armed_ts"])
        self.assertIn("intent", record)
        self.assertEqual(record["intent"]["instrument"]["ticker"], "KO")
        self.assertEqual(record["intent"]["spec"]["disaster_stop"], 90.0)

    def test_arm_pick_never_rewrites_appends_second_line(self) -> None:
        arm_pick(_intent("KO", "2026-07-20"), path=self.path)
        arm_pick(_intent("MU", "2026-07-21"), path=self.path)
        self.assertEqual(len(self.path.read_text().splitlines()), 2)

    def test_arm_pick_creates_parent_dir(self) -> None:
        nested = Path(self._tmp.name) / "broker_orders" / "picks.jsonl"
        arm_pick(_intent("KO", "2026-07-20"), path=nested)
        self.assertTrue(nested.exists())


class MarkRefusedTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "picks.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_mark_refused_appends_terminal_refused_line(self) -> None:
        arm_pick(_intent("ko", "2026-07-29"), path=self.path)
        mark_refused("ko", dt.date(2026, 7, 29), "portfolio cap exceeded", path=self.path)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2, "append-only: the armed line must stay")
        record = json.loads(lines[1])
        self.assertEqual(record["ticker"], "KO")
        self.assertEqual(record["date"], "2026-07-29")
        self.assertEqual(record["status"], STATUS_REFUSED)
        self.assertEqual(record["reason"], "portfolio cap exceeded")
        parsed_ts = dt.datetime.fromisoformat(record["refused_ts"])
        self.assertIsNotNone(parsed_ts.tzinfo, "refused_ts must be timezone-aware UTC")

    def test_mark_refused_creates_parent_dir(self) -> None:
        nested = Path(self._tmp.name) / "broker_orders" / "picks.jsonl"
        mark_refused("KO", dt.date(2026, 7, 29), "cap", path=nested)
        self.assertTrue(nested.exists())


class MarkExpiredTest(unittest.TestCase):
    """#1734: the daemon's terminal for a pick still unplaced when its window ends."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "picks.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_mark_expired_appends_a_terminal_line_naming_the_window(self) -> None:
        arm_pick(_intent("ko", "2026-09-30"), path=self.path)
        window_end = dt.datetime(2026, 10, 9, 20, 0, tzinfo=dt.UTC)
        mark_expired(
            "ko",
            dt.date(2026, 9, 30),
            window_end=window_end,
            reason="waiting: gross cap",
            path=self.path,
        )
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2, "append-only: the armed line must stay")
        record = json.loads(lines[1])
        self.assertEqual(record["ticker"], "KO")
        self.assertEqual(record["date"], "2026-09-30")
        self.assertEqual(record["status"], STATUS_EXPIRED)
        self.assertEqual(record["window_end"], "2026-10-09T20:00:00+00:00")
        self.assertEqual(record["reason"], "waiting: gross cap")
        self.assertIsNotNone(dt.datetime.fromisoformat(record["expired_ts"]).tzinfo)
        self.assertNotIn("generation", record)

    def test_an_expired_pick_is_never_yielded_to_the_drain(self) -> None:
        arm_pick(_intent("KO", "2026-09-30"), path=self.path)
        arm_pick(_intent("MU", "2026-09-30"), path=self.path)
        mark_expired(
            "KO",
            dt.date(2026, 9, 30),
            window_end=dt.datetime(2026, 10, 9, 20, 0, tzinfo=dt.UTC),
            reason="unplaced at window end",
            path=self.path,
        )
        self.assertEqual([i.instrument.ticker for i in iter_picks(path=self.path)], ["MU"])

    def test_an_expired_line_that_carries_its_document_is_still_not_yielded(self) -> None:
        # The test above passes through the "no intent" branch: mark_expired
        # writes no document, so it cannot tell whether the STATUS filter works.
        # An expired line that does carry one (a hand-edited journal, a future
        # writer) must be skipped by its status alone.
        from broker_contract.trade_intent.codec import intent_to_jsonable

        intent = _intent("KO", "2026-09-30")
        arm_pick(intent, path=self.path)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ticker": "KO",
                        "date": "2026-09-30",
                        "status": STATUS_EXPIRED,
                        "window_end": "2026-10-09T20:00:00+00:00",
                        "reason": "unplaced at window end",
                        "intent": intent_to_jsonable(intent),
                    }
                )
                + "\n"
            )
        self.assertEqual(list(iter_picks(path=self.path)), [])

    def test_the_generation_is_carried_after_a_re_arm(self) -> None:
        arm_pick(_intent("KO", "2026-09-30", generation=2), path=self.path)
        mark_expired(
            "KO",
            dt.date(2026, 9, 30),
            window_end=dt.datetime(2026, 10, 9, 20, 0, tzinfo=dt.UTC),
            reason="unplaced at window end",
            generation=2,
            path=self.path,
        )
        (record,) = read_pick_fold(path=self.path).records
        self.assertEqual((record.status, record.generation), (STATUS_EXPIRED, 2))


class MarkDisarmedTest(unittest.TestCase):
    """`alphalens broker disarm` — the operator terminal, sibling of refused."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "picks.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_mark_disarmed_appends_terminal_disarmed_line(self) -> None:
        arm_pick(_intent("ko", "2026-08-26"), path=self.path)
        mark_disarmed("ko", dt.date(2026, 8, 26), note="duplicate", path=self.path)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2, "append-only: the armed line must stay")
        record = json.loads(lines[1])
        self.assertEqual(record["ticker"], "KO")
        self.assertEqual(record["date"], "2026-08-26")
        self.assertEqual(record["status"], STATUS_DISARMED)
        self.assertEqual(record["note"], "duplicate")
        self.assertNotIn("intent", record)
        parsed_ts = dt.datetime.fromisoformat(record["disarmed_ts"])
        self.assertIsNotNone(parsed_ts.tzinfo, "disarmed_ts must be timezone-aware UTC")

    def test_disarmed_latest_line_retires_the_armed_pick(self) -> None:
        arm_pick(_intent("KO", "2026-08-26"), path=self.path)
        arm_pick(_intent("MU", "2026-08-26"), path=self.path)
        mark_disarmed("KO", dt.date(2026, 8, 26), path=self.path)
        intents = list(iter_picks(path=self.path))
        self.assertEqual([i.instrument.ticker for i in intents], ["MU"])

    def test_rearm_after_disarm_yields_the_pick_again(self) -> None:
        # Queue-side re-arm works (latest wins), mirroring refused. NOTE the
        # entry-trail watch side is stickier: a cancelled crid stays terminal
        # forever (see test_entry_trails_disarm.py), so a re-armed pick for the
        # SAME (ticker, date) will not re-open its watch.
        arm_pick(_intent("KO", "2026-08-26"), path=self.path)
        mark_disarmed("KO", dt.date(2026, 8, 26), path=self.path)
        arm_pick(_intent("KO", "2026-08-26"), path=self.path)
        intents = list(iter_picks(path=self.path))
        self.assertEqual([i.instrument.ticker for i in intents], ["KO"])

    def test_disarm_scoped_to_its_trade_date(self) -> None:
        # The exact production case: IBRX armed under 2026-08-25 AND 2026-08-26;
        # disarming one date must not retire the other.
        arm_pick(_intent("IBRX", "2026-08-25"), path=self.path)
        arm_pick(_intent("IBRX", "2026-08-26"), path=self.path)
        mark_disarmed("IBRX", dt.date(2026, 8, 26), path=self.path)
        intents = list(iter_picks(path=self.path))
        self.assertEqual(
            [(i.instrument.ticker, i.meta.trade_date) for i in intents],
            [("IBRX", "2026-08-25")],
        )


class IterPicksTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "picks.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_iter_missing_file_yields_nothing(self) -> None:
        self.assertEqual(list(iter_picks(path=self.path)), [])

    def test_iter_round_trips_in_append_order(self) -> None:
        arm_pick(_intent("KO", "2026-07-20"), path=self.path)
        arm_pick(_intent("MU", "2026-07-21"), path=self.path)
        intents = list(iter_picks(path=self.path))
        self.assertEqual([i.instrument.ticker for i in intents], ["KO", "MU"])
        self.assertEqual(intents[0].meta.trade_date, "2026-07-20")
        self.assertIsInstance(intents[0], TradeIntent)
        self.assertEqual(intents[0].spec.disaster_stop, 90.0)
        self.assertEqual(intents[0].exit.initial_levels.stop, 90.0)

    def test_iter_yields_only_armed_status_lines(self) -> None:
        # A non-armed status line (cancelled / filled / expired) must NEVER be
        # yielded — the drain places whatever iter_picks emits, so the ARMED
        # filter belongs inside iter_picks (defence in depth against re-placing a
        # retired intent).
        armed = _intent("ARMEDX", "2026-07-20")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        from broker_contract.trade_intent.codec import intent_to_jsonable

        self.path.write_text(
            json.dumps(
                {
                    "ticker": "ARMEDX",
                    "date": "2026-07-20",
                    "armed_ts": "2026-07-20T00:00:00+00:00",
                    "status": "armed",
                    "intent": intent_to_jsonable(armed),
                }
            )
            + "\n"
            + json.dumps(
                {
                    "ticker": "CANCELLEDX",
                    "date": "2026-07-20",
                    "armed_ts": "2026-07-20T00:00:00+00:00",
                    "status": "cancelled",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "ticker": "FILLEDX",
                    "date": "2026-07-21",
                    "armed_ts": "2026-07-21T00:00:00+00:00",
                    "status": "filled",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        intents = list(iter_picks(path=self.path))
        self.assertEqual([i.instrument.ticker for i in intents], ["ARMEDX"])

    def test_refused_latest_line_retires_the_armed_pick(self) -> None:
        # Terminal refusal: the latest status line per (ticker, date) wins, so a
        # refused line appended AFTER the armed line stops the drain from ever
        # retrying the pick (the live 2026-07-30 every-45s retry hazard). Other
        # armed tickers are untouched.
        arm_pick(_intent("KO", "2026-07-29"), path=self.path)
        arm_pick(_intent("MU", "2026-07-29"), path=self.path)
        mark_refused("KO", dt.date(2026, 7, 29), "portfolio cap exceeded", path=self.path)
        intents = list(iter_picks(path=self.path))
        self.assertEqual([i.instrument.ticker for i in intents], ["MU"])

    def test_rearm_after_refusal_yields_the_pick_again(self) -> None:
        # `alphalens broker arm` is the explicit human path back: a NEW armed
        # line after the refusal makes armed the latest status again.
        arm_pick(_intent("KO", "2026-07-29"), path=self.path)
        mark_refused("KO", dt.date(2026, 7, 29), "portfolio cap exceeded", path=self.path)
        arm_pick(_intent("KO", "2026-07-29"), path=self.path)
        intents = list(iter_picks(path=self.path))
        self.assertEqual([i.instrument.ticker for i in intents], ["KO"])

    def test_refusal_scoped_to_its_trade_date(self) -> None:
        # A refusal for one brief date must not retire the same ticker armed for
        # a different brief date — the queue key is (ticker, date).
        arm_pick(_intent("KO", "2026-07-28"), path=self.path)
        arm_pick(_intent("KO", "2026-07-29"), path=self.path)
        mark_refused("KO", dt.date(2026, 7, 28), "portfolio cap exceeded", path=self.path)
        intents = list(iter_picks(path=self.path))
        self.assertEqual(
            [(i.instrument.ticker, i.meta.trade_date) for i in intents], [("KO", "2026-07-29")]
        )

    def test_iter_skips_malformed_and_undated_lines(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            "not json\n"
            + json.dumps(["a", "list"])
            + "\n"
            + json.dumps({"ticker": "NODATE", "status": "armed"})
            + "\n"
            + json.dumps(
                {
                    "ticker": "GOOD",
                    "date": "2026-07-20",
                    "armed_ts": "2026-07-20T00:00:00+00:00",
                    "status": "armed",
                    "intent": {
                        "intent_id": "GOOD:2026-07-20",
                        "instrument": {"ticker": "GOOD", "mic": "XNYS"},
                        "spec": {
                            "entry_tiers": [{"limit_price": 100.0, "alloc_pct": 100.0, "tag": ""}],
                            "disaster_stop": 90.0,
                            "tp_tranches": [],
                            "size": {"notional_acct": 2000.0, "currency": "USD"},
                            "order_ttl_days": 7,
                            "side": "long",
                            "schema_version": "3",
                        },
                        "meta": {
                            "armed_ts": "2026-07-20T00:00:00+00:00",
                            "trade_date": "2026-07-20",
                            "schema_version": "1",
                        },
                        "exit": None,
                        "account_id": "default",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        intents = list(iter_picks(path=self.path))
        self.assertEqual([i.instrument.ticker for i in intents], ["GOOD"])

    def test_armed_line_without_intent_key_is_skipped(self) -> None:
        # The pre-PR-7 bare (ticker, date) line shape — no back-compat per
        # solo-project doctrine (re-arm is the explicit human path back).
        # The skip is logged at DEBUG, never WARNING: the manager daemon
        # re-reads picks.jsonl every ~45s tick, so a WARNING per bare line
        # per tick floods the journal (~28 lines x ~1900 ticks/day) for an
        # expected, inert, self-healing condition. DEBUG keeps it available
        # for troubleshooting without drowning real signal.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "ticker": "LEGACY",
                    "date": "2026-07-20",
                    "armed_ts": "2026-07-20T00:00:00+00:00",
                    "status": "armed",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertLogs(level="DEBUG") as captured:
            intents = list(iter_picks(path=self.path))
        self.assertEqual(intents, [])
        # The skip must be logged so troubleshooting is possible,
        self.assertTrue(
            any("bare shape" in r.getMessage() for r in captured.records),
            "expected the bare-shape skip to be logged",
        )
        # but never at WARNING+ (that is the per-tick journal spam).
        self.assertTrue(
            all(r.levelno < logging.WARNING for r in captured.records),
            "bare-shape skip must not log at WARNING or above",
        )

    def test_armed_line_with_undecodable_intent_is_skipped(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "ticker": "BAD",
                    "date": "2026-07-20",
                    "armed_ts": "2026-07-20T00:00:00+00:00",
                    "status": "armed",
                    "intent": {"instrument": {"ticker": "BAD"}},  # missing required keys
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertLogs(level="WARNING"):
            intents = list(iter_picks(path=self.path))
        self.assertEqual(intents, [])


class ReadPickFoldTest(unittest.TestCase):
    """The fold ``iter_picks`` computes internally, exposed for readers.

    ``iter_picks`` yields only DECODED ARMED intents, so a reader asking
    "what is in this queue" cannot see refused / disarmed rows or their
    reason. This fold is that reader's input; the CLI view joins it against
    the submissions journal exactly like the drain does. Shape mirrors the
    sibling ``entry_trails.read_entry_trail_fold`` — records + a malformed
    count, surfaced rather than silently dropped.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "picks.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file_folds_empty(self) -> None:
        fold = read_pick_fold(path=self.path)
        self.assertEqual(fold.records, [])
        self.assertEqual(fold.malformed, 0)

    def test_latest_status_line_per_key_wins(self) -> None:
        arm_pick(_intent("KO", "2026-07-20"), path=self.path)
        mark_refused("KO", dt.date(2026, 7, 20), "gross cap", path=self.path)
        arm_pick(_intent("KO", "2026-07-20"), path=self.path)
        records = read_pick_fold(path=self.path).records
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, STATUS_ARMED)

    def test_fold_order_matches_iter_picks(self) -> None:
        # Positive control on ordering: the CLI renders rows in this order, so
        # a fold that reordered keys would silently reshuffle the operator view.
        arm_pick(_intent("KO", "2026-07-20"), path=self.path)
        arm_pick(_intent("MU", "2026-07-21"), path=self.path)
        arm_pick(_intent("AMD", "2026-07-22"), path=self.path)
        folded = [r.ticker for r in read_pick_fold(path=self.path).records]
        yielded = [i.instrument.ticker for i in iter_picks(path=self.path)]
        self.assertEqual(folded, ["KO", "MU", "AMD"])
        self.assertEqual(folded, yielded)

    def test_non_armed_rows_are_folded_with_their_detail(self) -> None:
        mark_refused("GME", dt.date(2026, 8, 27), "gross cap: exceeds limit", path=self.path)
        mark_disarmed("SMG", dt.date(2026, 8, 19), note="retired to free it", path=self.path)
        by_ticker = {r.ticker: r for r in read_pick_fold(path=self.path).records}
        self.assertEqual(by_ticker["GME"].status, STATUS_REFUSED)
        self.assertEqual(by_ticker["GME"].record["reason"], "gross cap: exceeds limit")
        self.assertEqual(by_ticker["SMG"].status, STATUS_DISARMED)
        self.assertEqual(by_ticker["SMG"].record["note"], "retired to free it")

    def test_trade_date_is_a_date_and_ticker_is_upper(self) -> None:
        arm_pick(_intent("ko", "2026-07-20"), path=self.path)
        record = read_pick_fold(path=self.path).records[0]
        self.assertEqual(record.trade_date, dt.date(2026, 7, 20))
        self.assertEqual(record.ticker, "KO")

    def test_malformed_lines_are_counted_not_fatal(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("not json\n[]\n", encoding="utf-8")
        arm_pick(_intent("KO", "2026-07-20"), path=self.path)
        fold = read_pick_fold(path=self.path)
        self.assertEqual([r.ticker for r in fold.records], ["KO"])
        self.assertEqual(fold.malformed, 2)

    def test_a_blank_line_is_not_counted_as_malformed(self) -> None:
        # Trailing newlines are normal in an append-only journal; counting them
        # would put a permanent non-zero malformed count in front of the operator.
        arm_pick(_intent("KO", "2026-07-20"), path=self.path)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        self.assertEqual(read_pick_fold(path=self.path).malformed, 0)


class AnUnreadableLineIsCountedAndNeverRaisedOutOfThePickFold(unittest.TestCase):
    """The fold's stated rule is that it REPORTS a line it cannot read (#1788).

    ``ReadPickFoldTest`` above covers the shapes the fold already knew about,
    every one of which arrives at ``_parse_record`` as a ``ValueError``. A line
    that fails EARLIER — while the ``for`` statement advances the file and
    decodes its bytes — never reached that guard at all, so it escaped the
    fold. One such line anywhere in ``picks.jsonl`` then made the WHOLE queue
    unreadable, and this fold is the reader's half of the arming join: the
    door, ``alphalens broker picks`` and the daemon's drain all stop on it.

    One test per member of the named exception set, so the set cannot quietly
    shrink back to the narrower one it was.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "picks.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _append_bytes(self, payload: bytes) -> None:
        with self.path.open("ab") as fh:
            fh.write(payload)

    @staticmethod
    def _torn_utf8_line() -> bytes:
        """A rendered line cut on the LEAD byte of its last multibyte sequence,
        so the bytes end with a sequence that has no continuation byte.

        The non-ASCII character is written as an escape and the cut is made on
        its measured lead byte, so this file stays ASCII and the English-only
        gate (``tests.test_no_polish_chars``) has nothing to find.
        """
        euro_sign = "€"  # U+20AC -> b"\xe2\x82\xac"
        whole = json.dumps(
            {"ticker": "TORN", "date": "2026-07-21", "status": "armed", "note": euro_sign},
            ensure_ascii=False,
        ).encode("utf-8")
        torn = whole[: whole.rindex(b"\xe2") + 1]
        assert torn != whole
        assert torn.endswith(b"\xe2")
        return torn + b"\n"

    def test_a_line_whose_bytes_are_not_utf8_is_counted_not_raised(self) -> None:
        """Member one: ``UnicodeDecodeError``.

        ``arm_pick`` cannot write such a line itself — ``json.dumps`` escapes
        every non-ASCII character by default, and a tear of ASCII bytes yields
        invalid JSON rather than invalid UTF-8. It arrives the way the contract
        README's ``jq`` copy recipe invites: a hand edit, or a restore from a
        copy whose bytes were mangled. The cost is what makes it worth a guard,
        not the odds.
        """
        arm_pick(_intent("KO", "2026-07-20"), path=self.path)
        self._append_bytes(self._torn_utf8_line())

        fold = read_pick_fold(path=self.path)

        self.assertEqual([r.ticker for r in fold.records], ["KO"])
        self.assertEqual(fold.malformed, 1)

    def test_an_undecodable_line_hides_none_of_the_readable_lines_after_it(self) -> None:
        """The cost the issue is about: not the counter, the picks BELOW it.

        A fold that raises reports nothing at all, so one undecodable byte
        loses every pick in the queue rather than one line — and the arming
        door reads this same fold before it lets a document in.
        """
        arm_pick(_intent("AAA", "2026-07-20"), path=self.path)
        self._append_bytes(self._torn_utf8_line())
        arm_pick(_intent("BBB", "2026-07-22"), path=self.path)

        fold = read_pick_fold(path=self.path)

        self.assertEqual([r.ticker for r in fold.records], ["AAA", "BBB"])
        self.assertEqual(fold.malformed, 1)

    def test_a_single_stray_byte_is_counted_the_same_way(self) -> None:
        """A second route to the same member, with no multibyte sequence at
        all: ``0xff`` is not a valid UTF-8 start byte."""
        arm_pick(_intent("KO", "2026-07-20"), path=self.path)
        self._append_bytes(b"\xff\n")

        fold = read_pick_fold(path=self.path)

        self.assertEqual([r.ticker for r in fold.records], ["KO"])
        self.assertEqual(fold.malformed, 1)

    def test_a_line_that_is_not_json_is_still_counted_exactly_as_before(self) -> None:
        """Member two: ``json.JSONDecodeError``. The regression guard — the
        remedy moves the decode, so the shape that already counted must keep
        counting, and keep counting ONCE per line."""
        self.path.write_text("not json\n[]\n", encoding="utf-8")
        arm_pick(_intent("KO", "2026-07-20"), path=self.path)

        fold = read_pick_fold(path=self.path)

        self.assertEqual([r.ticker for r in fold.records], ["KO"])
        self.assertEqual(fold.malformed, 2)

    def test_an_integer_literal_the_interpreter_refuses_is_counted_not_raised(self) -> None:
        """Member three: a plain ``ValueError`` that is NOT a ``JSONDecodeError``.

        CPython refuses to build an ``int`` from a literal longer than
        ``sys.get_int_max_str_digits()`` (4300 by default), and ``json.loads``
        lets that ``ValueError`` through unwrapped. Binary mode alone does not
        close this one — the precedent's guard is widened to ``ValueError``,
        which is what covers both members at once.
        """
        arm_pick(_intent("AAA", "2026-07-20"), path=self.path)
        digits = b"1" * (sys.get_int_max_str_digits() + 1)
        self._append_bytes(b'{"date": "2026-07-21", "generation": ' + digits + b"}\n")
        arm_pick(_intent("BBB", "2026-07-22"), path=self.path)

        fold = read_pick_fold(path=self.path)

        self.assertEqual([r.ticker for r in fold.records], ["AAA", "BBB"])
        self.assertEqual(fold.malformed, 1)

    def test_an_unbounded_json_integer_is_folded_rather_than_overflowing(self) -> None:
        """The issue's open question, answered by measurement: ``OverflowError``
        is NOT reachable here.

        The cash-book precedent needed it because that codec runs every money
        field through ``float(value)``. ``_parse_record`` reads exactly two
        fields and converts neither: ``generation`` is only type-checked, so
        ``10**400`` is a valid ``int`` that folds as an ordinary record. Pinned
        so that a later field which DOES convert cannot be added here without
        this assertion turning red first.
        """
        self._append_bytes(
            json.dumps(
                {"ticker": "KO", "date": "2026-07-20", "status": "armed", "generation": 10**400}
            ).encode("utf-8")
            + b"\n"
        )

        fold = read_pick_fold(path=self.path)

        self.assertEqual(fold.malformed, 0)
        self.assertEqual([r.ticker for r in fold.records], ["KO"])
        self.assertEqual(fold.records[0].generation, 10**400)

    def test_a_line_of_unicode_only_whitespace_stays_skipped_not_malformed(self) -> None:
        """Copying the precedent's binary open verbatim MOVES this count.

        ``str.strip()`` treats U+00A0, U+0085, U+2028 and U+3000 as whitespace;
        ``bytes.strip()`` does not. A reader that tested blankness on the bytes
        would start counting each of these as corruption, and the published
        rule is that a blank line is skipped WITHOUT being counted. So the
        blankness test stays on the decoded string.
        """
        arm_pick(_intent("KO", "2026-07-20"), path=self.path)
        for blank in ("\n", "   \n", "\t\n", "\u00a0\n", "\u0085\n", "\u2028\n", "\u3000\n"):
            with self.subTest(blank=blank.strip("\n").encode("unicode_escape")):
                self._append_bytes(blank.encode("utf-8"))
                self.assertEqual(read_pick_fold(path=self.path).malformed, 0)

    def test_a_bare_carriage_return_still_separates_two_lines(self) -> None:
        """The OTHER way the precedent's open is not behaviour-neutral here.

        Text mode's universal newlines make a lone ``\r`` a line boundary;
        iterating a binary file does not. A journal holding two good records
        separated by one bare ``\r`` folds to two picks today — read as a
        single line it would fold to ONE malformed line and lose BOTH. Neither
        appender writes a bare ``\r``, but it arrives by the same route as the
        undecodable byte does, so the splitting is kept rather than changed.
        """
        self.path.write_bytes(
            json.dumps({"ticker": "AAA", "date": "2026-07-20", "status": "armed"}).encode()
            + b"\r"
            + json.dumps({"ticker": "BBB", "date": "2026-07-21", "status": "armed"}).encode()
            + b"\n"
        )

        fold = read_pick_fold(path=self.path)

        self.assertEqual([r.ticker for r in fold.records], ["AAA", "BBB"])
        self.assertEqual(fold.malformed, 0)

    def test_an_undecodable_line_is_logged_at_debug_and_never_per_tick_warned(self) -> None:
        """The count is the published report, not a log line.

        The manager daemon re-folds this journal every ~45 s tick and the
        journal is append-only, so an undecodable line is permanent: a WARNING
        would repeat for ever on a condition that never heals. The same reason
        ``iter_picks`` already states for a pre-PR-7 bare line. The sibling
        malformed shapes log nothing at all, so a WARNING here would also make
        one of the four routes louder than the other three for no reason a
        reader could act on differently.
        """
        arm_pick(_intent("KO", "2026-07-20"), path=self.path)
        self._append_bytes(self._torn_utf8_line())

        with self.assertLogs(picks_module.__name__, level="DEBUG") as captured:
            self.assertEqual(read_pick_fold(path=self.path).malformed, 1)

        self.assertTrue(any("not UTF-8" in line for line in captured.output))
        self.assertEqual([line for line in captured.output if line.startswith("WARNING")], [])

    def test_the_fold_names_its_unreadable_set_rather_than_blanketing_it(self) -> None:
        """The remedy must not become ``except Exception``.

        A blanket handler would count a real defect in this module's own parser
        as corruption on disk, on the one journal the money path writes to. A
        structural check, because every behavioural test above passes under a
        blanket handler too.
        """
        named = picks_module._UNREADABLE_LINE
        self.assertIn(UnicodeDecodeError, named)
        self.assertIn(json.JSONDecodeError, named)
        self.assertIn(ValueError, named)
        self.assertNotIn(Exception, named)
        self.assertNotIn(BaseException, named)

        source = Path(picks_module.__file__).read_text(encoding="utf-8")
        handlers = [
            node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ExceptHandler)
        ]
        self.assertGreaterEqual(len(handlers), 3, "the except-handler scan found too little")
        for handler in handlers:
            with self.subTest(line=handler.lineno):
                self.assertIsNotNone(
                    handler.type, "a bare `except:` catches even a KeyboardInterrupt"
                )
                names = [node.id for node in ast.walk(handler.type) if isinstance(node, ast.Name)]
                self.assertNotIn("Exception", names)
                self.assertNotIn("BaseException", names)


class JoinHelperTest(unittest.TestCase):
    """The (ticker, trade_date) join the drain places on.

    Moved out of ``control_loop`` so the CLI view and the drain cannot drift
    apart — a second implementation of this join would be its own defect.
    """

    def test_pick_key_is_upper_ticker_and_string_date(self) -> None:
        self.assertEqual(pick_key(_intent("ko", "2026-07-20")), ("KO", "2026-07-20"))

    def test_submitted_pick_keys_collects_ticker_and_trade_date(self) -> None:
        records = [
            {"ticker": "ko", "trade_date": "2026-07-20"},
            {"ticker": "MU", "trade_date": "2026-07-21"},
        ]
        self.assertEqual(submitted_pick_keys(records), {("KO", "2026-07-20"), ("MU", "2026-07-21")})

    def test_submitted_pick_keys_skips_records_missing_either_half(self) -> None:
        records = [{"ticker": "KO"}, {"trade_date": "2026-07-20"}, {}]
        self.assertEqual(submitted_pick_keys(records), set())

    def test_a_torn_line_does_not_swallow_the_next_armed_pick(self) -> None:
        """#1421, and the reason that ticket was rewritten.

        A partial write (ENOSPC) leaves a line with no trailing newline. Before
        the shared append helper the NEXT arm concatenated onto it and both
        records merged into one unparseable line — the CLI printed "armed" and
        exited 0 while the pick never reached the fold, and the malformed
        counter read 1 rather than 2.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "picks.jsonl"
            arm_pick(_intent(ticker="AAA"), path=path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write('{"ticker": "TORN", "date": "2026-09-11", "intent')

            arm_pick(_intent(ticker="BBB"), path=path)

            fold = read_pick_fold(path=path)
            self.assertEqual(sorted(r.ticker for r in fold.records), ["AAA", "BBB"])
            self.assertEqual(fold.malformed, 1)

    def test_any_submission_counts_the_now_half_that_retirement_ignores(self) -> None:
        """The two helpers answer DIFFERENT questions and differ by exactly this.

        `submitted_pick_keys` answers the DRAIN's question — should I place this
        pick? — so the now half must not retire it (#1247). `keys_with_any_submission`
        answers the DOOR's — has anything reached the broker for this key? A pick
        whose now half rests at the broker must not be rewritten (#1406), and
        measured 2026-09-11 the SIM journal holds a key in exactly that state.
        """
        records = [{"ticker": "ko", "trade_date": "2026-07-20", "tranche": "now"}]

        self.assertEqual(submitted_pick_keys(records), set())
        self.assertEqual(keys_with_any_submission(records), {("KO", "2026-07-20")})

    def test_a_terminal_refusal_is_not_an_order(self) -> None:
        """The operator's correction path must stay open.

        `now refused: ask above cap` journals a record with NOTHING at the
        broker. Counting that record as an order would make the door refuse a
        replace of a pick that is still unplaced, and would block the ticker for
        a new document after `disarm` (#1468).
        Measured 2026-09-11: the SIM journal holds one such key (KO @ 2026-09-03)
        and it is the ONLY key whose records are all `tranche: now`.
        """
        refused = [
            {
                "ticker": "KO",
                "trade_date": "2026-09-03",
                "tranche": "now",
                "tranche_meta": {"outcome": "refused_cap"},
            }
        ]

        self.assertEqual(keys_with_any_submission(refused), set())

    def test_an_attempt_counts_because_its_outcome_is_unknown(self) -> None:
        """The write-ahead record exists precisely because the POST's outcome is
        not yet known, so it must count — the safe direction."""
        for outcome in ("attempt", "placed", "some_future_value", None):
            with self.subTest(outcome=outcome):
                meta = {} if outcome is None else {"outcome": outcome}
                records = [
                    {
                        "ticker": "QUBT",
                        "trade_date": "2026-09-03",
                        "tranche": "now",
                        "tranche_meta": meta,
                    }
                ]
                self.assertEqual(
                    keys_with_any_submission(records), {("QUBT", "2026-09-03")}, outcome
                )

    def test_any_submission_keys_the_same_way_as_retirement_otherwise(self) -> None:
        """Positive control: the only difference is the now half. A generation
        and the legacy date key must key identically through both."""
        records = [
            {"ticker": "MU", "trade_date": "2026-07-21", "generation": 2},
            {"ticker": "KO", "brief_date": "2026-07-20"},
        ]

        self.assertEqual(keys_with_any_submission(records), submitted_pick_keys(records))
        self.assertEqual(
            keys_with_any_submission(records), {("MU", "2026-07-21-g2"), ("KO", "2026-07-20")}
        )

    def test_now_tranche_record_does_not_retire_the_pick(self) -> None:
        # #1247: the now half's records must NOT join — otherwise a placed or
        # refused now tranche would strand the pullback siblings forever.
        records = [{"ticker": "RHI", "trade_date": "2026-09-03", "tranche": "now"}]
        self.assertEqual(submitted_pick_keys(records), set())

    def test_legacy_records_without_tranche_key_join_exactly_as_before(self) -> None:
        records = [
            {"ticker": "KO", "trade_date": "2026-07-20"},
            {"ticker": "RHI", "trade_date": "2026-09-03", "tranche": "now"},
            {"ticker": "RHI", "trade_date": "2026-09-03", "note": "entry-trail watch opened"},
        ]
        self.assertEqual(
            submitted_pick_keys(records), {("KO", "2026-07-20"), ("RHI", "2026-09-03")}
        )

    def test_submitted_pick_keys_folds_legacy_brief_date_key(self) -> None:
        # #1252: the journal date key was renamed brief_date -> trade_date, but
        # data on disk written before the rename still carries the old key
        # (append-only journals are never rewritten).
        records = [{"ticker": "KO", "brief_date": "2026-07-20"}]
        self.assertEqual(submitted_pick_keys(records), {("KO", "2026-07-20")})


class IdentityHelperTest(unittest.TestCase):
    """#1371: the ONE place the (ticker, trade_date, generation) identity is
    rendered. Generation 1 renders exactly the pre-#1371 strings, so every
    journal line written before the field existed keeps its identity."""

    def test_generation_one_token_is_the_bare_date(self) -> None:
        self.assertEqual(identity_token("2026-09-08", 1), "2026-09-08")

    def test_later_generations_suffix_the_date(self) -> None:
        self.assertEqual(identity_token("2026-09-08", 2), "2026-09-08-g2")
        self.assertEqual(identity_token("2026-09-08", 12), "2026-09-08-g12")

    def test_pick_key_str_is_upper_ticker_colon_token(self) -> None:
        self.assertEqual(pick_key_str("enph", "2026-09-08", 1), "ENPH:2026-09-08")
        self.assertEqual(pick_key_str("enph", "2026-09-08", 2), "ENPH:2026-09-08-g2")

    def test_generation_below_one_is_rejected(self) -> None:
        for bad in (0, -1):
            with self.subTest(generation=bad):
                with self.assertRaises(ValueError):
                    identity_token("2026-09-08", bad)

    def test_generation_of_reads_absent_or_null_as_one(self) -> None:
        self.assertEqual(generation_of({}), 1)
        self.assertEqual(generation_of({"generation": None}), 1)
        self.assertEqual(generation_of({"generation": 3}), 3)

    def test_generation_of_rejects_non_int_or_non_positive(self) -> None:
        for bad in ("2", 0, True, 2.0):
            with self.subTest(generation=bad):
                with self.assertRaises(ValueError):
                    generation_of({"generation": bad})


class GenerationQueueTest(unittest.TestCase):
    """#1371: a same-day re-arm is a NEW pick, distinguishable from the one it
    follows in every reader of the queue."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "picks.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_generation_one_armed_line_carries_no_generation_key(self) -> None:
        # Positive control on the journal shape: today's lines stay as they are.
        arm_pick(_intent("KO", "2026-09-08"), path=self.path)
        record = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertNotIn("generation", record)

    def test_later_generation_armed_line_carries_the_key(self) -> None:
        arm_pick(_intent("KO", "2026-09-08", generation=2), path=self.path)
        record = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["generation"], 2)

    def test_fold_keeps_each_generation_as_its_own_record(self) -> None:
        arm_pick(_intent("KO", "2026-09-08"), path=self.path)
        mark_disarmed("KO", dt.date(2026, 9, 8), note="wrong geometry", path=self.path)
        arm_pick(_intent("KO", "2026-09-08", generation=2), path=self.path)
        records = read_pick_fold(path=self.path).records
        self.assertEqual(
            [(r.ticker, r.trade_date.isoformat(), r.generation, r.status) for r in records],
            [("KO", "2026-09-08", 1, STATUS_DISARMED), ("KO", "2026-09-08", 2, STATUS_ARMED)],
        )

    def test_iter_picks_yields_only_the_live_generation(self) -> None:
        arm_pick(_intent("KO", "2026-09-08"), path=self.path)
        mark_disarmed("KO", dt.date(2026, 9, 8), note="wrong geometry", path=self.path)
        arm_pick(_intent("KO", "2026-09-08", generation=2), path=self.path)
        intents = list(iter_picks(path=self.path))
        self.assertEqual([i.meta.generation for i in intents], [2])

    def test_terminal_lines_are_scoped_to_their_generation(self) -> None:
        arm_pick(_intent("KO", "2026-09-08"), path=self.path)
        arm_pick(_intent("KO", "2026-09-08", generation=2), path=self.path)
        mark_refused("KO", dt.date(2026, 9, 8), "gross cap", generation=2, path=self.path)
        mark_disarmed("KO", dt.date(2026, 9, 8), note="x", generation=2, path=self.path)
        live = [(i.meta.generation) for i in iter_picks(path=self.path)]
        self.assertEqual(live, [1])
        lines = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([line.get("generation") for line in lines[2:]], [2, 2])

    def test_pick_key_renders_the_generation_token(self) -> None:
        self.assertEqual(
            pick_key(_intent("ko", "2026-09-08", generation=2)), ("KO", "2026-09-08-g2")
        )
        self.assertEqual(pick_key(_intent("ko", "2026-09-08")), ("KO", "2026-09-08"))

    def test_submitted_pick_keys_separate_generations(self) -> None:
        records = [
            {"ticker": "KO", "trade_date": "2026-09-08"},
            {"ticker": "KO", "trade_date": "2026-09-08", "generation": 2},
        ]
        self.assertEqual(
            submitted_pick_keys(records), {("KO", "2026-09-08"), ("KO", "2026-09-08-g2")}
        )

    def test_submitted_pick_keys_folds_a_malformed_generation_to_one(self) -> None:
        # Conservative: a record that exists but carries garbage in the field
        # still proves generation 1 was submitted — never under-count the join.
        records = [{"ticker": "KO", "trade_date": "2026-09-08", "generation": "2"}]
        self.assertEqual(submitted_pick_keys(records), {("KO", "2026-09-08")})

    def test_malformed_generation_fold_is_logged_at_debug(self) -> None:
        # The asymmetry with the queue fold (which drops the line) is deliberate;
        # it must be visible on demand, and never a per-tick WARNING flood.
        records = [{"ticker": "KO", "trade_date": "2026-09-08", "generation": "2"}]
        with self.assertLogs("alphalens_pipeline.brokers.automanager.picks", level="DEBUG") as cm:
            submitted_pick_keys(records)
        self.assertTrue(any("malformed generation" in line for line in cm.output))
        self.assertFalse(any(line.startswith("WARNING") for line in cm.output))

    def test_queue_line_with_malformed_generation_is_malformed(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"ticker": "KO", "date": "2026-09-08", "status": "armed", "generation": "2"})
            + "\n",
            encoding="utf-8",
        )
        fold = read_pick_fold(path=self.path)
        self.assertEqual(fold.records, [])
        self.assertEqual(fold.malformed, 1)

    def test_next_generation_counts_every_line_of_the_key(self) -> None:
        day = dt.date(2026, 9, 8)
        self.assertEqual(next_generation("KO", day, path=self.path), 1)
        arm_pick(_intent("KO", "2026-09-08"), path=self.path)
        self.assertEqual(next_generation("ko", day, path=self.path), 2)
        mark_disarmed("KO", day, note="x", path=self.path)
        self.assertEqual(next_generation("KO", day, path=self.path), 2)
        arm_pick(_intent("KO", "2026-09-08", generation=2), path=self.path)
        self.assertEqual(next_generation("KO", day, path=self.path), 3)

    def test_next_generation_is_scoped_to_ticker_and_date(self) -> None:
        arm_pick(_intent("KO", "2026-09-08", generation=2), path=self.path)
        self.assertEqual(next_generation("KO", dt.date(2026, 9, 9), path=self.path), 1)
        self.assertEqual(next_generation("MU", dt.date(2026, 9, 8), path=self.path), 1)


if __name__ == "__main__":
    unittest.main()
