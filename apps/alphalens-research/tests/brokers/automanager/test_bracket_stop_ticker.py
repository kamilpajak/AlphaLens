"""#1621: a BRACKET pick's stop pages name the ticker, not the uic.

An ``entry_mode: "immediate"`` pick enters through the bracket path, so its
protective stop's reference is ``<bracket crid>-stop-<n>`` with a UUID crid. The
entry-trail parser cannot read a ticker out of that, and before this fix every
such page went out as ``uic <n>``: measured on LIVE in the week of 2026-11-02,
three stop fills out of three (NVDA, AVGO, AMAT) paged without a ticker.

The second source is ``submissions.jsonl``: the record whose ``brackets`` list
holds that crid carries the ticker and the uic. The join is used only when it is
EXACT -- one bracket in one record, on the same uic -- and every other shape
falls back to the ``uic <n>`` page the operator got before.

The fixture is the real SIM journal of RHI:2026-09-03 (``trades_fixture``): one
bracket, crid ``d1b6f68d-e98c-4a65-a541-74eac07b655d``, uic ``'641'`` stored as a
STRING, closed by the stop ``d1b6f68d-...-stop-0`` (order 5040004072, 1796 @
40.46). Every expected page below is written out literally.
"""

from __future__ import annotations

import ast
import inspect
import json
import shutil
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import alphalens_pipeline.brokers.automanager.control_loop as cl
from alphalens_pipeline.brokers import submission_log
from alphalens_pipeline.brokers.automanager import state_paths, trades
from alphalens_pipeline.brokers.automanager import stop_journal as sj
from alphalens_pipeline.brokers.automanager.position_manager import PlaceStop
from alphalens_pipeline.brokers.automanager.trades import TradesFilters, build_trades

from tests.brokers.automanager.home_isolation import (
    IsolatedHomeTestCase,
    OperatorStateReadError,
)
from tests.brokers.automanager.test_stop_fill_reconcile import _deps, _filled, _StopBroker
from tests.brokers.automanager.trades_fixture import NOW, SIM_RHI_DIR, install_journals

_RHI_UIC = 641
_RHI_CRID = "d1b6f68d-e98c-4a65-a541-74eac07b655d"
_RHI_STOP_REF = f"{_RHI_CRID}-stop-0"
_RHI_ORDER = "5040004072"
# The real stop_placed line of that stop, verbatim from the fixture.
_RHI_STOP_PLACED = {
    "kind": "stop_placed",
    "order_id": _RHI_ORDER,
    "qty": 701.0,
    "ref": _RHI_STOP_REF,
    "ts": 1788442349.0392632,
    "uic": _RHI_UIC,
}

_FILL_PAGE_NAMED = "SELL RHI SL 1796 @ 40.46 - plan stop - position closed (order 5040004072)"
_FILL_PAGE_FALLBACK = (
    "SELL uic 641 SL 1796 @ 40.46 - plan stop - position closed (order 5040004072)"
)
_PLACED_PAGE_NAMED = "PROTECTED RHI SL 701 - stop placed 40.50 (order 5040004072)"
_PLACED_PAGE_FALLBACK = "PROTECTED uic 641 SL 701 - stop placed 40.50 (order 5040004072)"


def _rhi_submissions() -> list[dict[str, Any]]:
    path = SIM_RHI_DIR / "submissions.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _bracket_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    (record,) = [r for r in records if r.get("brackets")]
    return record


class _BracketStopCase(IsolatedHomeTestCase):
    """Writes the journals under the isolated home, where the daemon reads them."""

    def _write_submissions(self, records: list[Any], *, raw_tail: bytes = b"") -> Path:
        path = state_paths.submissions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(json.dumps(record) + "\n" for record in records).encode("utf-8")
        path.write_bytes(body + raw_tail)
        return path

    def _install_real_submissions(self) -> Path:
        path = state_paths.submissions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SIM_RHI_DIR / "submissions.jsonl", path)
        return path

    def _fill_page(self) -> str:
        stops = sj._standalone_stop_journal_path()
        stops.parent.mkdir(parents=True, exist_ok=True)
        stops.write_text(json.dumps(_RHI_STOP_PLACED) + "\n", encoding="utf-8")
        alerts: list[str] = []
        broker = _StopBroker(outcome=_filled(_RHI_ORDER, 1796.0, 40.46))
        cl._run_stop_fill_reconcile_pass(_deps(broker, alerts), cl.TickReport())
        self.assertEqual(len(alerts), 1, alerts)
        return alerts[0]

    def _placed_page(self) -> str:
        sent: list[str] = []
        throttle = cl._AlertThrottle(sent.append)
        action = PlaceStop(_RHI_UIC, "SELL", 701.0, 40.5, _RHI_STOP_REF)
        self.assertTrue(cl._send_stop_placed_page(throttle, action, 701.0, _RHI_ORDER))
        self.assertEqual(len(sent), 1, sent)
        return sent[0]

    def _both_pages(self) -> tuple[str, str]:
        return self._fill_page(), self._placed_page()


