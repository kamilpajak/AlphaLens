"""The recovery of the brief list that existed before the arrival open (#1479 follow-up).

Until #1482 the thematic build rewrote the brief for a date up to six times a day, so on
17 as-of dates the stored list is not the one a reader could have seen at the open. The
build journal still holds, per run, the score-stage table of candidates BY NAME, so the
pre-open list is recoverable. These tests pin the recovery rule on synthetic journal text
and pin the committed CSV against ticker lists read by hand from the frozen extract.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import tempfile
import unittest
from pathlib import Path

from scripts.recover_pre_open_brief_names import (
    RECOVERY_EXACT,
    RECOVERY_NO_LIST,
    RECOVERY_PARTIAL,
    DateRecovery,
    recover_all,
    recover_date,
    recovery_dates,
    write_csv,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EXTRACT = REPO_ROOT / "docs/research/pre_open_brief_names_2026_09_18_journal.log.gz"
CSV_PATH = REPO_ROOT / "docs/research/pre_open_brief_names_2026_09_18.csv"

# 2026-06-10 is a Wednesday; its arrival session is Thursday 2026-06-11, open 13:30Z.
ASOF = dt.date(2026, 6, 10)


def _line(ts: str, pid: int, msg: str) -> str:
    return f"{ts} vmi2478967 env[{pid}]: {msg}"


def _table(ts: str, pid: int, tickers: list[str]) -> list[str]:
    head = _line(ts, pid, "ticker   industry             score       ins$  fcff%  val% technicals")
    rule = _line(ts, pid, "-" * 110)
    rows = [
        _line(
            ts,
            pid,
            f"{t:8s} {'Services-Prepackage':20s} {3:>5d} {'0k':>10s} {'7.3':>6s} {'53':>5s} RSI 55",
        )
        for t in tickers
    ]
    return [head, rule, *rows]


def _run(ts_prefix: str, pid: int, tickers: list[str], *, briefs: int | None = None) -> list[str]:
    """One build run: its score table, then its brief write."""
    briefs = len(tickers) if briefs is None else briefs
    return [
        *_table(f"{ts_prefix}:00+00:00", pid, tickers),
        _line(f"{ts_prefix}:01+00:00", pid, f"[{ts_prefix}:01Z] thematic brief"),
        _line(
            f"{ts_prefix}:02+00:00",
            pid,
            f"Generating briefs for {len(tickers)} scored rows from "
            f"/app/home/.alphalens/thematic_scored/{ASOF.isoformat()}.parquet (asof={ASOF.isoformat()})...",
        ),
        _line(
            f"{ts_prefix}:03+00:00",
            pid,
            f"generate_briefs {ASOF.isoformat()}: wrote {briefs} briefs (Pro=1, Flash={briefs - 1}) "
            f"→ /app/home/.alphalens/thematic_briefs/{ASOF.isoformat()}.parquet",
        ),
    ]


class TestWhichRunIsTheList(unittest.TestCase):
    def test_the_last_write_before_the_open_wins(self):
        text = "\n".join(
            [
                *_run("2026-06-11T04:50", 100, ["AAA", "BBB"]),
                *_run("2026-06-11T08:50", 200, ["CCC", "DDD", "EEE"]),
            ]
        )
        result = recover_date(text, ASOF)
        self.assertEqual(result.names, ["CCC", "DDD", "EEE"])
        self.assertEqual(result.source_pid, "200")

    def test_a_write_after_the_open_is_never_used(self):
        text = "\n".join(
            [
                *_run("2026-06-11T08:50", 100, ["AAA", "BBB"]),
                *_run("2026-06-11T20:50", 200, ["CCC", "DDD", "EEE"]),
            ]
        )
        result = recover_date(text, ASOF)
        self.assertEqual(result.names, ["AAA", "BBB"])

    def test_a_write_exactly_at_the_open_is_after_it(self):
        text = "\n".join(_run("2026-06-11T13:29", 100, ["AAA"]))
        # The write line lands at 13:29:03Z, before the 13:30Z open.
        self.assertEqual(recover_date(text, ASOF).names, ["AAA"])
        late = text.replace("2026-06-11T13:29:03", "2026-06-11T13:30:00")
        self.assertEqual(recover_date(late, ASOF).status, RECOVERY_NO_LIST)

    def test_a_date_without_a_pre_open_write_recovers_no_names(self):
        text = "\n".join(_run("2026-06-11T20:50", 100, ["AAA", "BBB"]))
        result = recover_date(text, ASOF)
        self.assertEqual(result.status, RECOVERY_NO_LIST)
        self.assertEqual(result.names, [])
        self.assertIsNone(result.source_pid)

    def test_another_dates_run_is_not_mixed_in(self):
        other = "\n".join(_run("2026-06-11T08:50", 900, ["ZZZ"])).replace(
            ASOF.isoformat(), "2026-06-12"
        )
        text = "\n".join([other, *_run("2026-06-11T08:55", 100, ["AAA", "BBB"])])
        self.assertEqual(recover_date(text, ASOF).names, ["AAA", "BBB"])

    def test_a_table_printed_by_another_pid_is_not_read(self):
        text = "\n".join(
            [
                *_table("2026-06-11T08:49:00+00:00", 999, ["XXX", "YYY"]),
                *_run("2026-06-11T08:50", 100, ["AAA", "BBB"]),
            ]
        )
        self.assertEqual(recover_date(text, ASOF).names, ["AAA", "BBB"])

    def test_a_pid_that_runs_twice_is_refused_instead_of_guessed(self):
        text = "\n".join(
            [
                *_run("2026-06-11T04:50", 100, ["AAA"]),
                *_run("2026-06-11T08:50", 100, ["BBB", "CCC"]),
            ]
        )
        with self.assertRaises(ValueError):
            recover_date(text, ASOF)


class TestWhatTheTableSays(unittest.TestCase):
    def test_a_ticker_in_two_themes_collapses_to_one_name(self):
        text = "\n".join(_run("2026-06-11T08:50", 100, ["AAA", "BBB", "AAA"], briefs=2))
        result = recover_date(text, ASOF)
        self.assertEqual(result.names, ["AAA", "BBB"])
        self.assertEqual(result.status, RECOVERY_EXACT)

    def test_fewer_names_than_briefs_is_partial(self):
        # _print_score_preview stops at 25 rows, so a longer list loses its tail.
        text = "\n".join(_run("2026-06-11T08:50", 100, ["AAA", "BBB"], briefs=3))
        result = recover_date(text, ASOF)
        self.assertEqual(result.status, RECOVERY_PARTIAL)
        self.assertEqual(result.n_briefs_logged, 3)
        self.assertEqual(result.n_names_recovered, 2)

    def test_log_noise_inside_the_table_is_not_a_ticker(self):
        rows = _run("2026-06-11T08:50", 100, ["AAA", "BBB"])
        noise = _line(
            "2026-06-11T08:50:00+00:00",
            100,
            "2026-06-11 08:50:00,123 WARNING alphalens_pipeline.data.alt_data.yfinance_client: PIT shares",
        )
        text = "\n".join([*rows[:3], noise, *rows[3:]])
        self.assertEqual(recover_date(text, ASOF).names, ["AAA", "BBB"])

    def test_the_two_readings_of_the_table_must_agree(self):
        text = "\n".join(_run("2026-06-11T08:50", 100, ["AAA", "BBB"]))
        # Shift one row out of the fixed-width ticker column: the two parsers now disagree.
        broken = text.replace("AAA      Services", "AAA Services")
        with self.assertRaises(ValueError):
            recover_date(broken, ASOF)

    def test_a_table_that_never_ends_is_refused(self):
        # A cut extract would otherwise let later lines pass as rows of the table.
        text = "\n".join(_table("2026-06-11T08:50:00+00:00", 100, ["AAA"]))
        text += "\n" + _line(
            "2026-06-11T08:50:04+00:00",
            100,
            f"generate_briefs {ASOF.isoformat()}: wrote 1 briefs (Pro=1, Flash=0)",
        )
        with self.assertRaises(ValueError):
            recover_date(text, ASOF)

    def test_a_first_token_longer_than_the_ticker_column_is_reported_as_partial(self):
        # Both readings drop it, so only the count check can see that a name is missing.
        text = "\n".join(_run("2026-06-11T08:50", 100, ["TOOLONGNAME", "BBB"], briefs=2))
        result = recover_date(text, ASOF)
        self.assertEqual(result.names, ["BBB"])
        self.assertEqual(result.status, RECOVERY_PARTIAL)

    def test_the_order_of_the_table_is_kept(self):
        text = "\n".join(_run("2026-06-11T08:50", 100, ["CCC", "AAA", "BBB"]))
        self.assertEqual(recover_date(text, ASOF).names, ["CCC", "AAA", "BBB"])


class TestWhichDatesTheExtractCanAnswer(unittest.TestCase):
    def test_a_date_whose_open_precedes_the_extract_is_not_attempted(self):
        text = _line("2026-06-11T00:00:00+00:00", 1, "Wrote 1 candidate rows → x.parquet")
        dates = recovery_dates(text)
        self.assertNotIn(dt.date(2026, 5, 28), dates)
        self.assertIn(dt.date(2026, 6, 14), dates)

    def test_the_committed_extract_answers_every_affected_date_after_it_starts(self):
        text = gzip.decompress(EXTRACT.read_bytes()).decode("utf-8")
        dates = recovery_dates(text)
        self.assertEqual(min(dates), dt.date(2026, 5, 28))
        self.assertEqual(len(dates), 16)

    def test_an_empty_extract_is_refused(self):
        with self.assertRaises(ValueError):
            recovery_dates("")


class TestTheCsv(unittest.TestCase):
    def test_a_date_without_names_writes_no_rows(self):
        rec = DateRecovery(
            asof=ASOF,
            status=RECOVERY_NO_LIST,
            n_briefs_logged=None,
            names=[],
            source_run_utc=None,
            source_pid=None,
            n_scored_logged=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.csv"
            write_csv([rec], out)
            with out.open(encoding="utf-8") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])

    def test_every_name_carries_its_date_position_and_run(self):
        text = "\n".join(_run("2026-06-11T08:50", 100, ["AAA", "BBB"]))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.csv"
            write_csv(recover_all(text, [ASOF]), out)
            with out.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual([r["ticker"] for r in rows], ["AAA", "BBB"])
        self.assertEqual([r["position"] for r in rows], ["1", "2"])
        self.assertEqual({r["asof"] for r in rows}, {ASOF.isoformat()})
        self.assertEqual({r["source_pid"] for r in rows}, {"100"})
        self.assertEqual({r["recovery_status"] for r in rows}, {RECOVERY_EXACT})
        self.assertEqual({r["source_run_utc"] for r in rows}, {"2026-06-11T08:50:03Z"})


class TestTheCommittedRecovery(unittest.TestCase):
    """The committed CSV must be what the committed script reads from the frozen extract."""

    @classmethod
    def setUpClass(cls):
        cls.text = gzip.decompress(EXTRACT.read_bytes()).decode("utf-8")
        with CSV_PATH.open(encoding="utf-8") as fh:
            cls.rows = list(csv.DictReader(fh))

    def _names(self, asof: str) -> list[str]:
        rows = sorted((r for r in self.rows if r["asof"] == asof), key=lambda r: int(r["position"]))
        return [r["ticker"] for r in rows]

    def test_the_csv_is_the_script_run_over_the_extract(self):
        rebuilt = []
        for rec in recover_all(self.text, recovery_dates(self.text)):
            rebuilt.extend((rec.asof.isoformat(), t, i + 1) for i, t in enumerate(rec.names))
        self.assertEqual(rebuilt, [(r["asof"], r["ticker"], int(r["position"])) for r in self.rows])

    def test_names_read_by_hand_from_the_extract(self):
        # Read by eye from the frozen log text, NOT from the script's output: a parser bug
        # cannot write its own ground truth. One small date, one large, one truncated.
        self.assertEqual(self._names("2026-06-04"), ["CRL", "FDS", "GME", "IRDM"])
        self.assertEqual(
            self._names("2026-08-02"),
            ["ESTC", "WT", "MORN", "MTCH", "LYFT", "SNAP", "ACLS", "UCTT", "KTOS", "MRCY"],
        )
        # 2026-05-28 printed 25 of its 26 scored rows, nine of them the same name (BAH).
        self.assertEqual(
            self._names("2026-05-28"),
            [
                "AMPL",
                "BRZE",
                "CXM",
                "EXPO",
                "NEOG",
                "MRCY",
                "BAH",
                "WEX",
                "MUSA",
                "WK",
                "MMS",
                "TTEK",
                "GPRE",
                "DAR",
                "AMRC",
                "ENPH",
                "RUN",
            ],
        )

    def test_the_recovery_covers_the_dates_it_claims(self):
        exact = {r["asof"] for r in self.rows if r["recovery_status"] == RECOVERY_EXACT}
        partial = {r["asof"] for r in self.rows if r["recovery_status"] == RECOVERY_PARTIAL}
        self.assertEqual(len(exact), 14)
        self.assertEqual(partial, {"2026-05-28"})
        self.assertEqual(len(self.rows), 155 + 17)

    def test_an_exact_date_recovered_every_name_the_log_counted(self):
        for r in self.rows:
            if r["recovery_status"] == RECOVERY_EXACT:
                self.assertEqual(r["n_names_recovered"], r["n_briefs_logged"], r["asof"])

    def test_no_date_repeats_a_ticker(self):
        seen = {(r["asof"], r["ticker"]) for r in self.rows}
        self.assertEqual(len(seen), len(self.rows))


if __name__ == "__main__":
    unittest.main()
