"""Idempotent-freeze behaviour for the thematic ``map-themes`` stage.

A re-run for the SAME asof date must reuse the frozen candidate parquet
instead of re-rolling the (server-side non-deterministic) DeepSeek MoE
proposal — otherwise a borderline candidate appears in one of the 6×/day
runs and vanishes in the next, silently mutating the recommended set the
EDGE feedback record is keyed on. The freeze is keyed by ``(asof,
config_version)``; ``--rebuild`` forces a recompute; a degraded/legacy/
mismatched parquet is NOT reused (anti-poisoned-freeze).
"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.thematic.mapping import channel_assessor, orchestrator, theme_mapper
from alphalens_pipeline.thematic.mapping.catalyst_contract import CatalystPayload

from tests.thematic.mapping_stubs import stub_assessor, theme_proposal

ASOF = dt.date(2026, 6, 15)
MCAP = orchestrator.DEFAULT_MCAP_RANGE


_CATALYST = CatalystPayload(
    url="https://example.com/freeze",
    title="Stub catalyst",
    published_at="2026-08-04",
    event_type="contract_award",
    primary_entities=[],
    confidence=0.8,
    second_order_implications=[],
    echo_count=1,
    trigger_url="https://example.com/freeze",
    trigger_published_at="2026-08-04",
    is_amplified=False,
    template_id=None,
    template_facts=None,
)


def _cfg(**over) -> str:
    """The freeze token, with the stage-B channel token composed in.

    ``mapper_config_version`` takes ``channel_config_version`` as a REQUIRED
    keyword since 2026-08-19: an assessment-prompt edit must invalidate the
    day's frozen candidate parquet, otherwise the 6x/day rerun would serve
    channel fields produced under different rules.
    """
    model = over.pop("model", None)
    return theme_mapper.mapper_config_version(
        market_cap_range=over.pop("market_cap_range", MCAP),
        model=model,
        channel_config_version=over.pop(
            "channel_config_version", channel_assessor.channel_config_version(model=model)
        ),
    )


def _frozen_row(ticker: str, *, verified: bool) -> dict:
    return {
        "theme": "government_contract",
        "ticker": ticker,
        "company_name": "Example Corp",
        "rationale": "serves U.S. government agencies",
        "llm_confidence": 0.90,
        "market_cap": 1_000_000_000,
        "gates_passed": ["tenk"],
        "gates_passed_str": "tenk",
        "n_gates_passed": 1,
        "gates_failed": [],
        "gates_failed_str": "",
        "n_gates_failed": 0,
        "gates_unknown": [],
        "gates_unknown_str": "",
        "n_gates_unknown": 0,
        "verified": verified,
        "gate_verdict_json": "{}",
        "source_event_url": "https://example.com/news",
        "source_event_title": "headline",
        "source_event_published_at": "2026-06-15",
        "theme_search_keywords": ["government contract"],
    }


def _write_frozen(
    out_dir: Path,
    *,
    config_version: str | None,
    verified: bool = True,
    rows: int = 1,
) -> None:
    """Write a candidates parquet for ``ASOF``. ``config_version=None`` omits the
    column entirely (legacy/pre-freeze shape); ``rows=0`` writes an empty frame."""
    if rows == 0:
        df = pd.DataFrame(columns=list(orchestrator._MAP_THEMES_COLUMNS))
    else:
        df = pd.DataFrame([_frozen_row(f"TIC{i}", verified=verified) for i in range(rows)])
    if config_version is not None:
        df["mapper_config_version"] = config_version
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_dir / f"{ASOF.isoformat()}.parquet", index=False)


class TestMapperConfigVersion(unittest.TestCase):
    def test_stable_for_identical_inputs(self):
        a = _cfg()
        b = _cfg()
        self.assertEqual(a, b)

    def test_changes_with_mcap_range(self):
        a = _cfg(market_cap_range=(1, 2))
        b = _cfg(market_cap_range=(1, 3))
        self.assertNotEqual(a, b)

    def test_carries_schema_tag(self):
        self.assertIn("mapper-freeze", _cfg())

    def test_changes_with_model(self):
        a = _cfg()
        b = _cfg(model="other/model")
        self.assertNotEqual(a, b)

    def test_changes_with_the_channel_assessment_config(self):
        # An assessment-prompt edit must invalidate the frozen candidate
        # parquet: its channel fields were produced under the old rules, and a
        # rerun that reused them would mix two configs in one column.
        self.assertNotEqual(_cfg(), _cfg(channel_config_version="channel-assess-vX"))

    def test_default_model_token_matches_explicit_default(self):
        # Threading model must not gratuitously invalidate existing frozen sets:
        # the default-model token equals the no-model token byte-for-byte.
        self.assertEqual(_cfg(), _cfg(model=theme_mapper.DEFAULT_MODEL))


class TestMapThemesFreeze(unittest.TestCase):
    def setUp(self) -> None:
        # Stage B calls OpenRouter once per in-bracket candidate; without
        # this stub these tests hit the live API.
        stub_assessor(self)

    def test_reuses_frozen_set_without_proposing_or_building_llm(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            cfg = _cfg()
            _write_frozen(out, config_version=cfg)
            with (
                patch.object(orchestrator, "_resolve_catalyst") as cat,
                patch.object(orchestrator, "_propose_and_bracket") as prop,
                patch.object(orchestrator, "_init_pro_client") as pro,
            ):
                df = orchestrator.map_themes(
                    themes=["government_contract"], asof=ASOF, output_dir=out
                )
            # Freeze returns before catalyst resolution / LLM client build / proposal.
            cat.assert_not_called()
            prop.assert_not_called()
            pro.assert_not_called()
            self.assertEqual(list(df["ticker"]), ["TIC0"])

    def test_a_frozen_reuse_still_publishes_the_channel_gauges_as_zeros(self):
        # A series that disappears on a healthy day is indistinguishable from a
        # stopped exporter, so the six counters must be present-and-zero on the
        # frozen path too. Asserted on the frame a REAL frozen reuse returns —
        # the CLI-side test patches map_themes wholesale and therefore only
        # exercises its own ``attrs.get(name, 0)`` default.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            cfg = _cfg()
            _write_frozen(out, config_version=cfg)
            with (
                patch.object(orchestrator, "_resolve_catalyst"),
                patch.object(orchestrator, "_propose_and_bracket"),
                patch.object(orchestrator, "_init_pro_client"),
            ):
                df = orchestrator.map_themes(
                    themes=["government_contract"], asof=ASOF, output_dir=out
                )
        for name in (
            "channel_established",
            "channel_suggestive",
            "channel_not_established",
            "channel_assess_failed",
            "themes_shadow_kept",
            "themes_shadow_refused",
        ):
            self.assertIn(name, df.attrs)
            self.assertEqual(df.attrs[name], 0)

    def test_rebuild_flag_forces_recompute(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            cfg = _cfg()
            _write_frozen(out, config_version=cfg)
            with (
                patch.object(orchestrator, "_resolve_catalyst", return_value=_CATALYST),
                patch.object(
                    orchestrator,
                    "_propose_and_bracket",
                    return_value=theme_proposal(outcome=theme_mapper.MapperOutcome.DECLINED),
                ) as prop,
                patch.object(orchestrator, "_init_pro_client"),
                patch.object(orchestrator, "_fetch_press_window", return_value=None),
            ):
                orchestrator.map_themes(
                    themes=["government_contract"], asof=ASOF, output_dir=out, rebuild=True
                )
            prop.assert_called_once()

    def test_model_override_threaded_into_proposal(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            with (
                patch.object(orchestrator, "_resolve_catalyst", return_value=_CATALYST),
                patch.object(
                    orchestrator,
                    "_propose_and_bracket",
                    return_value=theme_proposal(outcome=theme_mapper.MapperOutcome.DECLINED),
                ) as prop,
                patch.object(orchestrator, "_init_pro_client"),
                patch.object(orchestrator, "_fetch_press_window", return_value=None),
            ):
                orchestrator.map_themes(
                    themes=["government_contract"],
                    asof=ASOF,
                    output_dir=out,
                    model="custom/model-x",
                )
            self.assertEqual(prop.call_args.kwargs["model"], "custom/model-x")

    def test_config_version_mismatch_recomputes(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            _write_frozen(out, config_version="stale-config-token")
            with (
                patch.object(orchestrator, "_resolve_catalyst", return_value=_CATALYST),
                patch.object(
                    orchestrator,
                    "_propose_and_bracket",
                    return_value=theme_proposal(outcome=theme_mapper.MapperOutcome.DECLINED),
                ) as prop,
                patch.object(orchestrator, "_init_pro_client"),
                patch.object(orchestrator, "_fetch_press_window", return_value=None),
            ):
                orchestrator.map_themes(themes=["government_contract"], asof=ASOF, output_dir=out)
            prop.assert_called_once()

    def test_degraded_frozen_set_recomputes(self):
        # A first run that produced only unverified rows must not seal the date.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            cfg = _cfg()
            _write_frozen(out, config_version=cfg, verified=False)
            with (
                patch.object(orchestrator, "_resolve_catalyst", return_value=_CATALYST),
                patch.object(
                    orchestrator,
                    "_propose_and_bracket",
                    return_value=theme_proposal(outcome=theme_mapper.MapperOutcome.DECLINED),
                ) as prop,
                patch.object(orchestrator, "_init_pro_client"),
                patch.object(orchestrator, "_fetch_press_window", return_value=None),
            ):
                orchestrator.map_themes(themes=["government_contract"], asof=ASOF, output_dir=out)
            prop.assert_called_once()

    def test_legacy_parquet_without_version_recomputes(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            _write_frozen(out, config_version=None)  # pre-freeze shape
            with (
                patch.object(orchestrator, "_resolve_catalyst", return_value=_CATALYST),
                patch.object(
                    orchestrator,
                    "_propose_and_bracket",
                    return_value=theme_proposal(outcome=theme_mapper.MapperOutcome.DECLINED),
                ) as prop,
                patch.object(orchestrator, "_init_pro_client"),
                patch.object(orchestrator, "_fetch_press_window", return_value=None),
            ):
                orchestrator.map_themes(themes=["government_contract"], asof=ASOF, output_dir=out)
            prop.assert_called_once()

    def test_fresh_run_stamps_config_version(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            cfg = _cfg()
            with (
                patch.object(orchestrator, "_resolve_catalyst", return_value=_CATALYST),
                patch.object(
                    orchestrator,
                    "_propose_and_bracket",
                    return_value=theme_proposal(
                        proposed=[{"ticker": "AAA"}],
                        in_bracket={"AAA": 1_000_000_000},
                        outcome=theme_mapper.MapperOutcome.SUCCESS,
                    ),
                ),
                patch.object(
                    orchestrator,
                    "_verify_candidates_for_theme",
                    return_value=([_frozen_row("AAA", verified=True)], 0, 0),
                ),
                patch.object(orchestrator, "_init_pro_client"),
                patch.object(orchestrator, "_fetch_press_window", return_value=None),
            ):
                df = orchestrator.map_themes(
                    themes=["government_contract"], asof=ASOF, output_dir=out
                )
            self.assertIn("mapper_config_version", df.columns)
            self.assertTrue((df["mapper_config_version"] == cfg).all())
            # And the stamped parquet is reusable on the next run.
            self.assertTrue((out / f"{ASOF.isoformat()}.parquet").exists())


class TestWriteEmptyCandidates(unittest.TestCase):
    """A zero-novel-themes day writes a typed-empty candidates parquet so the
    downstream score/brief stages find the file (the run_thematic_day.sh
    `set -e` chain would otherwise abort). The empty set MUST remain
    recompute-eligible: a later 6×/day slot that DOES surface novel themes for
    the same date must not reuse the empty freeze (anti-poisoned-freeze)."""

    def test_writes_typed_empty_parquet_with_config_stamp(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            path = orchestrator.write_empty_candidates(
                asof=ASOF, output_dir=out, market_cap_range=MCAP
            )
            self.assertEqual(path, out / f"{ASOF.isoformat()}.parquet")
            self.assertTrue(path.exists())
            df = pd.read_parquet(path)
            self.assertEqual(len(df), 0)
            for col in orchestrator._MAP_THEMES_COLUMNS:
                self.assertIn(col, df.columns)
            cfg = _cfg()
            self.assertTrue((df["mapper_config_version"] == cfg).all())

    def test_empty_set_is_not_reused_so_later_slots_recompute(self):
        # The empty parquet is config-stamped, but _load_frozen_candidates
        # treats an empty/all-unverified set as degraded -> recompute, so a
        # later run that surfaces news is NOT poisoned by the morning's empty
        # freeze.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            path = orchestrator.write_empty_candidates(
                asof=ASOF, output_dir=out, market_cap_range=MCAP
            )
            cfg = _cfg()
            self.assertIsNone(orchestrator._load_frozen_candidates(path, cfg))


if __name__ == "__main__":
    unittest.main()