class TestTheRealBracketNamesItsTicker(_BracketStopCase):
    def test_the_stop_fill_page_names_RHI(self) -> None:
        self._install_real_submissions()
        self.assertEqual(self._fill_page(), _FILL_PAGE_NAMED)

    def test_the_stop_placed_page_names_RHI(self) -> None:
        self._install_real_submissions()
        self.assertEqual(self._placed_page(), _PLACED_PAGE_NAMED)

    def test_an_int_uic_on_the_record_joins_too(self) -> None:
        # Everywhere but submissions the uic is an int; a record written that
        # way must join as well as the string form the real file carries.
        records = _rhi_submissions()
        _bracket_record(records)["uic"] = _RHI_UIC
        self._write_submissions(records)
        self.assertEqual(self._both_pages(), (_FILL_PAGE_NAMED, _PLACED_PAGE_NAMED))

    def test_a_half_written_last_line_does_not_hide_the_record(self) -> None:
        # A crash mid-append leaves a torn line; the canonical reader skips it.
        self._write_submissions(_rhi_submissions(), raw_tail=b'{"ticker": "RHI", "bra')
        self.assertEqual(self._both_pages(), (_FILL_PAGE_NAMED, _PLACED_PAGE_NAMED))


class TestEveryInexactJoinFallsBackToTheUic(_BracketStopCase):
    def test_no_submissions_file(self) -> None:
        self.assertEqual(self._both_pages(), (_FILL_PAGE_FALLBACK, _PLACED_PAGE_FALLBACK))

    def test_a_non_utf8_submissions_file(self) -> None:
        self._write_submissions(_rhi_submissions(), raw_tail=b"\xff\xfe\n")
        with self.assertLogs(cl.logger, "WARNING") as logged:
            pages = self._both_pages()
        self.assertEqual(pages, (_FILL_PAGE_FALLBACK, _PLACED_PAGE_FALLBACK))
        self.assertIn("submissions unreadable -- naming uic 641", logged.output[0])

    def test_an_unreadable_submissions_file(self) -> None:
        path = self._install_real_submissions()
        with mock.patch.object(
            submission_log, "iter_submission_records", side_effect=PermissionError(str(path))
        ):
            with self.assertLogs(cl.logger, "WARNING"):
                pages = self._both_pages()
        self.assertEqual(pages, (_FILL_PAGE_FALLBACK, _PLACED_PAGE_FALLBACK))

    def test_no_bracket_carries_the_crid(self) -> None:
        records = _rhi_submissions()
        _bracket_record(records)["brackets"][0]["client_request_id"] = "another-crid"
        self._write_submissions(records)
        self.assertEqual(self._both_pages(), (_FILL_PAGE_FALLBACK, _PLACED_PAGE_FALLBACK))

    def test_a_crid_that_only_extends_the_ref_crid_does_not_join(self) -> None:
        # The join is EQUALITY: a bracket whose crid merely starts with (or
        # contains) the ref's crid is another bracket.
        records = _rhi_submissions()
        _bracket_record(records)["brackets"][0]["client_request_id"] = f"{_RHI_CRID}0"
        self._write_submissions(records)
        self.assertEqual(self._both_pages(), (_FILL_PAGE_FALLBACK, _PLACED_PAGE_FALLBACK))

    def test_a_reader_that_raises_anything_falls_back(self) -> None:
        # A line nested deep enough makes ``json.loads`` raise RecursionError,
        # which is neither an OSError nor a ValueError. The fill page is sent
        # after ``stop_filled`` is journaled, so a raise here would lose it.
        self._install_real_submissions()
        with mock.patch.object(
            submission_log, "iter_submission_records", side_effect=RecursionError("deep")
        ):
            with self.assertLogs(cl.logger, "WARNING") as logged:
                pages = self._both_pages()
        self.assertEqual(pages, (_FILL_PAGE_FALLBACK, _PLACED_PAGE_FALLBACK))
        self.assertIn("submissions unreadable -- naming uic 641", logged.output[0])

    def test_two_records_carry_the_crid(self) -> None:
        # Never pick one of two: the second names another ticker on purpose,
        # so a page that picked either would be a guess.
        records = _rhi_submissions()
        twin = json.loads(json.dumps(_bracket_record(records)))
        twin["ticker"] = "ZZZ"
        self._write_submissions([*records, twin])
        self.assertEqual(self._both_pages(), (_FILL_PAGE_FALLBACK, _PLACED_PAGE_FALLBACK))

    def test_one_record_carries_the_crid_twice(self) -> None:
        records = _rhi_submissions()
        bracket = _bracket_record(records)
        bracket["brackets"] = [*bracket["brackets"], dict(bracket["brackets"][0])]
        self._write_submissions(records)
        self.assertEqual(self._both_pages(), (_FILL_PAGE_FALLBACK, _PLACED_PAGE_FALLBACK))

    def test_the_record_names_another_uic(self) -> None:
        records = _rhi_submissions()
        _bracket_record(records)["uic"] = "642"
        self._write_submissions(records)
        self.assertEqual(self._both_pages(), (_FILL_PAGE_FALLBACK, _PLACED_PAGE_FALLBACK))

    def test_the_record_uic_is_not_a_number(self) -> None:
        records = _rhi_submissions()
        _bracket_record(records)["uic"] = "641a"
        self._write_submissions(records)
        self.assertEqual(self._both_pages(), (_FILL_PAGE_FALLBACK, _PLACED_PAGE_FALLBACK))

    def test_the_record_ticker_is_not_a_string(self) -> None:
        records = _rhi_submissions()
        _bracket_record(records)["ticker"] = 641
        self._write_submissions(records)
        self.assertEqual(self._both_pages(), (_FILL_PAGE_FALLBACK, _PLACED_PAGE_FALLBACK))

    def test_a_bool_uic_is_no_uic(self) -> None:
        # ``True`` is an int to Python. As a uic it is a corrupt record, and a
        # stop on uic 1 must not join it.
        records = _rhi_submissions()
        _bracket_record(records)["uic"] = True
        self.assertIsNone(sj._bracket_ticker(_RHI_STOP_REF, 1, records))

    def test_a_float_uic_is_no_uic(self) -> None:
        # The writer stores a string; a float is not a shape it produces.
        records = _rhi_submissions()
        _bracket_record(records)["uic"] = 641.0
        self.assertIsNone(sj._bracket_ticker(_RHI_STOP_REF, _RHI_UIC, records))

    def test_a_record_whose_brackets_is_not_a_list(self) -> None:
        # A number cannot be iterated at all; a dict would iterate as its keys
        # and a string as its characters, neither of them a bracket.
        for brackets in (7, {"client_request_id": _RHI_CRID}, _RHI_CRID):
            with self.subTest(brackets=brackets):
                records = _rhi_submissions()
                _bracket_record(records)["brackets"] = brackets
                self._write_submissions(records)
                self.assertEqual(self._both_pages(), (_FILL_PAGE_FALLBACK, _PLACED_PAGE_FALLBACK))

    def test_a_bracket_entry_that_is_not_an_object_is_skipped(self) -> None:
        # The good bracket still joins: one bad entry must not hide the rest.
        records = _rhi_submissions()
        bracket = _bracket_record(records)
        bracket["brackets"] = [_RHI_CRID, *bracket["brackets"]]
        self._write_submissions(records)
        self.assertEqual(self._both_pages(), (_FILL_PAGE_NAMED, _PLACED_PAGE_NAMED))


