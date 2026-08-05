"""The operator's provider pin has to reach EVERY thematic LLM stage.

``provider_routing_from_env()`` is read in exactly one place —
``OpenRouterClient.from_env()``, i.e. ``get_default_openrouter_client()``.
A stage that builds its own ``OpenRouterClient(api_key=...)`` therefore opts
itself out of the pin, and does so INVISIBLY: the request just carries no
``provider`` block, the serving-provider log line looks identical, and the
operator reading the journal concludes the pin took effect.

`extract` and `map-themes` are the two heaviest LLM stages, so "the pin is
live" is false unless they route through the default client. These tests pin
both halves of that: the CLI must not hand a raw key down (which is what
selects the bypassing branch), and the stage must land on the pinned client
when it is not handed one.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import pandas as pd
from alphalens_cli.main import app
from alphalens_pipeline.data.alt_data import openrouter_client as _orc_module
from alphalens_pipeline.thematic.extraction import event_extractor
from alphalens_pipeline.thematic.mapping import orchestrator as mapping_orchestrator
from typer.testing import CliRunner

_FAKE_KEY = "fake-openrouter-key"

# One knob is enough to make the routing block non-None, and it is the knob
# the live pipeline is documented to set.
_PINNED_ENV = {
    "OPENROUTER_API_KEY": _FAKE_KEY,
    _orc_module.PROVIDER_ORDER_ENV: "",
    _orc_module.PROVIDER_ALLOW_FALLBACKS_ENV: "",
    _orc_module.PROVIDER_QUANTIZATIONS_ENV: "fp8",
    _orc_module.PROVIDER_REQUIRE_PARAMETERS_ENV: "",
}

_SAMPLE_EXTRACTION = {
    "event_type": "product_launch",
    "primary_entities": ["NVDA"],
    "themes": ["quantum_computing"],
    "sentiment": "positive",
    "second_order_implications": ["QUBT may benefit"],
    "confidence": 0.85,
}


def _news_row(news_id: str = "p1", title: str = "anything") -> dict:
    return {
        "id": news_id,
        "source": "polygon",
        "timestamp": pd.Timestamp("2026-05-15T10:00:00Z"),
        "tickers": [],
        "title": title,
        "body": "",
        "url": f"https://example.com/{news_id}",
        "keywords": [],
        "extra": "{}",
    }


class TestStagesLandOnThePinnedDefaultClient(unittest.TestCase):
    """Given no explicit key, each stage must build its client through
    ``get_default_openrouter_client()`` — the only constructor that reads the
    routing env. Asserted on the client instance that actually made the call,
    not on a mock's call args, so a stage that quietly re-derives a key from
    the environment still fails."""

    def setUp(self) -> None:
        _orc_module._reset_default_client_for_tests()
        self.addCleanup(_orc_module._reset_default_client_for_tests)

    def test_event_extraction_calls_a_client_carrying_the_pin(self) -> None:
        captured: dict = {}

        def fake_call_llm(llm_client, prompt, *, model):
            captured["client"] = llm_client
            return SimpleNamespace(text=json.dumps(_SAMPLE_EXTRACTION))

        with (
            mock.patch.dict(os.environ, _PINNED_ENV, clear=False),
            mock.patch.object(event_extractor, "_call_llm", side_effect=fake_call_llm),
        ):
            event_extractor.extract_one(_news_row())

        self.assertEqual(
            captured["client"]._provider_routing,
            {"quantizations": ["fp8"], "require_parameters": True},
        )

    def test_theme_mapping_batch_client_carries_the_pin(self) -> None:
        """``map_themes`` hoists ONE client for the whole batch. That hoist is
        the stage's real client, so it is what has to carry the pin."""
        with mock.patch.dict(os.environ, _PINNED_ENV, clear=False):
            client = mapping_orchestrator._init_pro_client(None)

        self.assertIsNotNone(client)
        self.assertEqual(
            client._provider_routing,
            {"quantizations": ["fp8"], "require_parameters": True},
        )


