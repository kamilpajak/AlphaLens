"""Property-based tests for the point-in-time Form-4 record store.

``data/store/form4_pit.py`` is the PIT gate for the insider signal: every audit
reads it with an ``asof`` date and MUST NOT see a filing that was not yet public
on that date. The flagship invariant is **no look-ahead** — no returned row may
carry ``filed_date > asof`` — and it is exactly the kind of off-by-one a mutant
(``<=`` → ``<`` / ``>=``, or a dropped filter clause) slips past example tests.

Strategy: generate a random panel of Form-4 records + a random query, write it
to a REAL hive-partitioned parquet dataset on disk (the production read path,
including the per-year LRU partition cache), and assert:

  * no look-ahead / lookback lower bound (the PIT invariants);
  * ``records_as_of`` equals a naive pandas filter over the full panel
    (a differential oracle — the strongest mutant-killer);
  * ``records_for_person`` equals its own naive [Y-3, Y) filter;
  * the result is invariant to ``partition_cache_size`` (the LRU cache is a
    pure optimization: 0 / 1 / 2 / 32 must return the SAME rows);
  * monotonicity in ``lookback_days`` and determinism across repeated calls.

Disk-backed properties carry a per-test ``max_examples`` cap (one parquet write
per example) rather than the profile's default 300.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from alphalens_pipeline.data.store.delisting import DelistingEvent
from alphalens_pipeline.data.store.form4_pit import (
    FORM4_SCHEMA_COLUMNS,
    Form4PITStore,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from .base import PropertyTestCase

# Bounded panel window. Records span these years; queries anchor inside it so
# non-empty results are common (a vacuous all-empty panel would still satisfy
# the invariants but would not exercise the filter boundaries).
_MIN_DATE = dt.date(2019, 1, 1)
_MAX_DATE = dt.date(2023, 12, 31)
_LOOKBACKS = (30, 90, 180, 365)

# A small fixed pool of issuers (ticker <-> distinct zero-padded CIK) and
# reporting persons keeps the differential oracle discriminating: several
# records collide on issuer / person so the filters actually partition them.
_ISSUERS: tuple[tuple[str, str], ...] = (
    ("AAA", "0000000001"),
    ("BBB", "0000000002"),
    ("CCC", "0000000003"),
)
_PERSONS: tuple[str, ...] = ("0000000100", "0000000200", "0000000300")


class _StaticCikResolver:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = {k.upper(): v for k, v in mapping.items()}

    def lookup(self, ticker: str) -> str | None:
        return self._mapping.get(ticker.upper())


_RESOLVER = _StaticCikResolver(dict(_ISSUERS))


@st.composite
def _panels(draw: Any) -> list[dict[str, Any]]:
    """A list of Form-4 records with ``filed_date >= transaction_date``.

    ``filed_date >= transaction_date`` is the real-world constraint (a filing
    cannot predate its own transaction); it keeps ``transaction_date <= asof``
    whenever ``filed_date <= asof``, so the year-partition read window can never
    silently drop an in-window record. Accession numbers are index-unique.
    """
    specs = draw(
        st.lists(
            st.tuples(
                st.integers(0, len(_ISSUERS) - 1),  # issuer index
                st.integers(0, len(_PERSONS) - 1),  # person index
                st.dates(_MIN_DATE, _MAX_DATE),  # transaction_date
                st.integers(0, 400),  # filing lag in days
            ),
            min_size=0,
            max_size=10,
        )
    )
    records: list[dict[str, Any]] = []
    for i, (iss, per, txn, lag) in enumerate(specs):
        ticker, cik = _ISSUERS[iss]
        filed = min(txn + dt.timedelta(days=lag), _MAX_DATE)
        records.append(
            {
                "issuer_cik": cik,
                "ticker": ticker,
                "accession_number": f"ACC-{i:04d}",
                "filed_date": filed,
                "reporting_owner_cik": _PERSONS[per],
                "reporting_owner_name": f"Person {per}",
                "transaction_date": txn,
                "transaction_code": "P",
                "transaction_shares": 1000.0,
                "transaction_price_per_share": 50.0,
                "is_director": False,
                "is_officer": True,
                "is_ten_percent_owner": False,
                "acquired_disposed": "A",
                "is_amendment": False,
            }
        )
    return records


def _write_panel(root: Path, records: list[dict[str, Any]]) -> None:
    """Write records to a hive-partitioned (transaction_year) parquet dataset."""
    by_year: dict[int, list[dict[str, Any]]] = {}
    for rec in records:
        by_year.setdefault(rec["transaction_date"].year, []).append(rec)
    for year, recs in by_year.items():
        df = pd.DataFrame.from_records(recs, columns=FORM4_SCHEMA_COLUMNS)
        for col in ("filed_date", "transaction_date"):
            df[col] = pd.to_datetime(df[col]).dt.date
        table = pa.Table.from_pandas(df, preserve_index=False)
        part_dir = root / f"transaction_year={year}"
        part_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, part_dir / "part-0000.parquet")


def _oracle_as_of(
    records: list[dict[str, Any]], cik: str, asof: dt.date, lookback_days: int
) -> set[str]:
    """Naive independent filter: the accession numbers ``records_as_of`` should return."""
    lo = asof - dt.timedelta(days=lookback_days)
    return {
        r["accession_number"]
        for r in records
        if r["issuer_cik"] == cik and r["filed_date"] <= asof and r["transaction_date"] >= lo
    }


def _oracle_for_person(records: list[dict[str, Any]], person: str, year_y: int) -> set[str]:
    window = set(range(year_y - 3, year_y))
    return {
        r["accession_number"]
        for r in records
        if r["reporting_owner_cik"] == person and r["transaction_date"].year in window
    }


def _accessions(frame: pd.DataFrame) -> set[str]:
    if frame.empty:
        return set()
    return set(frame["accession_number"].astype(str))


@st.composite
def _panel_and_query(draw: Any) -> tuple[list[dict[str, Any]], str, dt.date, int]:
    """A panel plus a query ``(ticker, asof, lookback)`` anchored to the panel.

    Sampling ``asof`` independently of the record dates makes the query window
    miss the data ~98% of the time — the differential oracle then passes on
    ``empty == empty`` and kills no mutants. So when the panel is non-empty,
    ``asof`` is anchored just at/after one record's ``filed_date`` (offset 0
    hits the ``filed_date <= asof`` boundary — the exact ``<=`` vs ``<`` mutant),
    and ``ticker`` is that record's issuer so its rows are live candidates.
    Other issuers' rows in the panel still exercise the issuer filter, and
    later-filed rows still exercise the look-ahead exclusion.
    """
    records = draw(_panels())
    lookback = draw(st.sampled_from(_LOOKBACKS))
    if records:
        anchor = draw(st.sampled_from(records))
        offset = draw(st.integers(0, 60))
        asof = anchor["filed_date"] + dt.timedelta(days=offset)
        ticker = anchor["ticker"]
    else:
        asof = draw(st.dates(_MIN_DATE, _MAX_DATE))
        ticker = draw(st.sampled_from([t for t, _ in _ISSUERS]))
    return records, ticker, asof, lookback


class TestRecordsAsOfPIT(PropertyTestCase):
    @settings(max_examples=80)
    @given(data=_panel_and_query())
    def test_no_look_ahead_and_lookback_bound(
        self, data: tuple[list[dict[str, Any]], str, dt.date, int]
    ) -> None:
        records, ticker, asof, lookback = data
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_panel(root, records)
            store = Form4PITStore(parquet_root=root, ticker_cik_resolver=_RESOLVER)
            result = store.records_as_of(ticker, asof=asof, lookback_days=lookback)
        lo = asof - dt.timedelta(days=lookback)
        for _, row in result.iterrows():
            # Flagship PIT invariant: nothing filed after asof may surface.
            self.assertLessEqual(row["filed_date"], asof)
            self.assertGreaterEqual(row["transaction_date"], lo)

    @settings(max_examples=80)
    @given(data=_panel_and_query())
    def test_equals_naive_filter_oracle(
        self, data: tuple[list[dict[str, Any]], str, dt.date, int]
    ) -> None:
        records, ticker, asof, lookback = data
        cik = _RESOLVER.lookup(ticker)
        assert cik is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_panel(root, records)
            store = Form4PITStore(parquet_root=root, ticker_cik_resolver=_RESOLVER)
            result = store.records_as_of(ticker, asof=asof, lookback_days=lookback)
        self.assertEqual(_accessions(result), _oracle_as_of(records, cik, asof, lookback))

    @settings(max_examples=60)
    @given(data=_panel_and_query())
    def test_result_invariant_to_partition_cache_size(
        self, data: tuple[list[dict[str, Any]], str, dt.date, int]
    ) -> None:
        """The per-year LRU cache is a pure optimization: 0/1/2/32 -> same rows."""
        records, ticker, asof, lookback = data
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_panel(root, records)
            got = []
            for size in (0, 1, 2, 32):
                store = Form4PITStore(
                    parquet_root=root,
                    ticker_cik_resolver=_RESOLVER,
                    partition_cache_size=size,
                )
                # Query twice so an eviction/move_to_end bug can corrupt the 2nd hit.
                store.records_as_of(ticker, asof=asof, lookback_days=lookback)
                got.append(
                    _accessions(store.records_as_of(ticker, asof=asof, lookback_days=lookback))
                )
        for other in got[1:]:
            self.assertEqual(got[0], other)

    @settings(max_examples=60)
    @given(data=_panel_and_query(), extra=st.integers(1, 400))
    def test_monotonic_in_lookback(
        self, data: tuple[list[dict[str, Any]], str, dt.date, int], extra: int
    ) -> None:
        """Same asof, wider lookback -> a superset of accessions (no fire-sale here)."""
        records, ticker, asof, lookback = data
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_panel(root, records)
            store = Form4PITStore(parquet_root=root, ticker_cik_resolver=_RESOLVER)
            narrow = _accessions(store.records_as_of(ticker, asof=asof, lookback_days=lookback))
            wide = _accessions(
                store.records_as_of(ticker, asof=asof, lookback_days=lookback + extra)
            )
        self.assertTrue(narrow.issubset(wide))

    @settings(max_examples=40)
    @given(data=_panel_and_query())
    def test_unresolvable_ticker_is_empty(
        self, data: tuple[list[dict[str, Any]], str, dt.date, int]
    ) -> None:
        records, _ticker, asof, lookback = data
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_panel(root, records)
            store = Form4PITStore(parquet_root=root, ticker_cik_resolver=_RESOLVER)
            # A ticker no issuer maps to -> resolver miss -> empty, never a crash.
            result = store.records_as_of("ZZZ", asof=asof, lookback_days=lookback)
        self.assertEqual(len(result), 0)


class TestRecordsAsOfFireSale(PropertyTestCase):
    @settings(max_examples=60)
    @given(data=_panel_and_query(), delist_offset=st.integers(1, 179))
    def test_delisting_within_window_forces_empty(
        self,
        data: tuple[list[dict[str, Any]], str, dt.date, int],
        delist_offset: int,
    ) -> None:
        """A delisting <= 180d AFTER asof empties the frame regardless of contents."""
        records, ticker, asof, lookback = data
        delisted = asof + dt.timedelta(days=delist_offset)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_panel(root, records)
            store = Form4PITStore(
                parquet_root=root,
                ticker_cik_resolver=_RESOLVER,
                delisting_events=[
                    DelistingEvent(ticker=ticker, delisted_date=delisted, reason="test")
                ],
            )
            result = store.records_as_of(ticker, asof=asof, lookback_days=lookback)
        self.assertEqual(len(result), 0)

    @settings(max_examples=60)
    @given(data=_panel_and_query(), delist_offset=st.integers(181, 2000))
    def test_delisting_far_future_matches_oracle(
        self,
        data: tuple[list[dict[str, Any]], str, dt.date, int],
        delist_offset: int,
    ) -> None:
        """A delisting well beyond 180d must NOT change the result vs the oracle."""
        records, ticker, asof, lookback = data
        cik = _RESOLVER.lookup(ticker)
        assert cik is not None
        delisted = asof + dt.timedelta(days=delist_offset)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_panel(root, records)
            store = Form4PITStore(
                parquet_root=root,
                ticker_cik_resolver=_RESOLVER,
                delisting_events=[
                    DelistingEvent(ticker=ticker, delisted_date=delisted, reason="test")
                ],
            )
            result = store.records_as_of(ticker, asof=asof, lookback_days=lookback)
        self.assertEqual(_accessions(result), _oracle_as_of(records, cik, asof, lookback))


class TestRecordsForPerson(PropertyTestCase):
    @settings(max_examples=80)
    @given(records=_panels(), person=st.sampled_from(_PERSONS), year_y=st.integers(2020, 2024))
    def test_equals_naive_person_window_oracle(
        self, records: list[dict[str, Any]], person: str, year_y: int
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_panel(root, records)
            store = Form4PITStore(parquet_root=root)
            result = store.records_for_person(person_cik=person, classification_year=year_y)
        got = _accessions(result)
        self.assertEqual(got, _oracle_for_person(records, person, year_y))
        # Explicit window witness: every returned txn year is strictly in [Y-3, Y).
        for _, row in result.iterrows():
            self.assertIn(row["transaction_date"].year, set(range(year_y - 3, year_y)))