class TestTheReadStaysOffTheProtectivePath(_BracketStopCase):
    def test_queueing_the_stop_placed_page_reads_nothing(self) -> None:
        # ``_page_stop_placed`` runs inside the serial per-action protection
        # loop, where nothing but a list append may sit. The journal read must
        # happen in the deferred send, i.e. at the drain.
        self._install_real_submissions()
        sent: list[str] = []
        throttle = cl._AlertThrottle(sent.append)
        action = PlaceStop(_RHI_UIC, "SELL", 701.0, 40.5, _RHI_STOP_REF)
        report = cl.TickReport()
        reader = submission_log.iter_submission_records
        with mock.patch.object(
            submission_log, "iter_submission_records", side_effect=reader
        ) as spy:
            cl._page_stop_placed(throttle, action, 701.0, _RHI_ORDER, report)
            self.assertEqual(spy.call_count, 0, "read inside the protection loop")
            cl._drain_pages(report)
            self.assertEqual(spy.call_count, 1)
        self.assertEqual(sent, [_PLACED_PAGE_NAMED])

    def test_an_entry_trail_ref_never_reads_the_submissions(self) -> None:
        sent: list[str] = []
        throttle = cl._AlertThrottle(sent.append)
        action = PlaceStop(_RHI_UIC, "SELL", 7.0, 50.64, "ASTS-2026-09-23-entry-t0-fire-stop-0")
        with mock.patch.object(submission_log, "iter_submission_records") as spy:
            cl._send_stop_placed_page(throttle, action, 7.0, "5446399206")
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(sent, ["PROTECTED ASTS SL 7 - stop placed 50.64 (order 5446399206)"])


