"""TDD for the Form-4 daily-incremental ingest engine.

Covers:
  * ``parse_form4_index_rows`` keeps only Form-4 / 4-A rows from a daily ``.idx``.
  * ``fetch_form4_records_for_window`` resolves each daily-index row to its
    deterministic full-submission ``.txt``, slices out the inline
    ``<ownershipDocument>`` XML, parses it, writes parquet, and compacts — with
    NO per-CIK ``submissions`` roundtrip (the speedup contract).
  * overlap dedup-safety: two overlapping windows + compaction -> unique accessions.
  * window date math is inclusive UTC.
  * a daily-index 403 is counted and does not raise / abort the window.
  * ``IncrementalResult`` carries the metric fields the runner emits.

All SEC access is through a fake client (no live HTTP). The runner argparse
defaults are also pinned here.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock

import pandas as pd
import pyarrow.parquet as pq
from alphalens_pipeline.data.alt_data.form4_incremental import (
    IncrementalResult,
    _extract_ownership_document,
    _full_submission_txt_url,
    fetch_form4_records_for_window,
    latest_filed_date_in_store,
    parse_form4_index_rows,
)
from alphalens_pipeline.data.alt_data.form4_records import Form4ParseError
from alphalens_pipeline.data.alt_data.sec_edgar_client import SecForbiddenError


def _idx(rows: list[str]) -> str:
    """Build a daily form .idx body with a header + dashed separator + rows."""
    header = [
        "Description:           Daily Index of EDGAR Dissemination Feed by Form Type.",
        "Last Data Received:    June 5, 2026",
        " ",
        "Form Type   Company Name                              CIK         Date Filed  File Name",
        "-" * 110,
    ]
    return "\n".join(header + rows) + "\n"


def _index_row(form: str, company: str, cik: int, date_filed: str, accession: str) -> str:
    """One fixed-width-ish whitespace-separated daily-index row.

    The parser anchors on column 0 (Form Type) + the last three tokens
    (CIK, Date Filed, File Name), so spacing only needs to be whitespace.
    """
    file_name = f"edgar/data/{cik}/{accession}-index.htm"
    return f"{form:<11} {company:<40} {cik:<10}  {date_filed}  {file_name}"


def _ownership_xml(*, issuer_cik: int, ticker: str, owner_cik: int, tx_date: str, code: str) -> str:
    return (
        "<ownershipDocument>"
        "<documentType>4</documentType>"
        f"<issuer><issuerCik>{issuer_cik}</issuerCik>"
        f"<issuerTradingSymbol>{ticker}</issuerTradingSymbol></issuer>"
        "<reportingOwner>"
        f"<reportingOwnerId><rptOwnerCik>{owner_cik}</rptOwnerCik>"
        "<rptOwnerName>Doe John</rptOwnerName></reportingOwnerId>"
        "<reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>"
        "</reportingOwner>"
        "<nonDerivativeTable><nonDerivativeTransaction>"
        f"<transactionDate><value>{tx_date}</value></transactionDate>"
        f"<transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>"
        "<transactionAmounts>"
        "<transactionShares><value>100</value></transactionShares>"
        "<transactionPricePerShare><value>50.0</value></transactionPricePerShare>"
        "<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>"
        "</transactionAmounts>"
        "</nonDerivativeTransaction></nonDerivativeTable>"
        "</ownershipDocument>"
    )


def _submission_txt(
    *, issuer_cik: int, ticker: str, owner_cik: int, tx_date: str, code: str
) -> str:
    """A realistic full-submission .txt: SEC headers + SGML wrapper + inline XML."""
    body = _ownership_xml(
        issuer_cik=issuer_cik, ticker=ticker, owner_cik=owner_cik, tx_date=tx_date, code=code
    )
    return (
        "<SEC-DOCUMENT>0000000111-26-000050.txt : 20260605\n"
        "<SEC-HEADER>0000000111-26-000050.hdr.sgml : 20260605\n"
        "ACCESSION NUMBER:\t\t0000000111-26-000050\n"
        "</SEC-HEADER>\n"
        "<DOCUMENT>\n<TYPE>4\n<SEQUENCE>1\n<FILENAME>wf-form4_172.xml\n"
        "<TEXT>\n<XML>\n"
        "<?xml version='1.0'?>\n"
        f"{body}\n"
        "</XML>\n</TEXT>\n</DOCUMENT>\n</SEC-DOCUMENT>\n"
    )


class _FakeClient:
    """Minimal stand-in for SecEdgarClient — serves daily ``.idx`` and the
    deterministic full-submission ``.txt`` through the single ``get_text`` entry.

    ``index_by_date`` maps a UTC ``date`` to a raw ``.idx`` text (or an exception
    instance to raise on fetch). ``txt_by_accession`` maps an accession to its
    full-submission ``.txt`` body (or an exception to raise on fetch).

    ``fetch_submissions`` raises on call so any regression that reintroduces the
    per-CIK roundtrip fails loudly (the speedup contract). ``txt_fetches`` records
    every full-submission fetch so a test can assert exactly one per accession.
    """

    def __init__(
        self,
        *,
        index_by_date: dict[date, object],
        txt_by_accession: dict[str, object],
    ) -> None:
        self._index_by_date = index_by_date
        self._txt_by_accession = txt_by_accession
        self.index_fetches: list[date] = []
        self.txt_fetches: list[str] = []
        self.txt_urls: list[str] = []
        self.submissions_calls: list[str] = []

    @staticmethod
    def _date_from_idx_url(url: str) -> date:
        # .../form.YYYYMMDD.idx
        stamp = url.rsplit("form.", 1)[1].split(".idx", 1)[0]
        return date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))

    @staticmethod
    def _accession_from_txt_url(url: str) -> str:
        # .../{acc_no_dashes}/{acc_with_dashes}.txt
        return url.rsplit("/", 1)[-1].removesuffix(".txt")

    def get_text(self, url: str, *, encoding: str = "utf-8") -> str:
        if url.endswith(".txt"):
            acc = self._accession_from_txt_url(url)
            self.txt_fetches.append(acc)
            self.txt_urls.append(url)
            value = self._txt_by_accession.get(acc)
            if isinstance(value, Exception):
                raise value
            if value is None:
                raise AssertionError(f"unexpected .txt fetch for {acc}")
            return value  # type: ignore[return-value]
        d = self._date_from_idx_url(url)
        self.index_fetches.append(d)
        value = self._index_by_date.get(d)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise AssertionError(f"unexpected index fetch for {d}")
        return value  # type: ignore[return-value]

    def fetch_submissions(self, cik: str) -> dict:
        self.submissions_calls.append(cik)
        raise AssertionError(
            "form4-incremental must NOT fetch per-CIK submissions on the daily-index path"
        )


class TestFullSubmissionTxtPath(unittest.TestCase):
    def test_txt_url_is_deterministic_from_cik_and_accession(self) -> None:
        url = _full_submission_txt_url(cik="0000320193", accession_number="0000320193-26-000050")
        self.assertEqual(
            url,
            "https://www.sec.gov/Archives/edgar/data/320193/"
            "000032019326000050/0000320193-26-000050.txt",
        )

    def test_extract_ownership_document_slices_inline_xml(self) -> None:
        txt = _submission_txt(
            issuer_cik=320193, ticker="AAPL", owner_cik=999, tx_date="2026-06-04", code="P"
        )
        xml = _extract_ownership_document(txt)
        self.assertTrue(xml.startswith(b"<ownershipDocument>"))
        self.assertTrue(xml.endswith(b"</ownershipDocument>"))
        # The SGML header (accession line) and the <XML> wrapper must be gone.
        self.assertNotIn(b"<SEC-HEADER>", xml)
        self.assertNotIn(b"<XML>", xml)

    def test_extract_ownership_document_raises_when_block_absent(self) -> None:
        with self.assertRaises(Form4ParseError):
            _extract_ownership_document("<SEC-DOCUMENT>no ownership xml here</SEC-DOCUMENT>")


class TestParseFormIndexRows(unittest.TestCase):
    def test_parse_form4_index_rows_keeps_only_form4_and_amendments(self) -> None:
        idx = _idx(
            [
                _index_row("4", "ACME CORP", 111, "2026-06-05", "0000000111-26-000001"),
                _index_row("4/A", "ACME CORP", 111, "2026-06-05", "0000000111-26-000002"),
                _index_row("8-K", "ACME CORP", 111, "2026-06-05", "0000000111-26-000003"),
                _index_row("10-K", "OTHER INC", 222, "2026-06-05", "0000000222-26-000009"),
            ]
        )

        rows = parse_form4_index_rows(idx)

        forms = sorted(r.form for r in rows)
        self.assertEqual(forms, ["4", "4/A"])
        accessions = {r.accession_number for r in rows}
        self.assertEqual(accessions, {"0000000111-26-000001", "0000000111-26-000002"})
        # CIK is zero-padded to 10 digits.
        self.assertTrue(all(len(r.cik) == 10 and r.cik.isdigit() for r in rows))


class TestFetchWindowSpeedup(unittest.TestCase):
    def test_daily_index_path_makes_no_per_cik_submissions_calls(self) -> None:
        # Speedup contract: a day with 3 distinct CIKs (one filing 2 Form-4s) must
        # be ingested with ZERO submissions roundtrips and EXACTLY one
        # full-submission .txt fetch per accession (4) plus the 1 daily index.
        today = date(2026, 6, 5)
        rows = [
            ("4", 111, "0000000111-26-000041"),
            ("4", 111, "0000000111-26-000042"),  # same CIK, second filing same day
            ("4", 222, "0000000222-26-000010"),
            ("4/A", 333, "0000000333-26-000007"),
        ]
        idx = _idx([_index_row(form, "CO", cik, "2026-06-05", acc) for form, cik, acc in rows])
        txt_by_accession = {
            acc: _submission_txt(
                issuer_cik=cik, ticker="CO", owner_cik=999, tx_date="2026-06-04", code="P"
            )
            for _form, cik, acc in rows
        }
        client = _FakeClient(index_by_date={today: idx}, txt_by_accession=txt_by_accession)

        with self.tmp_root() as root:
            result = fetch_form4_records_for_window(
                client, start_date=today, end_date=today, parquet_root=root
            )
            accessions = self._all_accessions(root)

        self.assertEqual(client.submissions_calls, [], "no per-CIK submissions fetch allowed")
        self.assertEqual(
            sorted(client.txt_fetches),
            sorted(acc for _f, _c, acc in rows),
            "exactly one full-submission .txt fetch per accession",
        )
        self.assertEqual(accessions, {acc for _f, _c, acc in rows})
        self.assertEqual(result.distinct_accessions, 4)

    def test_txt_url_built_from_index_issuer_cik_not_accession_prefix(self) -> None:
        # Real EDGAR: the daily-index (issuer) CIK differs from the accession
        # prefix (the filer/agent CIK). The full-submission .txt lives under the
        # ISSUER directory, so the URL must be built from the index row's CIK,
        # not from the accession prefix — otherwise prod 404s while the hermetic
        # suite (which ignored the CIK segment) stayed green.
        today = date(2026, 6, 5)
        issuer_cik, acc = 34782, "0000939167-26-000123"  # prefix 939167 != issuer 34782
        idx = _idx([_index_row("4", "DELTA AIR LINES", issuer_cik, "2026-06-05", acc)])
        txt = _submission_txt(
            issuer_cik=issuer_cik, ticker="DAL", owner_cik=999, tx_date="2026-06-04", code="P"
        )
        client = _FakeClient(index_by_date={today: idx}, txt_by_accession={acc: txt})

        with self.tmp_root() as root:
            result = fetch_form4_records_for_window(
                client, start_date=today, end_date=today, parquet_root=root
            )

        self.assertEqual(result.rows_written, 1)
        self.assertEqual(len(client.txt_urls), 1)
        self.assertIn("/data/34782/", client.txt_urls[0], "txt URL must use the issuer CIK dir")
        self.assertNotIn(
            "/data/939167/", client.txt_urls[0], "must NOT use the accession-prefix CIK"
        )

    def tmp_root(self):
        import tempfile
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            with tempfile.TemporaryDirectory() as d:
                yield Path(d) / "form4_parquet"

        return _cm()

    @staticmethod
    def _all_accessions(root: Path) -> set[str]:
        accs: set[str] = set()
        for f in root.rglob("*.parquet"):
            tbl = pq.read_table(f)
            accs.update(tbl.column("accession_number").to_pylist())
        return accs


class TestWindowDateMath(unittest.TestCase):
    def test_window_date_math_is_inclusive_utc(self) -> None:
        # lookback window [asof - 2, asof] must touch exactly floor, mid, asof.
        asof = date(2026, 6, 5)
        floor = date(2026, 6, 3)
        touched: dict[date, str] = {}
        for d in (date(2026, 6, 3), date(2026, 6, 4), date(2026, 6, 5)):
            acc = f"0000000111-26-0000{d.day:02d}"
            touched[d] = acc
        idx_by_date = {
            d: _idx([_index_row("4", "ACME", 111, d.isoformat(), acc)])
            for d, acc in touched.items()
        }
        client = _FakeClient(
            index_by_date=idx_by_date,
            txt_by_accession={
                acc: _submission_txt(
                    issuer_cik=111, ticker="ACME", owner_cik=999, tx_date=d.isoformat(), code="P"
                )
                for d, acc in touched.items()
            },
        )

        with TestFetchWindowSpeedup().tmp_root() as root:
            fetch_form4_records_for_window(
                client, start_date=floor, end_date=asof, parquet_root=root
            )

        self.assertEqual(sorted(client.index_fetches), [floor, date(2026, 6, 4), asof])


class TestOverlapDedup(unittest.TestCase):
    def test_overlapping_windows_dedup_to_unique_accessions(self) -> None:
        # Two windows overlap on 2026-06-04 + 2026-06-05; the shared accessions
        # must collapse to one row each after the engine's own compaction.
        days = {
            date(2026, 6, 3): "0000000111-26-000003",
            date(2026, 6, 4): "0000000111-26-000004",
            date(2026, 6, 5): "0000000111-26-000005",
        }
        idx_by_date = {
            d: _idx([_index_row("4", "ACME", 111, d.isoformat(), acc)]) for d, acc in days.items()
        }
        client = _FakeClient(
            index_by_date=idx_by_date,
            txt_by_accession={
                acc: _submission_txt(
                    issuer_cik=111, ticker="ACME", owner_cik=999, tx_date=d.isoformat(), code="P"
                )
                for d, acc in days.items()
            },
        )

        with TestFetchWindowSpeedup().tmp_root() as root:
            fetch_form4_records_for_window(
                client, start_date=date(2026, 6, 3), end_date=date(2026, 6, 4), parquet_root=root
            )
            fetch_form4_records_for_window(
                client, start_date=date(2026, 6, 4), end_date=date(2026, 6, 5), parquet_root=root
            )
            # Count rows for the two overlapping accessions: must be exactly one each.
            counts: dict[str, int] = {}
            for f in root.rglob("*.parquet"):
                tbl = pq.read_table(f)
                for acc in tbl.column("accession_number").to_pylist():
                    counts[acc] = counts.get(acc, 0) + 1

        self.assertEqual(counts.get("0000000111-26-000004"), 1)
        self.assertEqual(counts.get("0000000111-26-000005"), 1)
        self.assertEqual(counts.get("0000000111-26-000003"), 1)


class TestGracefulDegrade(unittest.TestCase):
    def test_daily_index_403_is_counted_and_does_not_raise(self) -> None:
        # One date 403s; the other date in the window succeeds. The window must
        # NOT raise, must count the transient, and must still write the good day.
        bad = date(2026, 6, 4)
        good = date(2026, 6, 5)
        good_acc = "0000000111-26-000005"
        client = _FakeClient(
            index_by_date={
                bad: SecForbiddenError("403 traffic threshold"),
                good: _idx([_index_row("4", "ACME", 111, good.isoformat(), good_acc)]),
            },
            txt_by_accession={
                good_acc: _submission_txt(
                    issuer_cik=111, ticker="ACME", owner_cik=999, tx_date="2026-06-05", code="P"
                )
            },
        )

        with TestFetchWindowSpeedup().tmp_root() as root:
            result = fetch_form4_records_for_window(
                client, start_date=bad, end_date=good, parquet_root=root
            )
            accessions = TestFetchWindowSpeedup._all_accessions(root)

        self.assertEqual(result.transient_errors, 1)
        self.assertEqual(accessions, {good_acc})

    def test_submission_txt_without_ownership_block_is_counted_other_error(self) -> None:
        # A .txt whose ownership block is missing/unparseable must be counted as
        # an other_error and skipped — not raised, not written.
        d = date(2026, 6, 5)
        bad_acc = "0000000111-26-000001"
        good_acc = "0000000111-26-000002"
        client = _FakeClient(
            index_by_date={
                d: _idx(
                    [
                        _index_row("4", "ACME", 111, d.isoformat(), bad_acc),
                        _index_row("4", "ACME", 111, d.isoformat(), good_acc),
                    ]
                )
            },
            txt_by_accession={
                bad_acc: "<SEC-DOCUMENT>no ownership xml</SEC-DOCUMENT>",
                good_acc: _submission_txt(
                    issuer_cik=111, ticker="ACME", owner_cik=999, tx_date="2026-06-04", code="P"
                ),
            },
        )

        with TestFetchWindowSpeedup().tmp_root() as root:
            result = fetch_form4_records_for_window(
                client, start_date=d, end_date=d, parquet_root=root
            )
            accessions = TestFetchWindowSpeedup._all_accessions(root)

        self.assertEqual(result.other_errors, 1)
        self.assertEqual(accessions, {good_acc})


class TestIncrementalResult(unittest.TestCase):
    def test_result_reports_rows_written_and_latest_filing_date(self) -> None:
        d = date(2026, 6, 5)
        acc = "0000000111-26-000005"
        client = _FakeClient(
            index_by_date={d: _idx([_index_row("4", "ACME", 111, d.isoformat(), acc)])},
            txt_by_accession={
                acc: _submission_txt(
                    issuer_cik=111, ticker="ACME", owner_cik=999, tx_date="2026-06-04", code="P"
                )
            },
        )

        with TestFetchWindowSpeedup().tmp_root() as root:
            result = fetch_form4_records_for_window(
                client, start_date=d, end_date=d, parquet_root=root
            )

        self.assertIsInstance(result, IncrementalResult)
        self.assertEqual(result.rows_written, 1)
        self.assertEqual(result.distinct_accessions, 1)
        self.assertEqual(result.latest_filing_date, d)
        self.assertEqual(result.transient_errors, 0)


class TestRunnerArgparse(unittest.TestCase):
    def test_runner_argparse_defaults_lookback_three_and_asof_today(self) -> None:
        # Import the runner module from the scripts package (requires -t apps/alphalens-research).
        from scripts.run_form4_daily_incremental import parse_args

        args = parse_args([])
        self.assertEqual(args.lookback_days, 3)
        # Runner default is today_utc(); assert against UTC (not local date.today())
        # so the test cannot flake near UTC midnight on a non-UTC machine.
        self.assertEqual(args.asof_date, datetime.now(UTC).date())


class TestWindowSizing(unittest.TestCase):
    """The window auto-extends to cover the gap to the store's newest filing, so
    a late first deploy or a missed run self-heals — no manual catch-up sizing.
    """

    def _start(self, **kw):
        from scripts.run_form4_daily_incremental import _resolve_window_start

        base = {
            "asof_date": date(2026, 6, 10),
            "lookback_days": 3,
            "overlap_days": 2,
            "max_catchup_days": 400,
        }
        base.update(kw)
        return _resolve_window_start(**base)

    def test_fresh_store_floors_at_lookback(self) -> None:
        # Store fully current (today already in store) -> the 3-day lookback
        # floor (asof - 2) dominates; the reach-back (asof - overlap) ties it.
        self.assertEqual(self._start(latest_in_store=date(2026, 6, 10)), date(2026, 6, 8))

    def test_fresh_store_yesterday_includes_overlap(self) -> None:
        # Store one day behind -> reach-back (latest - overlap = 06-07) extends
        # one day past the lookback floor; a dedup-safe 4-day window.
        self.assertEqual(self._start(latest_in_store=date(2026, 6, 9)), date(2026, 6, 7))

    def test_stale_store_reaches_back_to_latest_minus_overlap(self) -> None:
        # Store ends a month ago -> window starts at latest - overlap (gap closed).
        self.assertEqual(self._start(latest_in_store=date(2026, 5, 7)), date(2026, 5, 5))

    def test_runaway_capped_by_max_catchup(self) -> None:
        self.assertEqual(
            self._start(latest_in_store=date(2020, 1, 1), max_catchup_days=30),
            date(2026, 6, 10) - timedelta(days=30),
        )

    def test_empty_store_uses_lookback(self) -> None:
        self.assertEqual(self._start(latest_in_store=None), date(2026, 6, 8))


class TestLatestFiledDateInStore(unittest.TestCase):
    def test_empty_store_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(latest_filed_date_in_store(Path(d)))

    def test_max_filed_date_found_even_in_older_partition(self) -> None:
        # A late 4/A puts the newest filed_date in an OLDER transaction_year
        # partition, so scanning only the newest partition would miss it.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for year, filed in [("2026", date(2026, 5, 1)), ("2023", date(2026, 5, 20))]:
                part = root / f"transaction_year={year}"
                part.mkdir(parents=True)
                pd.DataFrame({"filed_date": [filed]}).to_parquet(part / "compacted.parquet")
            self.assertEqual(latest_filed_date_in_store(root), date(2026, 5, 20))

    def test_corrupted_future_filed_date_is_ignored(self) -> None:
        # A far-future filed_date must not drive window sizing; the newest
        # plausible (<= today) date wins instead.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            part = root / "transaction_year=2026"
            part.mkdir(parents=True)
            real = datetime.now(UTC).date() - timedelta(days=3)
            pd.DataFrame({"filed_date": [real, date(2999, 1, 1)]}).to_parquet(
                part / "compacted.parquet"
            )
            self.assertEqual(latest_filed_date_in_store(root), real)


class TestPerDateFlushBoundsMemory(unittest.TestCase):
    """A long catch-up window must write per-date, not accumulate the whole
    window in memory (a 34-day first run OOM-killed at the unit's 1G cap).
    """

    def test_writes_once_per_date_then_compacts_once(self) -> None:
        import alphalens_pipeline.data.alt_data.form4_incremental as eng

        d1, d2 = date(2026, 6, 4), date(2026, 6, 5)
        acc1, acc2 = "0000000111-26-000041", "0000000111-26-000042"
        client = _FakeClient(
            index_by_date={
                d1: _idx([_index_row("4", "ACME CORP", 111, "2026-06-04", acc1)]),
                d2: _idx([_index_row("4", "ACME CORP", 111, "2026-06-05", acc2)]),
            },
            txt_by_accession={
                acc1: _submission_txt(
                    issuer_cik=111, ticker="ACME", owner_cik=999, tx_date="2026-06-04", code="P"
                ),
                acc2: _submission_txt(
                    issuer_cik=111, ticker="ACME", owner_cik=999, tx_date="2026-06-05", code="P"
                ),
            },
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                eng, "write_records_to_parquet", wraps=eng.write_records_to_parquet
            ) as wr,
            mock.patch.object(eng, "compact_root", wraps=eng.compact_root) as cr,
        ):
            result = eng.fetch_form4_records_for_window(
                client, start_date=d1, end_date=d2, parquet_root=Path(tmp)
            )

        self.assertEqual(
            wr.call_count, 2, "must flush once per date, not once for the whole window"
        )
        self.assertEqual(cr.call_count, 1, "compaction runs once at the end")
        self.assertEqual(result.rows_written, 2)
        self.assertEqual(result.distinct_accessions, 2)


if __name__ == "__main__":
    unittest.main()
