"""Tests for US venue routing + the append-only submission journal (P2 + the
FX-leg schema-2 shape: fx provenance keys, REAL nulls on same-currency, v1
line tolerance)."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from alphalens_pipeline.brokers.contract import InstrumentNotFoundError, InstrumentRef
from alphalens_pipeline.brokers.execution import execution_config_version
from alphalens_pipeline.brokers.routing import US_MIC_PROBE_ORDER, resolve_us_instrument
from alphalens_pipeline.brokers.submission_log import (
    append_submission_record,
    build_submission_record,
    iter_submission_records,
)
from alphalens_pipeline.paper.fx import FxConversion

_FX_KEYS = (
    "sizing_currency",
    "instrument_currency",
    "sizing_equity",
    "fx_rate",
    "fx_rate_bid",
    "fx_rate_ask",
    "fx_rate_price_type",
    "fx_rate_source",
    "fx_rate_asof",
    "precheck_conversion_rate",
)

_EURPLN_FX = FxConversion(
    account_currency="EUR",
    instrument_currency="PLN",
    rate=4.34,
    sizing_buffer_pct=1.0,
    source="saxo-fxspot-uic-1343-mid",
    price_type="Tradable",
    bid=4.3331,
    ask=4.3469,
    asof=dt.datetime(2026, 7, 18, 10, 0, 0, tzinfo=dt.UTC),
)


def _ref(ticker: str, mic: str) -> InstrumentRef:
    return InstrumentRef(
        ticker=ticker,
        exchange_mic=mic,
        asset_type="Stock",
        broker_instrument_id="307",
        broker_symbol=f"{ticker.lower()}:{mic.lower()}",
    )


class _RoutingStubBroker:
    """Resolves only the (ticker, mic) pairs it is seeded with."""

    name = "stub"

    def __init__(self, known: dict[tuple[str, str], InstrumentRef]):
        self.known = known
        self.resolve_calls: list[tuple[str, str]] = []

    def resolve_instrument(self, ticker: str, exchange_mic: str = "XNYS") -> InstrumentRef:
        self.resolve_calls.append((ticker, exchange_mic))
        try:
            return self.known[(ticker, exchange_mic)]
        except KeyError:
            raise InstrumentNotFoundError(f"no ({ticker}, {exchange_mic})") from None


class TestResolveUsInstrument(unittest.TestCase):
    def test_probe_order_is_xnys_then_xnas(self):
        self.assertEqual(US_MIC_PROBE_ORDER, ("XNYS", "XNAS"))

    def test_probe_xnys_then_xnas_exactly_one_match(self):
        broker = _RoutingStubBroker({("NVDA", "XNAS"): _ref("NVDA", "XNAS")})

        ref = resolve_us_instrument(broker, "NVDA")  # type: ignore[arg-type]

        self.assertEqual(ref.exchange_mic, "XNAS")
        self.assertEqual(broker.resolve_calls, [("NVDA", "XNYS"), ("NVDA", "XNAS")])

    def test_no_match_raises_instrument_not_found(self):
        broker = _RoutingStubBroker({})
        with self.assertRaises(InstrumentNotFoundError) as ctx:
            resolve_us_instrument(broker, "NOPE")  # type: ignore[arg-type]
        self.assertIn("explicit exchange MIC", str(ctx.exception))

    def test_ambiguous_both_resolve_raises(self):
        broker = _RoutingStubBroker(
            {("DUAL", "XNYS"): _ref("DUAL", "XNYS"), ("DUAL", "XNAS"): _ref("DUAL", "XNAS")}
        )
        with self.assertRaises(InstrumentNotFoundError) as ctx:
            resolve_us_instrument(broker, "DUAL")  # type: ignore[arg-type]
        self.assertIn("AMBIGUOUS", str(ctx.exception))

    def test_explicit_mic_wins_no_probe(self):
        broker = _RoutingStubBroker({("CDR", "XWAR"): _ref("CDR", "XWAR")})

        ref = resolve_us_instrument(broker, "CDR", exchange_mic="XWAR")  # type: ignore[arg-type]

        self.assertEqual(ref.exchange_mic, "XWAR")
        self.assertEqual(broker.resolve_calls, [("CDR", "XWAR")], "explicit MIC must not probe")

    def test_xwar_never_probed_implicitly(self):
        # A WSE-only listing must NOT resolve without an explicit MIC — the
        # FX leg made XWAR SIZABLE, but adding it to a probe order stays a
        # follow-up decision after the GPW first-fill experiment (FX-leg
        # memo §6).
        broker = _RoutingStubBroker({("CDR", "XWAR"): _ref("CDR", "XWAR")})
        with self.assertRaises(InstrumentNotFoundError):
            resolve_us_instrument(broker, "CDR")  # type: ignore[arg-type]
        self.assertNotIn(("CDR", "XWAR"), broker.resolve_calls)


class TestSubmissionLog(unittest.TestCase):
    def _record(self, **overrides: object) -> dict:
        defaults: dict = {
            "brief_date": "2026-07-16",
            "ticker": "KO",
            "mic": "XNYS",
            "uic": "307",
            "brackets": [
                {
                    "client_request_id": "rid-1",
                    "entry_order_id": "E-1",
                    "exit_order_ids": ["T-1", "S-1"],
                    "qty": 10,
                    "entry": 50.0,
                    "stop": 45.0,
                    "tp": 60.0,
                    "ttl": 5,
                }
            ],
        }
        defaults.update(overrides)
        return build_submission_record(**defaults)

    def test_record_stamps_execution_config_version_and_utc_ts(self):
        record = self._record()
        self.assertEqual(record["execution_config_version"], execution_config_version())
        self.assertTrue(record["execution_config_version"].startswith("execution-v2-"))
        self.assertIn("+00:00", record["ts"])
        self.assertEqual(record["mic"], "XNYS")
        self.assertEqual(record["uic"], "307")

    def test_schema_2_same_currency_writes_real_nulls_never_a_fake_rate(self):
        # The fx keys are ALWAYS present in a v2 record; same-currency (and
        # explicit-qty callers that never sized) carry REAL nulls — a fake
        # 1.0 would masquerade as a quote.
        record = self._record(sizing_currency="USD", instrument_currency="USD")
        for key in _FX_KEYS:
            self.assertIn(key, record, f"schema-2 record must carry {key}")
        self.assertEqual(record["sizing_currency"], "USD")
        self.assertEqual(record["instrument_currency"], "USD")
        self.assertIsNone(record["fx_rate"])
        self.assertIsNone(record["fx_rate_bid"])
        self.assertIsNone(record["fx_rate_price_type"])
        self.assertIsNone(record["fx_rate_source"])
        self.assertIsNone(record["fx_rate_asof"])
        self.assertIsNone(record["precheck_conversion_rate"])

    def test_schema_2_cross_currency_stamps_the_conversion_verbatim(self):
        record = self._record(
            sizing_currency="EUR",
            instrument_currency="PLN",
            sizing_equity=1_000_000.0,
            fx=_EURPLN_FX,
            precheck_conversion_rate=0.2304,
        )
        self.assertEqual(record["sizing_currency"], "EUR")
        self.assertEqual(record["instrument_currency"], "PLN")
        self.assertEqual(record["sizing_equity"], 1_000_000.0)
        self.assertEqual(record["fx_rate"], 4.34)
        self.assertEqual(record["fx_rate_bid"], 4.3331)
        self.assertEqual(record["fx_rate_ask"], 4.3469)
        self.assertEqual(record["fx_rate_price_type"], "Tradable")
        self.assertEqual(record["fx_rate_source"], "saxo-fxspot-uic-1343-mid")
        self.assertEqual(record["fx_rate_asof"], "2026-07-18T10:00:00+00:00")
        self.assertEqual(record["precheck_conversion_rate"], 0.2304)

    def test_schema_2_round_trips_through_the_jsonl_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "submissions.jsonl"
            append_submission_record(
                self._record(
                    sizing_currency="EUR",
                    instrument_currency="PLN",
                    sizing_equity=1_000_000.0,
                    fx=_EURPLN_FX,
                    precheck_conversion_rate=0.2304,
                ),
                path=target,
            )
            (record,) = list(iter_submission_records(target))
        self.assertEqual(record["fx_rate"], 4.34)
        self.assertEqual(record["fx_rate_asof"], "2026-07-18T10:00:00+00:00")

    def test_v1_lines_without_fx_keys_still_read_cleanly(self):
        # Forward compat: schema-1 journal lines (the same-currency no-op
        # era) simply LACK the fx keys — the reader yields them untouched,
        # never fails, never back-fills.
        v1_line = {
            "execution_config_version": "execution-v1-abcdef012345",
            "ts": "2026-07-17T18:00:00+00:00",
            "brief_date": "2026-07-16",
            "ticker": "KO",
            "mic": "XNYS",
            "uic": "307",
            "brackets": [{"client_request_id": "rid-1", "entry_order_id": "E-1", "ttl": 5}],
            "precheck": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "submissions.jsonl"
            with target.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(v1_line) + "\n")
            append_submission_record(self._record(), path=target)

            records = list(iter_submission_records(target))

        self.assertEqual(len(records), 2)
        self.assertNotIn("fx_rate", records[0], "v1 line passes through untouched")
        self.assertIn("fx_rate", records[1])

    def test_append_is_jsonl_append_only_and_creates_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "submissions.jsonl"

            append_submission_record(self._record(), path=target)
            append_submission_record(self._record(note="second run"), path=target)

            lines = target.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        first, second = (json.loads(line) for line in lines)
        self.assertEqual(first["ticker"], "KO")
        self.assertNotIn("note", first)
        self.assertEqual(second["note"], "second run")
        self.assertEqual(second["brackets"][0]["exit_order_ids"], ["T-1", "S-1"])


class TestIterSubmissionRecords(unittest.TestCase):
    """P3 journal reader: yield parsed records, skip-and-collect malformed lines."""

    def _sample_record(self) -> dict:
        return build_submission_record(
            brief_date="2026-07-16",
            ticker="KO",
            mic="XNYS",
            uic="307",
            brackets=[{"client_request_id": "rid-1", "entry_order_id": "E-1", "ttl": 5}],
        )

    def test_round_trips_appended_records_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "submissions.jsonl"
            append_submission_record(self._sample_record(), path=target)
            append_submission_record(self._sample_record() | {"ticker": "NVDA"}, path=target)

            records = list(iter_submission_records(target))

        self.assertEqual([record["ticker"] for record in records], ["KO", "NVDA"])
        self.assertEqual(records[0]["brackets"][0]["entry_order_id"], "E-1")

    def test_malformed_lines_are_skipped_and_collected_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "submissions.jsonl"
            append_submission_record(self._sample_record(), path=target)
            with target.open("a", encoding="utf-8") as fh:
                fh.write("{not json at all\n")
                fh.write('"a bare string is not a record"\n')
                fh.write("\n")  # blank line: ignored, NOT malformed
            append_submission_record(self._sample_record(), path=target)

            malformed: list[str] = []
            records = list(iter_submission_records(target, malformed=malformed))

        self.assertEqual(len(records), 2, "good records around the bad lines must survive")
        self.assertEqual(len(malformed), 2, "both malformed lines collected, blank line ignored")

    def test_missing_journal_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope" / "submissions.jsonl"
            self.assertEqual(list(iter_submission_records(missing)), [])


if __name__ == "__main__":
    unittest.main()