def _resize_rhi_stop(journal: str, record: dict[str, Any]) -> dict[str, Any]:
    """The RHI stop as it reads after one resize: the same bracket, gen 1."""
    if journal == "standalone_stops" and record.get("ref") == _RHI_STOP_REF:
        return {**record, "ref": f"{_RHI_CRID}-stop-1"}
    return record


class TestThePageAndTheCommandAgree(_BracketStopCase):
    """The same real RHI journals, read by ``alphalens broker trades`` and by the
    stop-fill page, name the same pick for the same stop."""

    def test_a_resized_bracket_stop_is_rhis_in_both(self) -> None:
        install_journals(self.home, env="sim", source_dir=SIM_RHI_DIR, edit=_resize_rhi_stop)
        report = build_trades(
            "sim", broker=None, filters=TradesFilters(pick=None, limit=None), now=NOW
        )
        owners = [
            (record["pick_key"], exit_["attribution"])
            for record in report.trades
            for exit_ in record["exits"]
            if exit_["order_id"] == _RHI_ORDER
        ]
        self.assertEqual(owners, [("RHI:2026-09-03", "external_reference")])
        stops = sj._standalone_stop_journal_path()
        stops.write_text(
            json.dumps({**_RHI_STOP_PLACED, "ref": f"{_RHI_CRID}-stop-1"}) + "\n",
            encoding="utf-8",
        )
        alerts: list[str] = []
        broker = _StopBroker(outcome=_filled(_RHI_ORDER, 1796.0, 40.46))
        cl._run_stop_fill_reconcile_pass(_deps(broker, alerts), cl.TickReport())
        self.assertEqual(
            alerts, ["SELL RHI SL 1796 @ 40.46 - plan stop - position closed (order 5040004072)"]
        )


class TestTheBroadCatchCoversOnlyTheRead(unittest.TestCase):
    """Deliberately NOT an :class:`IsolatedHomeTestCase`. The broad catch
    around the submissions read must not also swallow the path resolution: here
    that resolution raises the suite's operator-state guard, and a catch that
    covered it would turn a test reading the real home into a silent fallback."""

    def test_a_failing_path_resolution_is_not_swallowed(self) -> None:
        with self.assertRaises(OperatorStateReadError):
            cl._stop_page_ticker(_RHI_STOP_REF, _RHI_UIC)


class TestOneRuleForWhoOwnsAStop(unittest.TestCase):
    """The page and ``alphalens broker trades`` answer the same question --
    which bracket does this stop belong to -- so they share one rule."""

    def test_trades_compiles_no_stop_ref_pattern_of_its_own(self) -> None:
        # Read off the SOURCE, not by identity: ``re.compile`` caches, so a
        # local copy of the same pattern string is the very same object and an
        # ``assertIs`` would pass on the duplicate it is meant to forbid. Any
        # ``-stop-`` pattern compiled in trades.py is a second rule, whatever
        # its name, annotated or not, module-level or inline.
        tree = ast.parse(inspect.getsource(trades))
        own = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) in {"re.compile", "compile"}
            and any(
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and "-stop-" in arg.value
                for arg in node.args
            )
        ]
        self.assertEqual(own, [])

    def test_a_bracket_ref_names_its_request(self) -> None:
        # Every resize bumps the gen (``position_manager._exit_stop_ref``), so
        # a gen above 0 is a normal shape.
        for gen in ("0", "1", "12"):
            with self.subTest(gen=gen):
                ref = f"{_RHI_CRID}-stop-{gen}"
                self.assertEqual(sj._bracket_request_from_stop_ref(ref), _RHI_CRID)

    def test_an_entry_trail_ref_is_not_a_bracket_ref(self) -> None:
        # It ALSO matches ``^.+-stop-\\d+$``, so the entry-trail shape is tried
        # first; a malformed date must not turn it into a bracket crid either.
        for ref in ("ASTS-2026-09-23-entry-t0-fire-stop-0", "ASTS-2026-13-45-entry-t0-stop-1"):
            with self.subTest(ref):
                self.assertIsNone(sj._bracket_request_from_stop_ref(ref))

    def test_other_shapes_name_no_request(self) -> None:
        for ref in (None, "", "-stop-0", f"{_RHI_CRID}-oco-0-stop", f"{_RHI_CRID}-stop-x"):
            with self.subTest(ref):
                self.assertIsNone(sj._bracket_request_from_stop_ref(ref))


if __name__ == "__main__":
    unittest.main()