class TestThematicCliDoesNotBypassThePin(unittest.TestCase):
    """The CLI reading ``OPENROUTER_API_KEY`` itself and threading it down is
    exactly what selects the un-pinned ``OpenRouterClient(api_key=...)``
    branch. It must keep its fail-fast check on the key and stop passing it."""

    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_extract_leaves_client_construction_to_the_stage(self) -> None:
        captured: dict = {}

        def fake_extract_daily(**kwargs):
            captured.update(kwargs)
            return pd.DataFrame()

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": _FAKE_KEY}, clear=False),
            mock.patch(
                "alphalens_cli.commands.thematic.event_extractor.extract_daily",
                side_effect=fake_extract_daily,
            ),
            mock.patch(
                "alphalens_cli.commands.thematic.themes_mod.roll_up",
                return_value=pd.DataFrame(),
            ),
            mock.patch(
                "alphalens_cli.commands.thematic.themes_mod.flag_novel",
                return_value=pd.DataFrame(),
            ),
        ):
            result = self.runner.invoke(
                app,
                ["thematic", "extract", "--date", "2026-05-15", "--news-dir", tmpdir],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIsNone(captured.get("api_key"))

    def test_map_themes_leaves_client_construction_to_the_stage(self) -> None:
        captured: dict = {}

        def fake_map_themes(**kwargs):
            captured.update(kwargs)
            return pd.DataFrame()

        novel = pd.DataFrame([{"theme": "quantum_computing", "novelty_score": 4.2}])
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": _FAKE_KEY}, clear=False),
            mock.patch(
                "alphalens_cli.commands.thematic.orchestrator.map_themes",
                side_effect=fake_map_themes,
            ),
            mock.patch(
                "alphalens_cli.commands.thematic.themes_mod.roll_up",
                return_value=pd.DataFrame(),
            ),
            mock.patch(
                "alphalens_cli.commands.thematic.themes_mod.flag_novel",
                return_value=novel,
            ),
        ):
            result = self.runner.invoke(
                app,
                ["thematic", "map-themes", "--date", "2026-05-15", "--output-dir", tmpdir],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIsNone(captured.get("api_key"))

    def test_extract_still_fails_fast_without_a_key(self) -> None:
        """Dropping the pass-through must not drop the early check: a missing
        key otherwise surfaces per-row, deep inside the extraction loop, as
        hundreds of warnings and a zero-event day at exit 0."""
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False),
        ):
            result = self.runner.invoke(
                app,
                ["thematic", "extract", "--date", "2026-05-15", "--news-dir", tmpdir],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("OPENROUTER_API_KEY", result.output)


class TestMalformedRoutingKnobFailsTheRun(unittest.TestCase):
    """A garbage value in ``/etc/alphalens/env`` — the file the runbook tells
    the operator to edit — must kill the run at startup.

    It cannot be left to surface at client-construction time: several stages
    catch ``ValueError`` there to degrade gracefully when the API key is
    missing, so ``brief`` would swallow it, stamp every row
    ``brief_status='unavailable'``, write the parquet and exit 0 — a whole day
    of prose-less briefs published to Postgres behind one warning line."""

    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_cli_refuses_to_start_on_an_unparseable_knob(self) -> None:
        with mock.patch.dict(
            os.environ,
            {_orc_module.PROVIDER_REQUIRE_PARAMETERS_ENV: "y"},
            clear=False,
        ):
            result = self.runner.invoke(app, ["thematic", "map-themes", "--date", "2026-05-15"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn(_orc_module.PROVIDER_REQUIRE_PARAMETERS_ENV, result.output)

    def test_the_gate_fires_before_any_command_body_runs(self) -> None:
        """Load-bearing: the gate is only worth anything if it is UPSTREAM of
        the stages that swallow the error. ``brief`` is the one that degraded
        — it catches ``ValueError`` around client construction to keep the
        missing-key case graceful. Proven by which complaint comes back: the
        knob, not ``brief``'s own missing-input check, which sits at the very
        top of the command body."""
        with mock.patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": _FAKE_KEY,
                _orc_module.PROVIDER_REQUIRE_PARAMETERS_ENV: "y",
            },
            clear=False,
        ):
            result = self.runner.invoke(app, ["thematic", "brief", "--date", "2026-05-15"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn(_orc_module.PROVIDER_REQUIRE_PARAMETERS_ENV, result.output)
        self.assertNotIn("scored parquet missing", result.output)

    def test_experts_is_gated_too(self) -> None:
        """``experts enrich`` swallows client-construction errors the same way
        ``brief`` does (buffett/qualitative.py catches bare ``Exception``), so it
        needs the same upstream gate — otherwise a bad knob silently produces a
        day with no qualitative layer."""
        with mock.patch.dict(
            os.environ,
            {_orc_module.PROVIDER_REQUIRE_PARAMETERS_ENV: "y"},
            clear=False,
        ):
            result = self.runner.invoke(app, ["experts", "enrich", "2026-05-15", "--all"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn(_orc_module.PROVIDER_REQUIRE_PARAMETERS_ENV, result.output)

    def test_a_command_group_that_never_calls_an_llm_is_not_gated(self) -> None:
        """Blast radius. ``/etc/alphalens/env`` is shared by every unit, so a
        gate in the ROOT callback would take down `alphalens edgar detect` — a
        15-minute poller that never reads an OpenRouter variable — over a typo
        in an LLM routing knob. The gate belongs on the command groups that
        actually construct an LLM client, not on the whole binary."""
        with mock.patch.dict(
            os.environ,
            {_orc_module.PROVIDER_REQUIRE_PARAMETERS_ENV: "y"},
            clear=False,
        ):
            result = self.runner.invoke(app, ["edgar", "detect", "--definitely-not-a-flag"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertNotIn(_orc_module.PROVIDER_REQUIRE_PARAMETERS_ENV, result.output)
        self.assertIn("No such option", result.output)


if __name__ == "__main__":
    unittest.main()
