"""CLI tests for ``alphalens broker watches`` (#1376).

A READ-ONLY view of the entry-trail journal fold: one row per NON-terminal
tier by default (``--all`` adds the terminal ones), the ``stage`` mirroring the
daemon's own sets, the reservation column valued by the SAME function the
money gates use. No broker, no auth, no mutation. Repo CLI doctrine: stdout =
result only, JSON mode = exactly one JSON value, errors to stderr + non-zero
exit + empty stdout. Lazy-import doctrine: the journal path is resolved through
``state_paths`` at call time, so ``Path.home()`` isolation is enough.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from tests.brokers.automanager.test_arm_cli import _isolate_home, _seed_legacy_flat_state

_WINDOW_END = "2026-09-10T20:00:00+00:00"


def _watch_open(crid: str, pick_key: str, ticker: str, tier_index: int, **extra: object) -> dict:
    record: dict = {
        "kind": "watch_open",
        "crid": crid,
        "pick_key": pick_key,
        "ticker": ticker,
        "tier_index": tier_index,
        "uic": 211,
        "exchange_mic": "XNYS",
        "instrument_currency": "USD",
        "window_end": _WINDOW_END,
        "d_bps": 50,
        "fx_rate": None,
    }
    record.update(extra)
    return record


# Every stage the fold can produce, in the shape the REAL (compacted) journal
# has: no `touched` line survives compaction, a `trough` marks the touch.
_JOURNAL: list[object] = [
    # AAA E1: open
    _watch_open("AAA-2026-09-01-entry-t0", "AAA:2026-09-01", "AAA", 0, limit=10.0, qty=5),
    # AAA E2: touched (trough after the touch)
    _watch_open("AAA-2026-09-01-entry-t1", "AAA:2026-09-01", "AAA", 1, limit=9.0, qty=5),
    {"kind": "trough", "crid": "AAA-2026-09-01-entry-t1", "trough": 8.8},
    # BBB E1: resting native arm (fx-converted reservation)
    _watch_open(
        "BBB-2026-09-02-entry-t0", "BBB:2026-09-02", "BBB", 0, limit=20.0, qty=2, fx_rate=4.0
    ),
    {"kind": "trough", "crid": "BBB-2026-09-02-entry-t0", "trough": 19.0},
    {
        "kind": "trail_armed",
        "crid": "BBB-2026-09-02-entry-t0",
        "order_id": "O-1",
        "trigger": 19.2,
        "ceiling": 19.3,
    },
    # BBB E2: arm in progress (null-id write-ahead)
    _watch_open(
        "BBB-2026-09-02-entry-t1", "BBB:2026-09-02", "BBB", 1, limit=18.0, qty=2, fx_rate=4.0
    ),
    {"kind": "trail_armed", "crid": "BBB-2026-09-02-entry-t1", "order_id": None, "trigger": 17.5},
    # CCC E1: a trough AFTER an arm — back under the watch pass, stale id kept
    _watch_open("CCC-2026-09-03-entry-t0", "CCC:2026-09-03", "CCC", 0, limit=5.0, qty=10),
    {"kind": "trail_armed", "crid": "CCC-2026-09-03-entry-t0", "order_id": "O-2", "trigger": 4.95},
    {"kind": "trough", "crid": "CCC-2026-09-03-entry-t0", "trough": 4.9},
    # DDD: fired + expired (terminal)
    _watch_open("DDD-2026-09-04-entry-t0", "DDD:2026-09-04", "DDD", 0, limit=7.0, qty=3),
    {"kind": "trail_armed", "crid": "DDD-2026-09-04-entry-t0", "order_id": "O-3", "trigger": 6.9},
    {
        "kind": "fired",
        "crid": "DDD-2026-09-04-entry-t0",
        "order_id": "O-3",
        "realized_qty": 3,
        "avg_price": 42.5,
    },
    _watch_open("DDD-2026-09-04-entry-t1", "DDD:2026-09-04", "DDD", 1, limit=6.0, qty=3),
    {"kind": "expired", "crid": "DDD-2026-09-04-entry-t1"},
    # EEE: suspended + cancelled (terminal)
    _watch_open("EEE-2026-09-05-entry-t0", "EEE:2026-09-05", "EEE", 0, limit=3.0, qty=4),
    {"kind": "suspended", "crid": "EEE-2026-09-05-entry-t0", "trough": 2.8, "next_tier_limit": 2.9},
    _watch_open("EEE-2026-09-05-entry-t1", "EEE:2026-09-05", "EEE", 1, limit=2.9, qty=4),
    {"kind": "cancelled", "crid": "EEE-2026-09-05-entry-t1", "note": "operator disarm"},
    # FFF: a tier with no watch_open (unvaluable, still a watch)
    {"kind": "touched", "crid": "FFF-2026-09-06-entry-t0"},
    # one malformed line
    "{not json",
]

_EXPECTED_STAGES = {
    "AAA-2026-09-01-entry-t0": "open",
    "AAA-2026-09-01-entry-t1": "touched",
    "BBB-2026-09-02-entry-t0": "trail_armed",
    "BBB-2026-09-02-entry-t1": "arming",
    "CCC-2026-09-03-entry-t0": "touched",
    "DDD-2026-09-04-entry-t0": "fired",
    "DDD-2026-09-04-entry-t1": "expired",
    "EEE-2026-09-05-entry-t0": "suspended",
    "EEE-2026-09-05-entry-t1": "cancelled",
    "FFF-2026-09-06-entry-t0": "touched",
}
_TERMINAL = {
    "DDD-2026-09-04-entry-t0",
    "DDD-2026-09-04-entry-t1",
    "EEE-2026-09-05-entry-t0",
    "EEE-2026-09-05-entry-t1",
}
# 50 + 45 + 40/4 + 36/4 + 50 — the non-terminal, valuable tiers.
_EXPECTED_RESERVED = 50.0 + 45.0 + 10.0 + 9.0 + 50.0


def _seed(home: Path, env: str, lines: list[object] = _JOURNAL) -> Path:
    journal = home / ".alphalens" / "broker_orders" / env / "entry_trails.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    text = "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines)
    journal.write_text(text, encoding="utf-8")
    return journal


class WatchesCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = _isolate_home(self)
        # The command honours ALPHALENS_BROKER_ENVIRONMENT; the host shell must
        # not leak one into the tests.
        patcher = mock.patch.dict("os.environ", {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ALPHALENS_BROKER_ENVIRONMENT", None)

    def _invoke(self, *args: str):
        from alphalens_cli.commands.broker import broker_app

        return self.runner.invoke(broker_app, ["watches", *args])

    def _json(self, *args: str) -> dict:
        result = self._invoke("--format", "json", *args)
        self.assertEqual(result.exit_code, 0, result.output)
        # Exactly one JSON value on stdout.
        return json.loads(result.stdout)

    # --- JSON contract ------------------------------------------------------

    def test_json_default_hides_terminal_tiers_and_maps_every_stage(self) -> None:
        _seed(self.home, "sim")
        payload = self._json()
        self.assertEqual(payload["schema"], "alphalens.broker.watches/v1")
        self.assertEqual(payload["env"], "sim")
        stages = {row["crid"]: row["stage"] for row in payload["watches"]}
        self.assertEqual(
            stages, {crid: s for crid, s in _EXPECTED_STAGES.items() if crid not in _TERMINAL}
        )
        self.assertEqual(len(payload["watches"]), len(_EXPECTED_STAGES) - len(_TERMINAL))

    def test_json_all_adds_terminal_tiers_with_their_records(self) -> None:
        _seed(self.home, "sim")
        payload = self._json("--all")
        rows = {row["crid"]: row for row in payload["watches"]}
        self.assertEqual({crid: row["stage"] for crid, row in rows.items()}, _EXPECTED_STAGES)
        fired = rows["DDD-2026-09-04-entry-t0"]
        self.assertEqual((fired["fired_avg_price"], fired["fired_realized_qty"]), (42.5, 3))
        self.assertEqual(fired["armed_order_id"], "O-3")
        self.assertEqual(rows["EEE-2026-09-05-entry-t1"]["terminal_note"], "operator disarm")

    def test_json_row_values(self) -> None:
        _seed(self.home, "sim")
        rows = {row["crid"]: row for row in self._json()["watches"]}
        armed = rows["BBB-2026-09-02-entry-t0"]
        self.assertEqual(armed["tier"], "E1")
        self.assertEqual(armed["pick_key"], "BBB:2026-09-02")
        self.assertEqual(armed["reservation_acct"], 20.0 * 2 / 4.0)
        self.assertEqual(
            (armed["armed_order_id"], armed["armed_trigger"], armed["armed_ceiling"]),
            ("O-1", 19.2, 19.3),
        )
        self.assertEqual(armed["window_end"], _WINDOW_END)
        stale = rows["CCC-2026-09-03-entry-t0"]
        self.assertEqual((stale["stage"], stale["armed_order_id"]), ("touched", "O-2"))
        arming = rows["BBB-2026-09-02-entry-t1"]
        self.assertEqual(
            (arming["stage"], arming["armed_order_id"], arming["armed_trigger"]),
            ("arming", None, 17.5),
        )
        touched = rows["AAA-2026-09-01-entry-t1"]
        self.assertEqual((touched["tier"], touched["min_trough"]), ("E2", 8.8))
        unvaluable = rows["FFF-2026-09-06-entry-t0"]
        self.assertEqual((unvaluable["reservation_acct"], unvaluable["pick_key"]), (None, None))

    def test_json_watching_summary_matches_the_money_gate(self) -> None:
        from alphalens_pipeline.brokers.automanager import entry_trails

        journal = _seed(self.home, "sim")
        payload = self._json()
        watching = payload["watching"]
        self.assertEqual(watching["tiers"], 6)
        # Distinct pick keys of the non-terminal tiers; the keyless FFF tier
        # counts by its crid, the way the daemon's MAX_OPEN fold counts it.
        self.assertEqual(watching["picks"], 4)
        self.assertEqual(watching["reserved_acct"], _EXPECTED_RESERVED)
        self.assertEqual(watching["unvaluable_tiers"], 1)
        self.assertEqual(payload["malformed"], 1)
        total, bad = entry_trails.watching_virtual_gross_acct(
            entry_trails.read_entry_trail_fold(path=journal)
        )
        self.assertEqual(watching["reserved_acct"], total)
        self.assertEqual(payload["malformed"] + watching["unvaluable_tiers"], bad)
        self.assertEqual(payload["journal"], str(journal))

    def test_json_summary_does_not_depend_on_all(self) -> None:
        _seed(self.home, "sim")
        self.assertEqual(self._json()["watching"], self._json("--all")["watching"])

    def test_json_empty_journal(self) -> None:
        payload = self._json()
        self.assertEqual(payload["watches"], [])
        self.assertEqual(payload["watching"]["tiers"], 0)
        self.assertEqual(payload["watching"]["reserved_acct"], 0.0)

    # --- env routing --------------------------------------------------------

    def test_env_option_reads_the_named_instance(self) -> None:
        _seed(self.home, "live")
        self.assertEqual(self._json("--env", "live")["watching"]["tiers"], 6)
        sim = self._invoke()
        self.assertEqual(sim.exit_code, 0, sim.output)
        self.assertIn("no watches in", sim.stdout)
        self.assertIn(str(self.home / ".alphalens" / "broker_orders" / "sim"), sim.stdout)

    def test_env_var_is_honoured_without_the_option(self) -> None:
        _seed(self.home, "live")
        with mock.patch.dict("os.environ", {"ALPHALENS_BROKER_ENVIRONMENT": "live"}):
            payload = self._json()
            human = self._invoke()
        self.assertEqual(payload["env"], "live")
        self.assertEqual(payload["watching"]["tiers"], 6)
        self.assertIn("env  live", human.stdout)

    def test_unknown_env_fails_with_empty_stdout(self) -> None:
        result = self._invoke("--env", "bogus")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("bogus", result.stderr)

    def test_unknown_format_fails_with_empty_stdout(self) -> None:
        _seed(self.home, "sim")
        result = self._invoke("--format", "xml")
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--format", result.stderr)

    def test_legacy_flat_layout_refuses(self) -> None:
        _seed_legacy_flat_state(self.home)
        result = self._invoke()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stdout, "")

    # --- human rendering ----------------------------------------------------

    def test_human_rows_carry_the_same_facts_per_line(self) -> None:
        _seed(self.home, "sim")
        result = self._invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        out = result.stdout
        self.assertTrue(out.startswith("env  sim\n"), out)
        lines = out.splitlines()
        aaa_e2 = next(line for line in lines if "AAA:2026-09-01" in line and " E2 " in line)
        self.assertIn("touched", aaa_e2)
        self.assertIn("trough 8.8", aaa_e2)
        bbb_e1 = next(line for line in lines if "BBB:2026-09-02" in line and " E1 " in line)
        self.assertIn("trail_armed", bbb_e1)
        self.assertIn("order O-1", bbb_e1)
        self.assertIn("trigger 19.2", bbb_e1)
        self.assertIn("ceiling 19.3", bbb_e1)
        self.assertIn(f"ttl {_WINDOW_END}", bbb_e1)
        self.assertIn("resv 10.00", bbb_e1)
        bbb_e2 = next(line for line in lines if "BBB:2026-09-02" in line and " E2 " in line)
        self.assertIn("arming", bbb_e2)
        self.assertIn("order pending", bbb_e2)
        ccc = next(line for line in lines if "CCC:2026-09-03" in line)
        self.assertIn("touched", ccc)
        self.assertIn("order O-2", ccc)
        # Terminal rows hidden by default; the fired price appears only with --all.
        self.assertNotIn("fired", out)
        self.assertNotIn("avg 42.5", out)
        self.assertNotIn("?", out)
        self.assertNotIn("None", out)
        footer = lines[-1]
        self.assertIn("watching 6 tier(s) / 4 pick(s)", footer)
        self.assertIn(f"reserved {_EXPECTED_RESERVED:.2f}", footer)
        self.assertIn("unvaluable 1", footer)
        self.assertIn("malformed 1", footer)

    def test_human_all_shows_terminal_rows_with_their_detail(self) -> None:
        _seed(self.home, "sim")
        result = self._invoke("--all")
        self.assertEqual(result.exit_code, 0, result.output)
        lines = result.stdout.splitlines()
        with_avg = [line for line in lines if "avg 42.5" in line]
        self.assertEqual(len(with_avg), 1)
        self.assertIn("fired", with_avg[0])
        self.assertIn("qty 3", with_avg[0])
        cancelled = next(line for line in lines if "EEE:2026-09-05" in line and " E2 " in line)
        self.assertIn("cancelled", cancelled)
        self.assertIn("operator disarm", cancelled)
        # The summary counts the same non-terminal tiers as without --all.
        self.assertIn("watching 6 tier(s) / 4 pick(s)", lines[-1])

    def test_human_renders_a_corrupt_fired_record_without_crashing(self) -> None:
        # Zen finding on #1381: the fill facts reached the `:g` formatter raw,
        # so a corrupt journal value crashed human mode while JSON succeeded.
        _seed(
            self.home,
            "sim",
            [
                _watch_open(
                    "KO-2026-09-08-entry-t0", "KO:2026-09-08", "KO", 0, limit="x", qty=None
                ),
                {
                    "kind": "fired",
                    "crid": "KO-2026-09-08-entry-t0",
                    "avg_price": "oops",
                    "realized_qty": [1],
                },
            ],
        )
        result = self._invoke("--all")
        self.assertEqual(result.exit_code, 0, result.output)
        line = next(line for line in result.stdout.splitlines() if "KO:2026-09-08" in line)
        self.assertIn("fired", line)
        self.assertNotIn("avg", line)
        self.assertNotIn("oops", line)
        self.assertIn("resv -", line)

    def test_human_footer_omits_zero_counts(self) -> None:
        _seed(self.home, "sim", [_JOURNAL[0]])
        result = self._invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        footer = result.stdout.splitlines()[-1]
        self.assertIn("watching 1 tier(s) / 1 pick(s)", footer)
        self.assertNotIn("unvaluable", footer)
        self.assertNotIn("malformed", footer)


if __name__ == "__main__":
    unittest.main()
