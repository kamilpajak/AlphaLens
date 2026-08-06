"""Every mapper proposal is recorded with the reason it did or did not survive.

The market-cap bracket is the largest sink in the candidate funnel — on 2026-08-05
it dropped 17 of 19 proposals — and it used to discard the names it rejected. The
per-theme log line carried counts only, so "were the dropped names bad, or merely
the wrong size?" could only be answered by re-running the whole funnel offline
against live yfinance. These tests pin the pre-bracket funnel parquet that makes
the question answerable from disk.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.thematic.mapping import orchestrator
from alphalens_pipeline.thematic.mapping.catalyst_contract import CatalystPayload
from alphalens_pipeline.thematic.mapping.theme_mapper import MapperOutcome
from alphalens_pipeline.thematic.verification import mcap_filter

ASOF = dt.date(2026, 8, 5)
MIN_CAP, MAX_CAP = 500_000_000, 10_000_000_000


def _catalyst() -> CatalystPayload:
    return CatalystPayload(
        url="https://example.com/ibm-quantum",
        title="Quantum computing nears commercial breakthrough, IBM CEO says",
        published_at="2026-07-31",
        event_type="product_launch",
        primary_entities=["IBM"],
        confidence=0.8,
        second_order_implications=[],
        echo_count=1,
        trigger_url="https://example.com/ibm-quantum",
        trigger_published_at="2026-07-31",
        is_amplified=False,
        template_id=None,
        template_facts=None,
    )


def _proposal(candidates: list[dict]) -> dict:
    return {
        "candidates": candidates,
        "search_keywords": [],
        "outcome": MapperOutcome.SUCCESS,
        "no_candidates_reason": "",
    }


class ProposalFunnelSinkTests(unittest.TestCase):
    """``_propose_and_filter_candidates`` records every proposal, not just survivors."""

    def _run(self, candidates: list[dict], mcaps: dict[str, float | None]):
        sink: list[dict] = []
        with (
            mock.patch.object(
                orchestrator.theme_mapper, "propose_candidates", return_value=_proposal(candidates)
            ),
            mock.patch.object(
                orchestrator.mcap_filter, "fetch_mcap", side_effect=lambda t, **_: mcaps.get(t)
            ),
        ):
            kept, _in_bracket, _kw, _outcome = orchestrator._propose_and_filter_candidates(
                theme="quantum_computing",
                catalyst=_catalyst(),
                api_key="k",
                pro_client=None,
                min_cap=MIN_CAP,
                max_cap=MAX_CAP,
                asof=ASOF,
                funnel_sink=sink,
            )
        return kept, sink

    def test_records_one_entry_per_proposal_including_the_dropped_ones(self):
        kept, sink = self._run(
            [
                {"ticker": "QUBT", "confidence": 0.7},
                {"ticker": "NVDA", "confidence": 0.9},
                {"ticker": "TINY", "confidence": 0.4},
                {"ticker": "DEAD", "confidence": 0.3},
            ],
            {
                "QUBT": 1_827_000_000,
                "NVDA": 4_000_000_000_000,
                "TINY": 100_000_000,
                "DEAD": None,
            },
        )
        self.assertEqual([c["ticker"] for c in kept], ["QUBT"])
        self.assertEqual([r["ticker"] for r in sink], ["QUBT", "NVDA", "TINY", "DEAD"])
        self.assertEqual(
            [r["bracket_verdict"] for r in sink],
            [
                mcap_filter.IN_BRACKET,
                mcap_filter.TOO_BIG,
                mcap_filter.TOO_SMALL,
                mcap_filter.NO_MCAP,
            ],
        )

    def test_carries_the_catalyst_so_a_proposal_is_traceable_to_its_event(self):
        _kept, sink = self._run([{"ticker": "QUBT", "confidence": 0.7}], {"QUBT": 1_827_000_000})
        row = sink[0]
        self.assertEqual(row["theme"], "quantum_computing")
        self.assertEqual(row["catalyst_url"], "https://example.com/ibm-quantum")
        self.assertEqual(row["catalyst_event_type"], "product_launch")
        self.assertEqual(row["market_cap"], 1_827_000_000)
        self.assertEqual(row["llm_confidence"], 0.7)

    def test_a_decline_records_nothing_rather_than_an_empty_row(self):
        sink: list[dict] = []
        with mock.patch.object(
            orchestrator.theme_mapper,
            "propose_candidates",
            return_value={
                "candidates": [],
                "search_keywords": [],
                "outcome": MapperOutcome.DECLINED,
                "no_candidates_reason": "no transmission channel",
            },
        ):
            orchestrator._propose_and_filter_candidates(
                theme="quantum_computing",
                catalyst=_catalyst(),
                api_key="k",
                pro_client=None,
                min_cap=MIN_CAP,
                max_cap=MAX_CAP,
                asof=ASOF,
                funnel_sink=sink,
            )
        self.assertEqual(sink, [])

    def test_the_sink_is_optional_so_existing_callers_are_unaffected(self):
        with (
            mock.patch.object(
                orchestrator.theme_mapper,
                "propose_candidates",
                return_value=_proposal([{"ticker": "QUBT", "confidence": 0.7}]),
            ),
            mock.patch.object(orchestrator.mcap_filter, "fetch_mcap", return_value=1_827_000_000),
        ):
            kept, _in_bracket, _kw, _outcome = orchestrator._propose_and_filter_candidates(
                theme="quantum_computing",
                catalyst=_catalyst(),
                api_key="k",
                pro_client=None,
                min_cap=MIN_CAP,
                max_cap=MAX_CAP,
                asof=ASOF,
            )
        self.assertEqual([c["ticker"] for c in kept], ["QUBT"])

    def test_logs_which_tickers_were_dropped_not_only_how_many(self):
        with self.assertLogs(
            "alphalens_pipeline.thematic.mapping.orchestrator", level="INFO"
        ) as cm:
            self._run(
                [{"ticker": "QUBT", "confidence": 0.7}, {"ticker": "NVDA", "confidence": 0.9}],
                {"QUBT": 1_827_000_000, "NVDA": 4_000_000_000_000},
            )
        logs = "\n".join(cm.output)
        self.assertIn("NVDA", logs)
        self.assertIn("too_big", logs)

    def test_the_dropped_ticker_list_in_the_log_is_capped(self):
        # The list is bounded today only because _MAX_CANDIDATES happens to be 15
        # in another module. Cap it here so raising that constant cannot silently
        # turn one INFO line into a wall of text.
        n = orchestrator._MAX_LOGGED_DROPPED_TICKERS + 5
        tickers = [f"T{i:02d}" for i in range(n)]
        with self.assertLogs(
            "alphalens_pipeline.thematic.mapping.orchestrator", level="INFO"
        ) as cm:
            self._run(
                [{"ticker": t, "confidence": 0.5} for t in tickers],
                dict.fromkeys(tickers, 4_000_000_000_000.0),
            )
        logs = "\n".join(cm.output)
        self.assertIn(f"+{5} more", logs)
        self.assertNotIn(tickers[-1], logs)

    def test_a_ticker_proposed_twice_gets_its_own_verdict_per_row(self):
        # Verdicts are attached POSITIONALLY. A dict keyed by ticker would collapse
        # the two rows onto the last verdict, so a duplicate proposal whose two
        # mcap lookups disagreed (cache write between calls, PIT->live fallback
        # firing once) would write a verdict that never applied to the first row.
        seen: list[str] = []

        def _drifting_mcap(ticker, **_):
            seen.append(ticker)
            # Second lookup of DUP comes back out of bracket.
            return 1_000_000_000.0 if seen.count(ticker) == 1 else 4_000_000_000_000.0

        sink: list[dict] = []
        with (
            mock.patch.object(
                orchestrator.theme_mapper,
                "propose_candidates",
                return_value=_proposal(
                    [{"ticker": "DUP", "confidence": 0.7}, {"ticker": "DUP", "confidence": 0.6}]
                ),
            ),
            mock.patch.object(orchestrator.mcap_filter, "fetch_mcap", side_effect=_drifting_mcap),
        ):
            orchestrator._propose_and_filter_candidates(
                theme="quantum_computing",
                catalyst=_catalyst(),
                api_key="k",
                pro_client=None,
                min_cap=MIN_CAP,
                max_cap=MAX_CAP,
                asof=ASOF,
                funnel_sink=sink,
            )
        self.assertEqual(len(sink), 2)
        self.assertEqual(
            [r["bracket_verdict"] for r in sink], [mcap_filter.IN_BRACKET, mcap_filter.TOO_BIG]
        )

    def test_missing_confidence_matches_the_candidates_parquet_default(self):
        # _build_row uses cand.get("confidence", 0.0); the funnel must not report
        # null for the same omission or the two files disagree on what happened.
        _kept, sink = self._run([{"ticker": "QUBT"}], {"QUBT": 1_827_000_000})
        self.assertEqual(sink[0]["llm_confidence"], 0.0)


class ProposalFunnelParquetTests(unittest.TestCase):
    """``map_themes`` persists the funnel next to the candidates it explains."""

    def _map(self, out: Path, sink_rows: list[dict]):
        def _fake_propose(**kwargs):
            kwargs["funnel_sink"].extend(sink_rows)
            return ([], {}, [], MapperOutcome.SUCCESS)

        with (
            mock.patch.object(orchestrator, "_resolve_catalyst", return_value=_catalyst()),
            mock.patch.object(
                orchestrator, "_propose_and_filter_candidates", side_effect=_fake_propose
            ),
            mock.patch.object(orchestrator, "_init_pro_client"),
            mock.patch.object(orchestrator, "_fetch_press_window", return_value=None),
        ):
            return orchestrator.map_themes(themes=["quantum_computing"], asof=ASOF, output_dir=out)

    def test_writes_one_row_per_proposal_including_names_the_bracket_dropped(self):
        rows = [
            {
                "theme": "quantum_computing",
                "ticker": "QUBT",
                "company_name": "Quantum Computing Inc",
                "llm_confidence": 0.7,
                "transmission_channel": "IBM signals viability -> sector re-rates",
                "market_cap": 1_827_000_000.0,
                "bracket_verdict": mcap_filter.IN_BRACKET,
                "catalyst_url": "https://example.com/ibm-quantum",
                "catalyst_event_type": "product_launch",
            },
            {
                "theme": "quantum_computing",
                "ticker": "NVDA",
                "company_name": "NVIDIA",
                "llm_confidence": 0.9,
                "transmission_channel": "supplies accelerators",
                "market_cap": 4_000_000_000_000.0,
                "bracket_verdict": mcap_filter.TOO_BIG,
                "catalyst_url": "https://example.com/ibm-quantum",
                "catalyst_event_type": "product_launch",
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._map(out, rows)
            path = out / "proposal_funnel" / f"{ASOF.isoformat()}.parquet"
            self.assertTrue(path.exists(), "the funnel parquet must be written next to candidates")
            df = pd.read_parquet(path)
        self.assertEqual(list(df["ticker"]), ["QUBT", "NVDA"])
        self.assertEqual(list(df["bracket_verdict"]), [mcap_filter.IN_BRACKET, mcap_filter.TOO_BIG])
        self.assertEqual(list(df["asof"].astype(str)), [ASOF.isoformat()] * 2)
        # The freeze fingerprint travels with the funnel so a replay can tell which
        # mapper config produced these proposals.
        self.assertEqual(df["mapper_config_version"].nunique(), 1)

    def test_a_day_with_no_proposals_writes_no_funnel_file(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._map(out, [])
            self.assertFalse((out / "proposal_funnel" / f"{ASOF.isoformat()}.parquet").exists())

    def test_a_funnel_write_failure_never_aborts_the_daily_build(self):
        rows = [
            {
                "theme": "quantum_computing",
                "ticker": "QUBT",
                "company_name": "Quantum Computing Inc",
                "llm_confidence": 0.7,
                "transmission_channel": "",
                "market_cap": 1_827_000_000.0,
                "bracket_verdict": mcap_filter.IN_BRACKET,
                "catalyst_url": "https://example.com/ibm-quantum",
                "catalyst_event_type": "product_launch",
            }
        ]
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            with mock.patch.object(
                orchestrator, "write_parquet_atomic", side_effect=_fail_on_funnel
            ):
                df = self._map(out, rows)
        # Telemetry is best-effort: the candidates frame is still returned.
        self.assertIsInstance(df, pd.DataFrame)


def _fail_on_funnel(frame, path, **kwargs):
    """Blow up only on the funnel write, so the candidates parquet still succeeds."""
    if "proposal_funnel" in str(path):
        raise OSError("disk full")


if __name__ == "__main__":
    unittest.main()
