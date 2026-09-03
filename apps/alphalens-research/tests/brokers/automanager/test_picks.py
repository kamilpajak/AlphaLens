"""Hermetic tests for the append-only pick queue.

PR-7 (memo section 5): ``arm_pick`` persists a FULL ``TradeIntent`` per
armed line; ``iter_picks`` decodes it back. Mirrors submission_log.py: one
JSON line per arm, file never rewritten, malformed/undated lines skipped not
fatal, missing file yields nothing. No back-compat for old bare
(ticker, date) lines (solo-project doctrine — re-arm).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alphalens_pipeline.brokers.automanager.picks import (
    STATUS_ARMED,
    STATUS_DISARMED,
    STATUS_REFUSED,
    arm_pick,
    iter_picks,
    mark_disarmed,
    mark_refused,
    pick_key,
    read_pick_fold,
    submitted_pick_keys,
)
from broker_contract.trade_intent.schema import (
    EntryTierSpec,
    ExitGeometrySpec,
    InitialLevels,
    InstrumentHint,
    IntentMeta,
    ReanchorOnFill,
    TpTrancheSpec,
    TradeIntent,
    TradeSpec,
)


def _intent(ticker: str = "KO", brief_date: str = "2026-07-20") -> TradeIntent:
    spec = TradeSpec(
        entry_tiers=(EntryTierSpec(limit_price=100.0, alloc_pct=50.0, tag="T1"),),
        disaster_stop=90.0,
        tp_tranches=(TpTrancheSpec(price=110.0, tranche_pct=100.0, r_multiple=2.0, tag="TP1"),),
        suggested_size_pct=2.0,
    )
    exit_spec = ExitGeometrySpec(
        initial_levels=InitialLevels(stop=90.0, tp=110.0),
        reaction_plan=(ReanchorOnFill(k_atr=1.5, atr=2.0),),
    )
    return TradeIntent(
        intent_id=f"{ticker}:{brief_date}",
        instrument=InstrumentHint(ticker=ticker.upper(), mic="XNYS"),
        spec=spec,
        meta=IntentMeta(armed_ts=f"{brief_date}T14:00:00+00:00", brief_date=brief_date),
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

    def test_disarm_scoped_to_its_brief_date(self) -> None:
        # The exact production case: IBRX armed under 2026-08-25 AND 2026-08-26;
        # disarming one date must not retire the other.
        arm_pick(_intent("IBRX", "2026-08-25"), path=self.path)
        arm_pick(_intent("IBRX", "2026-08-26"), path=self.path)
        mark_disarmed("IBRX", dt.date(2026, 8, 26), path=self.path)
        intents = list(iter_picks(path=self.path))
        self.assertEqual(
            [(i.instrument.ticker, i.meta.brief_date) for i in intents],
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
        self.assertEqual(intents[0].meta.brief_date, "2026-07-20")
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

    def test_refusal_scoped_to_its_brief_date(self) -> None:
        # A refusal for one brief date must not retire the same ticker armed for
        # a different brief date — the queue key is (ticker, date).
        arm_pick(_intent("KO", "2026-07-28"), path=self.path)
        arm_pick(_intent("KO", "2026-07-29"), path=self.path)
        mark_refused("KO", dt.date(2026, 7, 28), "portfolio cap exceeded", path=self.path)
        intents = list(iter_picks(path=self.path))
        self.assertEqual(
            [(i.instrument.ticker, i.meta.brief_date) for i in intents], [("KO", "2026-07-29")]
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
                            "suggested_size_pct": 2.0,
                            "order_ttl_days": 7,
                            "side": "long",
                            "schema_version": "1",
                        },
                        "meta": {
                            "armed_ts": "2026-07-20T00:00:00+00:00",
                            "brief_date": "2026-07-20",
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

    def test_brief_date_is_a_date_and_ticker_is_upper(self) -> None:
        arm_pick(_intent("ko", "2026-07-20"), path=self.path)
        record = read_pick_fold(path=self.path).records[0]
        self.assertEqual(record.brief_date, dt.date(2026, 7, 20))
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


class JoinHelperTest(unittest.TestCase):
    """The (ticker, brief_date) join the drain places on.

    Moved out of ``control_loop`` so the CLI view and the drain cannot drift
    apart — a second implementation of this join would be its own defect.
    """

    def test_pick_key_is_upper_ticker_and_string_date(self) -> None:
        self.assertEqual(pick_key(_intent("ko", "2026-07-20")), ("KO", "2026-07-20"))

    def test_submitted_pick_keys_collects_ticker_and_brief_date(self) -> None:
        records = [
            {"ticker": "ko", "brief_date": "2026-07-20"},
            {"ticker": "MU", "brief_date": "2026-07-21"},
        ]
        self.assertEqual(submitted_pick_keys(records), {("KO", "2026-07-20"), ("MU", "2026-07-21")})

    def test_submitted_pick_keys_skips_records_missing_either_half(self) -> None:
        records = [{"ticker": "KO"}, {"brief_date": "2026-07-20"}, {}]
        self.assertEqual(submitted_pick_keys(records), set())

    def test_now_tranche_record_does_not_retire_the_pick(self) -> None:
        # #1247: the now half's records must NOT join — otherwise a placed or
        # refused now tranche would strand the pullback siblings forever.
        records = [{"ticker": "RHI", "brief_date": "2026-09-03", "tranche": "now"}]
        self.assertEqual(submitted_pick_keys(records), set())

    def test_legacy_records_without_tranche_key_join_exactly_as_before(self) -> None:
        records = [
            {"ticker": "KO", "brief_date": "2026-07-20"},
            {"ticker": "RHI", "brief_date": "2026-09-03", "tranche": "now"},
            {"ticker": "RHI", "brief_date": "2026-09-03", "note": "entry-trail watch opened"},
        ]
        self.assertEqual(
            submitted_pick_keys(records), {("KO", "2026-07-20"), ("RHI", "2026-09-03")}
        )


if __name__ == "__main__":
    unittest.main()
