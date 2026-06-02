"""Issue #395 (option b) -- subject-match template catalyst lookup.

``brief_template_id`` was ~always empty in prod (0/168 on 2026-06-01) because
the template extraction path stamps ``themes=[]`` (event_extractor.py), while
the only catalyst lookup (``find_trigger_event``) matches events by theme-
membership. A template event therefore never matched any theme -> the catalyst
dict never carried ``template_id`` -> ``brief_template_id`` stayed empty.

Option (b) is the honest narrow fix: when a SCORED candidate's ticker IS the
subject (``primary_entities``) of a template-extracted event inside the catalyst
window, that event's typed facts attach to THAT candidate's brief -- independent
of theme. It deliberately does NOT bridge a filer's facts to a different
beneficiary (that is option-c / issue #394).
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.thematic.argumentation import generator, orchestrator
from alphalens_pipeline.thematic.mapping import catalyst_resolver
from alphalens_pipeline.thematic.screening import scorer


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _events_row(
    news_id: str,
    *,
    event_type: str,
    primary_entities: list[str],
    themes: list[str],
    extraction_method: str,
    template_id: str | None,
    template_fields_json: str | None = None,
    confidence: float = 0.9,
) -> dict:
    return {
        "news_id": news_id,
        "event_type": event_type,
        "primary_entities": primary_entities,
        "themes": themes,
        "sentiment": "positive",
        "second_order_implications": [],
        "confidence": confidence,
        "model": "deepseek/deepseek-v4-flash",
        "extracted_at": pd.Timestamp("2026-05-30T10:00:00Z"),
        "extraction_method": extraction_method,
        "template_id": template_id,
        "template_fields_json": template_fields_json,
    }


def _news_row(news_id: str, timestamp: str, title: str, url: str, tickers: list[str]) -> dict:
    return {
        "id": news_id,
        "source": "businesswire" if "businesswire" in url else "polygon",
        "timestamp": pd.Timestamp(timestamp),
        "tickers": tickers,
        "title": title,
        "body": "",
        "url": url,
        "keywords": [],
        "extra": "{}",
    }


def _write_window(events: pd.DataFrame, news: pd.DataFrame, tmp: str, date: str):
    events_dir = Path(tmp) / "events"
    news_dir = Path(tmp) / "news"
    events_dir.mkdir(exist_ok=True)
    news_dir.mkdir(exist_ok=True)
    events.to_parquet(events_dir / f"{date}.parquet", index=False)
    news.to_parquet(news_dir / f"{date}.parquet", index=False)
    return events_dir, news_dir


def _find(ticker, events_dir, news_dir, *, lookback_days=1, asof=dt.date(2026, 5, 30)):
    return catalyst_resolver.find_template_catalyst_for_ticker(
        ticker=ticker,
        asof=asof,
        events_dir=events_dir,
        news_dir=news_dir,
        lookback_days=lookback_days,
    )


# --------------------------------------------------------------------------- #
# 1 + 2 -- the subject-match lookup, in isolation
# --------------------------------------------------------------------------- #
class TestFindTemplateCatalystForTicker(unittest.TestCase):
    def setUp(self):
        catalyst_resolver._load_window.cache_clear()
        catalyst_resolver._load_url_blocklist_patterns.cache_clear()

    def test_ticker_is_template_subject_returns_payload(self):
        fields = {"reporting_ticker": "CELH", "eps_surprise_pct": 12.5}
        events = pd.DataFrame(
            [
                _events_row(
                    "bw:1",
                    event_type="earnings",
                    primary_entities=["CELH"],
                    themes=[],
                    extraction_method="template",
                    template_id="earnings_surprise",
                    template_fields_json=json.dumps(fields),
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "bw:1",
                    "2026-05-30T08:00:00Z",
                    "Celsius reports Q1",
                    "https://www.businesswire.com/celh",
                    ["CELH"],
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            ed, nd = _write_window(events, news, tmp, "2026-05-30")
            result = _find("CELH", ed, nd)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["template_id"], "earnings_surprise")
        self.assertEqual(result["template_facts"], fields)

    def test_lowercase_ticker_matches_uppercase_entity(self):
        events = pd.DataFrame(
            [
                _events_row(
                    "bw:1",
                    event_type="m_and_a",
                    primary_entities=["CUBE"],
                    themes=[],
                    extraction_method="template",
                    template_id="m_and_a_press_release",
                    template_fields_json=json.dumps({"acquirer_ticker": "CUBE"}),
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "bw:1", "2026-05-30T08:00:00Z", "x", "https://www.businesswire.com/x", ["CUBE"]
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            ed, nd = _write_window(events, news, tmp, "2026-05-30")
            self.assertIsNotNone(_find("  cube ", ed, nd))

    def test_dot_dash_symbol_normalization_matches(self):
        # Feed emits BRK.B; candidate ticker is BRK-B. Must match (separator
        # difference must not silently miss).
        events = pd.DataFrame(
            [
                _events_row(
                    "bw:1",
                    event_type="earnings",
                    primary_entities=["BRK.B"],
                    themes=[],
                    extraction_method="template",
                    template_id="earnings_surprise",
                    template_fields_json=json.dumps({"reporting_ticker": "BRK.B"}),
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "bw:1", "2026-05-30T08:00:00Z", "x", "https://www.businesswire.com/x", ["BRK.B"]
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            ed, nd = _write_window(events, news, tmp, "2026-05-30")
            self.assertIsNotNone(_find("BRK-B", ed, nd))

    def test_ticker_not_a_template_subject_returns_none(self):
        events = pd.DataFrame(
            [
                _events_row(
                    "bw:1",
                    event_type="earnings",
                    primary_entities=["AROW"],
                    themes=[],
                    extraction_method="template",
                    template_id="earnings_surprise",
                    template_fields_json=json.dumps({"reporting_ticker": "AROW"}),
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "bw:1", "2026-05-30T08:00:00Z", "x", "https://www.businesswire.com/x", ["AROW"]
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            ed, nd = _write_window(events, news, tmp, "2026-05-30")
            self.assertIsNone(_find("CHWY", ed, nd))

    def test_flash_event_naming_ticker_is_ignored(self):
        events = pd.DataFrame(
            [
                _events_row(
                    "p:1",
                    event_type="m_and_a",
                    primary_entities=["CELH"],
                    themes=["beverages"],
                    extraction_method="flash",
                    template_id=None,
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row("p:1", "2026-05-30T08:00:00Z", "x", "https://polygon.io/x", ["CELH"]),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            ed, nd = _write_window(events, news, tmp, "2026-05-30")
            self.assertIsNone(_find("CELH", ed, nd))

    def test_template_event_outside_window_returns_none(self):
        events = pd.DataFrame(
            [
                _events_row(
                    "bw:1",
                    event_type="earnings",
                    primary_entities=["CELH"],
                    themes=[],
                    extraction_method="template",
                    template_id="earnings_surprise",
                    template_fields_json=json.dumps({"reporting_ticker": "CELH"}),
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "bw:1", "2026-05-25T08:00:00Z", "x", "https://www.businesswire.com/x", ["CELH"]
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            ed, nd = _write_window(events, news, tmp, "2026-05-25")
            self.assertIsNone(_find("CELH", ed, nd, lookback_days=1, asof=dt.date(2026, 5, 30)))

    def test_noise_event_type_not_indexed(self):
        # An event whose type is in NOISE_EVENT_TYPES must not surface -- subject
        # path can't surface an event the theme path would drop.
        from alphalens_pipeline.thematic.extraction.schema import NOISE_EVENT_TYPES

        noise_type = sorted(NOISE_EVENT_TYPES)[0]
        events = pd.DataFrame(
            [
                _events_row(
                    "bw:1",
                    event_type=noise_type,
                    primary_entities=["CELH"],
                    themes=[],
                    extraction_method="template",
                    template_id="earnings_surprise",
                    template_fields_json=json.dumps({"reporting_ticker": "CELH"}),
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "bw:1", "2026-05-30T08:00:00Z", "x", "https://www.businesswire.com/x", ["CELH"]
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            ed, nd = _write_window(events, news, tmp, "2026-05-30")
            self.assertIsNone(_find("CELH", ed, nd))

    def test_two_template_events_same_ticker_richest_wins(self):
        sparse = {"reporting_ticker": "CELH"}
        rich = {"reporting_ticker": "CELH", "eps_surprise_pct": 12.5, "revenue_usd": 9_000_000_000}
        events = pd.DataFrame(
            [
                _events_row(
                    "bw:sparse",
                    event_type="earnings",
                    primary_entities=["CELH"],
                    themes=[],
                    extraction_method="template",
                    template_id="earnings_surprise",
                    template_fields_json=json.dumps(sparse),
                ),
                # Different template_id so PR-4 dedup keeps both (distinct cluster keys).
                _events_row(
                    "bw:rich",
                    event_type="m_and_a",
                    primary_entities=["CELH"],
                    themes=[],
                    extraction_method="template",
                    template_id="m_and_a_press_release",
                    template_fields_json=json.dumps(rich),
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "bw:sparse",
                    "2026-05-30T14:00:00Z",
                    "later sparse",
                    "https://www.businesswire.com/a",
                    ["CELH"],
                ),
                _news_row(
                    "bw:rich",
                    "2026-05-30T08:00:00Z",
                    "earlier rich",
                    "https://www.businesswire.com/b",
                    ["CELH"],
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            ed, nd = _write_window(events, news, tmp, "2026-05-30")
            result = _find("CELH", ed, nd)
        self.assertIsNotNone(result)
        assert result is not None
        # Richness beats recency: the rich payload wins though its news is earlier.
        self.assertEqual(result["template_facts"]["eps_surprise_pct"], 12.5)
        self.assertIn("revenue_usd", result["template_facts"])

    def test_multi_outlet_echo_collapses_before_index(self):
        # 3 outlets, same template event on CELH -> dedup keeps ONE; the index
        # must hold a single payload, carrying the richest extract.
        rich = {"acquirer_ticker": "CELH", "target_ticker": "ZZZ", "deal_value_usd": 5_000_000_000}
        events = pd.DataFrame(
            [
                _events_row(
                    f"bw:{i}",
                    event_type="m_and_a",
                    primary_entities=["CELH", "ZZZ"],
                    themes=[],
                    extraction_method="template",
                    template_id="m_and_a_press_release",
                    template_fields_json=json.dumps(
                        rich if i == 1 else {"acquirer_ticker": "CELH"}
                    ),
                )
                for i in range(3)
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    f"bw:{i}",
                    f"2026-05-30T0{i}:00:00Z",
                    f"outlet {i}",
                    f"https://www.businesswire.com/{i}",
                    ["CELH", "ZZZ"],
                )
                for i in range(3)
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            ed, nd = _write_window(events, news, tmp, "2026-05-30")
            index = catalyst_resolver.build_template_entity_index(
                asof=dt.date(2026, 5, 30), events_dir=ed, news_dir=nd, lookback_days=1
            )
        self.assertEqual(len(index["CELH"]), 1)
        self.assertEqual(index["CELH"][0]["template_facts"]["deal_value_usd"], 5_000_000_000)

    def test_empty_window_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            ed = Path(tmp) / "events"
            nd = Path(tmp) / "news"
            ed.mkdir()
            nd.mkdir()
            self.assertIsNone(_find("CELH", ed, nd))

    def test_missing_extraction_method_column_returns_empty_index(self):
        # Legacy parquet without extraction_method -> no subject path, no crash.
        events = pd.DataFrame(
            [
                {
                    "news_id": "bw:1",
                    "event_type": "earnings",
                    "primary_entities": ["CELH"],
                    "themes": [],
                    "confidence": 0.9,
                    "second_order_implications": [],
                }
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "bw:1", "2026-05-30T08:00:00Z", "x", "https://www.businesswire.com/x", ["CELH"]
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            ed, nd = _write_window(events, news, tmp, "2026-05-30")
            self.assertEqual(
                catalyst_resolver.build_template_entity_index(
                    asof=dt.date(2026, 5, 30), events_dir=ed, news_dir=nd, lookback_days=1
                ),
                {},
            )
            self.assertIsNone(_find("CELH", ed, nd))


# --------------------------------------------------------------------------- #
# Provenance guard -- option-b / #394 boundary
# --------------------------------------------------------------------------- #
class TestProvenanceGuardSubjectOnly(unittest.TestCase):
    def setUp(self):
        catalyst_resolver._load_window.cache_clear()
        catalyst_resolver._load_url_blocklist_patterns.cache_clear()

    def test_filer_facts_not_returned_for_non_subject_ticker(self):
        events = pd.DataFrame(
            [
                _events_row(
                    "bw:1",
                    event_type="m_and_a",
                    primary_entities=["EPRT"],
                    themes=[],
                    extraction_method="template",
                    template_id="m_and_a_press_release",
                    template_fields_json=json.dumps(
                        {"acquirer_ticker": "EPRT", "target_ticker": "ZZZ"}
                    ),
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "bw:1",
                    "2026-05-30T08:00:00Z",
                    "EPRT buys ZZZ",
                    "https://www.businesswire.com/eprt",
                    ["EPRT"],
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            ed, nd = _write_window(events, news, tmp, "2026-05-30")
            for non_subject in ("PSN", "CHWY", "AAPL"):
                self.assertIsNone(
                    _find(non_subject, ed, nd), f"{non_subject} must not inherit EPRT's facts"
                )

    def test_multi_entity_template_event_matches_each_named_subject_only(self):
        events = pd.DataFrame(
            [
                _events_row(
                    "bw:1",
                    event_type="m_and_a",
                    primary_entities=["NVDA", "ARM"],
                    themes=[],
                    extraction_method="template",
                    template_id="m_and_a_press_release",
                    template_fields_json=json.dumps(
                        {"acquirer_ticker": "NVDA", "target_ticker": "ARM"}
                    ),
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "bw:1",
                    "2026-05-30T08:00:00Z",
                    "NVDA acquires ARM",
                    "https://www.businesswire.com/x",
                    ["NVDA", "ARM"],
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            ed, nd = _write_window(events, news, tmp, "2026-05-30")
            self.assertIsNotNone(_find("NVDA", ed, nd))
            self.assertIsNotNone(_find("ARM", ed, nd))
            self.assertIsNone(_find("AMD", ed, nd))


# --------------------------------------------------------------------------- #
# Scorer wiring -- patch the catalyst_resolver MODULE (scorer lazy-imports it,
# so scorer.catalyst_resolver is not a module attribute to patch).
# --------------------------------------------------------------------------- #
class TestScorerWiresSubjectMatchLookup(unittest.TestCase):
    def _empty_signal(self):
        return {
            "score_usd": 0.0,
            "sector_percentile": 50.0,
            "yield_pct": None,
            "composite_sector_percentile": 50.0,
            "pe": None,
            "ps": None,
            "ev_rev": None,
            "fcf_margin": None,
            "financials_publish_date": None,
            "financials_age_days": None,
            "rsi": 50.0,
            "ma50_distance_pct": 0.0,
            "atr_pct": 0.0,
            "volume_zscore": 0.0,
            "summary": "n/a",
        }

    def test_scorer_attaches_subject_match_template_facts(self):
        candidates = pd.DataFrame(
            [
                {"ticker": "CELH", "theme": "beverages", "company_name": "Celsius"},
                {"ticker": "CHWY", "theme": "beverages", "company_name": "Chewy"},
            ]
        )

        def _fake_lookup(*, ticker, asof, entity_index=None, **_):
            if ticker.upper() == "CELH":
                return {
                    "template_id": "earnings_surprise",
                    "template_facts": {"reporting_ticker": "CELH", "eps_surprise_pct": 12.5},
                }
            return None

        sig = self._empty_signal()
        with (
            patch.object(catalyst_resolver, "build_template_entity_index", return_value={}),
            patch.object(
                catalyst_resolver, "find_template_catalyst_for_ticker", side_effect=_fake_lookup
            ),
            patch.object(catalyst_resolver, "find_trigger_event", return_value=None),
            patch.object(scorer, "_score_signals", return_value=(sig, sig, sig, sig)),
            patch.object(scorer, "_build_feature_fetcher", return_value=lambda *_a, **_k: {}),
            patch.object(scorer, "_build_ohlcv_loader", return_value=lambda *_a, **_k: None),
            patch.object(scorer.sector_peers, "get_industry_id", return_value=None),
        ):
            out = scorer.score_candidates(candidates, asof=dt.date(2026, 5, 30))

        celh = out[out["ticker"] == "CELH"].iloc[0]
        chwy = out[out["ticker"] == "CHWY"].iloc[0]
        self.assertEqual(celh["catalyst_template_id"], "earnings_surprise")
        self.assertEqual(json.loads(celh["catalyst_template_facts_json"])["eps_surprise_pct"], 12.5)
        self.assertTrue(
            chwy["catalyst_template_id"] is None or pd.isna(chwy["catalyst_template_id"])
        )

    def test_subject_match_does_not_override_theme_catalyst_strength(self):
        # Augment-only guard: a theme Flash catalyst sets event_type; the subject
        # template event stamps ONLY template fields.
        candidates = pd.DataFrame(
            [{"ticker": "CELH", "theme": "beverages", "company_name": "Celsius"}]
        )
        theme_event = {
            "event_type": "partnership",
            "confidence": 0.8,
            "template_id": None,
            "template_facts": None,
            "url": "https://polygon.io/celh",
        }

        def _fake_subject(*, ticker, asof, entity_index=None, **_):
            return {
                "template_id": "earnings_surprise",
                "template_facts": {"reporting_ticker": "CELH"},
            }

        sig = self._empty_signal()
        with (
            patch.object(catalyst_resolver, "build_template_entity_index", return_value={}),
            patch.object(
                catalyst_resolver, "find_template_catalyst_for_ticker", side_effect=_fake_subject
            ),
            patch.object(catalyst_resolver, "find_trigger_event", return_value=theme_event),
            patch.object(scorer, "_score_signals", return_value=(sig, sig, sig, sig)),
            patch.object(scorer, "_build_feature_fetcher", return_value=lambda *_a, **_k: {}),
            patch.object(scorer, "_build_ohlcv_loader", return_value=lambda *_a, **_k: None),
            patch.object(scorer.sector_peers, "get_industry_id", return_value=None),
        ):
            out = scorer.score_candidates(candidates, asof=dt.date(2026, 5, 30))
        row = out.iloc[0]
        # template fields come from the subject event ...
        self.assertEqual(row["catalyst_template_id"], "earnings_surprise")
        # ... but event_type stays the theme catalyst's (NOT overridden).
        self.assertEqual(row["catalyst_event_type"], "partnership")


# --------------------------------------------------------------------------- #
# End-to-end: subject-stamped scored row -> non-empty brief_template_id
# --------------------------------------------------------------------------- #
def _scored_row(*, ticker, theme, template_id=None, template_facts=None):
    return {
        "theme": theme,
        "ticker": ticker,
        "company_name": f"{ticker} Inc",
        "rationale": "x",
        "llm_confidence": 0.9,
        "market_cap": 1.0e9,
        "gates_passed_str": "tenk,press",
        "verified": True,
        "industry_id": 101001,
        "industry_name": "Computer Hardware",
        "sector_name": "Technology",
        "technicals_summary_str": "RSI 55",
        "layer4_weighted_score": 4,
        "catalyst_template_id": template_id,
        "catalyst_template_facts_json": (
            json.dumps(template_facts) if template_facts is not None else None
        ),
        "source_event_published_at": None,
    }


_FAKE_BRIEF = {
    "tldr": "tldr",
    "supply_chain_reasoning": "reasoning",
    "bear_summary": "bear",
    "catalyst_failure_exit": "exit",
    "model_used": generator.PRO_MODEL,
}


class TestEndToEndSubjectMatchPopulatesBriefTemplateId(unittest.TestCase):
    def test_candidate_that_is_template_subject_gets_brief_template_id(self):
        scored = pd.DataFrame(
            [
                _scored_row(
                    ticker="CELH",
                    theme="beverages",
                    template_id="earnings_surprise",
                    template_facts={"reporting_ticker": "CELH", "eps_surprise_pct": 12.5},
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(orchestrator, "_brief_for_row", return_value=(_FAKE_BRIEF, None)):
                out = orchestrator.generate_briefs(
                    scored, asof=dt.date(2026, 5, 31), output_dir=Path(tmp), api_key="testkey"
                )
        row = out.iloc[0]
        self.assertEqual(row["brief_template_id"], "earnings_surprise")
        self.assertEqual(json.loads(row["brief_template_facts_json"])["eps_surprise_pct"], 12.5)

    def test_theme_only_beneficiary_keeps_brief_template_id_empty(self):
        scored = pd.DataFrame(
            [
                _scored_row(
                    ticker="CHWY", theme="beverages", template_id=None, template_facts=None
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(orchestrator, "_brief_for_row", return_value=(_FAKE_BRIEF, None)):
                out = orchestrator.generate_briefs(
                    scored, asof=dt.date(2026, 5, 31), output_dir=Path(tmp), api_key="testkey"
                )
        row = out.iloc[0]
        self.assertTrue(row["brief_template_id"] is None or pd.isna(row["brief_template_id"]))


if __name__ == "__main__":
    unittest.main()
