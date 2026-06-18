"""Hermetic integration test for the catalyst source-gate SELECTION cascade.

The resolver-unit tests in ``tests/thematic/mapping/test_catalyst_resolver.py``
pin that :func:`catalyst_resolver.find_trigger_event` returns ``None`` for an
entity-less state-media event and a payload for a legit entity-less event. What
they do NOT cover is the consequence of that verdict ONE LAYER UP: the
``map_themes`` orchestrator's per-theme loop SKIPS a theme whose
``_resolve_catalyst`` returns ``None`` (logging "no catalyst event in window")
and emits NO candidate row, while a theme with a real catalyst produces a row
carrying that catalyst's ``source_event_url``.

This test wires the REAL gate (the real ``find_trigger_event`` over SEEDED
parquets) through ``map_themes`` and asserts the cascade:

  * theme A ("state_media_theme") — only catalyst is an entity-less
    state-media (voc.com.cn / China) article -> gate returns None -> theme is
    SKIPPED -> NO candidate row, and ``_propose_and_filter_candidates`` is
    NEVER called for it (the skip happens before propose).
  * theme B ("legit_theme") — entity-less TechCrunch article (non-state-media)
    -> gate returns a payload -> propose IS called -> a candidate row is emitted
    carrying theme B's catalyst URL.

Hermetic: the LLM proposal is mocked (``_propose_and_filter_candidates``
replaced by a deterministic stub), the verify gates are mocked to pass, the
press window is empty, and the Pro client is a ``Mock``. ``find_trigger_event``
is wrapped (not replaced) so the REAL source-gate logic runs over the tmp
parquets. No network, no keys, deterministic — runs under ``unittest discover``.

Anti-tautology: the test asserts the propose-stub's per-theme call set (theme B
called, theme A not), so a regression that lets the state-media event through
as a catalyst (i.e. reverting :func:`catalyst_resolver._filter_entityless_events`
to a no-op) makes theme A surface a candidate AND get a propose call, failing
both the row assertion and the call-args assertion. The test therefore pins the
gate's cascade through the orchestrator, not just the resolver in isolation.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
from alphalens_pipeline.thematic.mapping import catalyst_resolver, orchestrator

ASOF = dt.date(2026, 6, 12)

STATE_MEDIA_THEME = "state_media_theme"
LEGIT_THEME = "legit_theme"

# Tickers a (mocked) LLM would propose for each theme. The cascade is keyed on
# theme presence, so the exact tickers are arbitrary — they only need to differ
# so an assertion can distinguish "theme B row" from "theme A row".
STATE_MEDIA_TICKER = "BAH"  # the production false-positive class (state-media -> BAH/PSN/AVAV)
LEGIT_TICKER = "SNAP"  # the TechCrunch entity-less regression class

LEGIT_CATALYST_URL = "https://techcrunch.com/2026/06/12/ai-features"
STATE_MEDIA_URL = "https://voc.com.cn/tech-power"


def _gdelt_extra(*, domain: str | None = None, sourcecountry: str | None = None) -> str:
    """GDELT-shaped ``extra`` JSON: domain + sourcecountry live here (see sources/gdelt.py)."""
    return json.dumps({"domain": domain, "sourcecountry": sourcecountry})


def _seed_news(news_dir: Path, date: dt.date, rows: list[dict]) -> None:
    news_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(news_dir / f"{date.isoformat()}.parquet", index=False)


def _seed_events(events_dir: Path, date: dt.date, rows: list[dict]) -> None:
    events_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(events_dir / f"{date.isoformat()}.parquet", index=False)


def _seed_two_themes(news_dir: Path, events_dir: Path) -> None:
    """Seed one entity-less state-media event (theme A) + one legit one (theme B)."""
    _seed_news(
        news_dir,
        ASOF,
        [
            {
                "id": "n_state",
                "title": "Build a tech power, state media urges",
                "url": STATE_MEDIA_URL,
                "timestamp": pd.Timestamp("2026-06-12T08:00:00Z"),
                "extra": _gdelt_extra(domain="voc.com.cn", sourcecountry="China"),
            },
            {
                "id": "n_legit",
                "title": "Social platforms race to ship AI features",
                "url": LEGIT_CATALYST_URL,
                "timestamp": pd.Timestamp("2026-06-12T09:00:00Z"),
                "extra": _gdelt_extra(domain="techcrunch.com", sourcecountry="United States"),
            },
        ],
    )
    _seed_events(
        events_dir,
        ASOF,
        [
            {
                "news_id": "n_state",
                "themes": [STATE_MEDIA_THEME],
                "primary_entities": [],  # entity-less + state-media -> gate drops it
                "confidence": 0.9,
            },
            {
                "news_id": "n_legit",
                "themes": [LEGIT_THEME],
                "primary_entities": [],  # entity-less but reputable source -> kept
                "confidence": 0.9,
            },
        ],
    )


def _propose_stub(*, theme: str, **_kwargs) -> tuple[list[dict], dict[str, float], list[str]]:
    """Deterministic stand-in for ``_propose_and_filter_candidates``.

    Returns the real 3-tuple shape ``(candidates, in_bracket, keywords)`` with a
    single in-bracket candidate per theme so ``_build_row`` emits exactly one
    row. The real proposal (LLM + yfinance mcap filter) is what we are bypassing;
    the gate that decides whether this stub is reached at all is what we test.
    """
    ticker = STATE_MEDIA_TICKER if theme == STATE_MEDIA_THEME else LEGIT_TICKER
    candidate = {
        "ticker": ticker,
        "company_name": f"{ticker} Inc",
        "rationale": "stub",
        "confidence": 0.9,
    }
    return [candidate], {ticker: 2_000_000_000.0}, [theme]


def _passing_verdict(*, ticker: str, **_kwargs) -> dict:
    """A verify verdict with one gate passed so the candidate is kept + a row built."""
    return {
        "ticker": ticker,
        "gates_passed": ["press"],
        "gates_failed": [],
        "gates_unknown": [],
        "verified": True,
        "gate_verdict_json": "{}",
    }


class TestCatalystGateMapThemesIntegration(unittest.TestCase):
    def _run_map_themes(self, output_dir: Path, propose_mock: Mock) -> pd.DataFrame:
        """Run map_themes over the two seeded themes with the gate REAL, propose mocked.

        ``find_trigger_event`` is wrapped (not stubbed) so the real source-gate
        runs against the seeded tmp parquets; the wrapper only injects the dirs
        (the orchestrator calls it without dir args, and the module-level
        defaults bind ``~/.alphalens`` at def-time, so a constant patch would not
        redirect them).
        """
        real_find_trigger_event = catalyst_resolver.find_trigger_event

        def _wrapped_find_trigger_event(*, theme: str, asof: dt.date):
            return real_find_trigger_event(
                theme=theme,
                asof=asof,
                events_dir=self.events_dir,
                news_dir=self.news_dir,
                lookback_days=30,
            )

        with (
            patch.object(orchestrator, "_init_pro_client", return_value=Mock()),
            patch.object(orchestrator, "_fetch_press_window", return_value=pd.DataFrame()),
            patch.object(
                catalyst_resolver,
                "find_trigger_event",
                side_effect=_wrapped_find_trigger_event,
            ),
            patch.object(orchestrator, "_propose_and_filter_candidates", propose_mock),
            patch.object(orchestrator, "verify_candidate", side_effect=_passing_verdict),
        ):
            return orchestrator.map_themes(
                themes=[STATE_MEDIA_THEME, LEGIT_THEME],
                asof=ASOF,
                api_key="testkey",
                output_dir=output_dir,
                rebuild=True,  # bypass the idempotent freeze so the gate actually runs
            )

    def test_state_media_theme_skipped_legit_theme_surfaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.news_dir = tmp_path / "news"
            self.events_dir = tmp_path / "events"
            output_dir = tmp_path / "candidates"
            _seed_two_themes(self.news_dir, self.events_dir)

            propose_mock = Mock(side_effect=_propose_stub)
            df = self._run_map_themes(output_dir, propose_mock)

        themes = set(df["theme"].tolist())
        # The legit theme produced a row; the state-media theme was skipped.
        self.assertIn(LEGIT_THEME, themes)
        self.assertNotIn(STATE_MEDIA_THEME, themes)

        legit_rows = df[df["theme"] == LEGIT_THEME]
        self.assertEqual(len(legit_rows), 1)
        self.assertEqual(legit_rows.iloc[0]["ticker"], LEGIT_TICKER)
        # The row carries the legit catalyst's URL as its provenance.
        self.assertEqual(legit_rows.iloc[0]["source_event_url"], LEGIT_CATALYST_URL)
        # No state-media ticker / URL leaked anywhere into the candidate set.
        self.assertNotIn(STATE_MEDIA_TICKER, set(df["ticker"].tolist()))
        self.assertNotIn(STATE_MEDIA_URL, set(df["source_event_url"].dropna().tolist()))

        # Anti-tautology: propose ran for the legit theme but NOT for the skipped
        # state-media theme — the skip happens BEFORE the propose call. If the
        # source-gate were reverted (state-media event allowed as a catalyst),
        # theme A would NOT be skipped, propose would be called for it, and this
        # assertion (plus the row assertions above) would fail.
        proposed_themes = [call.kwargs["theme"] for call in propose_mock.call_args_list]
        self.assertIn(LEGIT_THEME, proposed_themes)
        self.assertNotIn(STATE_MEDIA_THEME, proposed_themes)
        self.assertEqual(propose_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
