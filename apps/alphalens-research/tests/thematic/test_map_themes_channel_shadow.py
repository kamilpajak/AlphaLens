"""The channel assessment is pure enrichment, and the shadow verdict is data.

Two properties are load-bearing and everything else here supports them:

1. **Assessment never shrinks the candidate list.** Whatever the assessor says —
   all-not_established, a total outage, an off-vocabulary answer — exactly the same
   rows reach the parquet as would have reached it with no assessor at all. If
   this ever stops holding, the gate the 2026-08-19 retrospective rejected is
   back without a new pre-registration.
2. **No ``channel_*`` column may reach selection, ordering or the brief sort.**
   Structural guard modelled on ``tests/test_no_market_state_in_selection.py``,
   planted positive control included.

The shadow verdict exists so a "refused" theme leaves rows that exist, ship and
mature — today a refusal leaves no candidate row, no brief row and no ladder
outcome, which is precisely why the ISO 40-42 window could not have measured
what it was pointed at.
"""

from __future__ import annotations

import datetime as dt
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.thematic.argumentation import orchestrator as brief_orchestrator
from alphalens_pipeline.thematic.mapping import channel_assessor, orchestrator, theme_mapper
from alphalens_pipeline.thematic.mapping.catalyst_contract import CatalystPayload
from alphalens_pipeline.thematic.screening import scorer

_ASOF = dt.date(2026, 8, 18)


def _catalyst() -> CatalystPayload:
    return CatalystPayload(
        url="https://example.com/e",
        title="Air Force awards a trapped-ion computing contract",
        published_at="2026-08-17",
        event_type="contract_award",
        primary_entities=["IONQ"],
        confidence=0.8,
        second_order_implications=[],
        echo_count=1,
        trigger_url="https://example.com/e",
        trigger_published_at="2026-08-17",
        is_amplified=False,
        template_id=None,
        template_facts=None,
    )


def _mcap_from(mcaps):
    return lambda ticker, **_: mcaps.get(ticker)


_SCORED = (channel_assessor.SUPPORT_ESTABLISHED, channel_assessor.SUPPORT_SUGGESTIVE)


def _assessment(
    status: str,
    *,
    outcome=None,
    channel_type="customer_demand",
    grounding=channel_assessor.GROUNDING_GROUNDED,
):
    # A failed call never carries a grounding VERDICT: a valid draw carries both
    # answers, so zero valid draws means `unknown` in both. Forcing it here keeps
    # the stub from expressing a state the assessor cannot produce.
    if outcome not in (None, channel_assessor.AssessmentOutcome.SUCCESS):
        grounding = channel_assessor.GROUNDING_UNKNOWN
    grounded = grounding == channel_assessor.GROUNDING_GROUNDED
    return channel_assessor.ChannelAssessment(
        support_status=status,
        grounding_status=grounding,
        grounding_quote="Air Force awards a trapped-ion computing contract" if grounded else "",
        grounding_reason="" if grounded else "the event is a daily market round-up",
        grounding_agree_n=3 if grounded else 0,
        grounding_quote_verbatim=grounded,
        channel_type=channel_type if status in _SCORED else "none",
        text="a -> b -> c" if status in _SCORED else "",
        evidence="the event states a contract award" if status in _SCORED else "",
        falsifier="the 10-K names no federal customer" if status in _SCORED else "",
        confidence=0.6,
        votes=3,
        valid_n=3,
        support_dispersion=0,
        outcome=outcome or channel_assessor.AssessmentOutcome.SUCCESS,
        assessed_at="2026-08-18T00:00:00+00:00",
    )


def _mapper_result(candidates, outcome=None, keywords=None, decline_reason=""):
    return {
        "candidates": list(candidates),
        "search_keywords": list(keywords or ["quantum computing"]),
        "outcome": outcome
        or (
            theme_mapper.MapperOutcome.SUCCESS
            if candidates
            else theme_mapper.MapperOutcome.DECLINED
        ),
        "decline_reason": decline_reason,
    }


class _RunMapThemes:
    """Drive a hermetic ``map_themes`` with stubbed mapper, mcap and gates."""

    @staticmethod
    def run(
        *,
        out_dir: Path,
        proposed,
        mcaps,
        assessments,
        keep_unverified=True,
        themes=("quantum_computing",),
    ):
        with (
            mock.patch.object(orchestrator, "_resolve_catalyst", return_value=_catalyst()),
            mock.patch.object(
                orchestrator.theme_mapper,
                "propose_candidates",
                return_value=_mapper_result(proposed),
            ),
            mock.patch.object(
                orchestrator.mcap_filter, "fetch_mcap", side_effect=_mcap_from(mcaps)
            ),
            mock.patch.object(
                orchestrator.channel_assessor, "assess_candidates", return_value=assessments
            ),
            mock.patch.object(orchestrator, "_gate_tenk", return_value=True),
            mock.patch.object(orchestrator, "_gate_press", return_value=False),
            mock.patch.object(orchestrator, "_gate_insider", return_value=False),
        ):
            return orchestrator.map_themes(
                themes=list(themes),
                asof=_ASOF,
                output_dir=out_dir,
                keep_unverified=keep_unverified,
            )


class AssessmentNeverShrinksTheCandidateList(unittest.TestCase):
    """The invariant. One case per assessment outcome that could tempt a drop."""

    def _row_count(self, assessments):
        with tempfile.TemporaryDirectory() as tmp:
            df = _RunMapThemes.run(
                out_dir=Path(tmp),
                proposed=[
                    {"ticker": "AAA", "confidence": 0.9},
                    {"ticker": "BBB", "confidence": 0.8},
                ],
                mcaps={"AAA": 1_000_000_000.0, "BBB": 2_000_000_000.0},
                assessments=assessments,
            )
            return len(df)

    def test_all_established_keeps_both_rows(self):
        self.assertEqual(self._row_count([_assessment("established")] * 2), 2)

    def test_all_not_established_keeps_both_rows(self):
        # The case the old gate dropped to zero.
        self.assertEqual(self._row_count([_assessment("not_established")] * 2), 2)

    def test_a_total_assessor_outage_keeps_both_rows(self):
        failed = _assessment(
            "not_established", outcome=channel_assessor.AssessmentOutcome.CALL_FAILED
        )
        self.assertEqual(self._row_count([failed] * 2), 2)

    def test_an_all_misroute_theme_keeps_both_rows(self):
        # DETECT, STAMP, KEEP, MEASURE. A theme_misroute row reads as "obviously
        # a bug, why ship it", which is exactly why it is the field most likely
        # to be turned into a filter later. It ships.
        misroute = _assessment(
            "not_established", grounding=channel_assessor.GROUNDING_THEME_MISROUTE
        )
        self.assertEqual(self._row_count([misroute] * 2), 2)

    def test_a_mixed_grounded_and_misroute_theme_keeps_both_rows(self):
        self.assertEqual(
            self._row_count(
                [
                    _assessment("established"),
                    _assessment(
                        "not_established",
                        grounding=channel_assessor.GROUNDING_CANDIDATE_MISFIT,
                    ),
                ]
            ),
            2,
        )

    def test_the_row_count_is_identical_across_every_grounding_value(self):
        counts = {
            self._row_count([_assessment("not_established", grounding=g)] * 2)
            for g in (
                channel_assessor.GROUNDING_GROUNDED,
                channel_assessor.GROUNDING_THEME_MISROUTE,
                channel_assessor.GROUNDING_CANDIDATE_MISFIT,
            )
        }
        self.assertEqual(counts, {2})

    def test_the_row_count_is_identical_across_every_outcome(self):
        counts = {
            self._row_count([_assessment(s)] * 2)
            for s in ("established", "suggestive", "not_established")
        }
        self.assertEqual(counts, {2})


class BuildRowStampsTheChannelFields(unittest.TestCase):
    def _row(self, assessment, shadow=channel_assessor.ShadowVerdict("keep", 1, 2, 0)):
        return orchestrator._build_row(
            theme="quantum_computing",
            cand={"ticker": "AAA", "rationale": "does x", "confidence": 0.9, "channel": assessment},
            verdict={
                "gates_passed": [],
                "gates_failed": [],
                "gates_unknown": [],
                "verified": False,
            },
            market_cap=1_000_000_000.0,
            catalyst=_catalyst(),
            keywords=["quantum computing"],
            shadow=shadow,
        )

    def test_the_grounding_columns_land_on_the_row(self):
        row = self._row(
            _assessment("established", grounding=channel_assessor.GROUNDING_THEME_MISROUTE)
        )
        # No cross-normalisation: (established x theme_misroute) is the
        # fabrication readout and must survive to the parquet intact.
        self.assertEqual(row["channel_support_status"], "established")
        self.assertEqual(row["channel_grounding_status"], "theme_misroute")
        self.assertEqual(row["channel_grounding_quote"], "")
        self.assertEqual(row["channel_grounding_reason"], "the event is a daily market round-up")
        self.assertEqual(row["channel_grounding_agree_n"], 0)
        self.assertFalse(row["channel_grounding_quote_verbatim"])

    def test_every_channel_column_lands_on_the_row(self):
        row = self._row(_assessment("suggestive"))
        for column in channel_assessor.CHANNEL_ROW_COLUMNS:
            self.assertIn(column, row)
        self.assertEqual(row["channel_support_status"], "suggestive")
        self.assertEqual(row["channel_type"], "customer_demand")
        self.assertEqual(row["channel_support_dispersion"], 0)

    def test_the_shadow_verdict_and_its_denominator_land_on_the_row(self):
        row = self._row(
            _assessment("established"), shadow=channel_assessor.ShadowVerdict("keep", 1, 3, 0)
        )
        self.assertEqual(row["shadow_strict_verdict"], "keep")
        self.assertEqual(row["shadow_strict_established_n"], 1)
        self.assertEqual(row["shadow_strict_assessed_n"], 3)
        self.assertEqual(
            row["shadow_strict_rule_version"], channel_assessor.SHADOW_STRICT_RULE_VERSION
        )

    def test_the_free_text_transmission_channel_column_is_gone(self):
        # No alias, no shim (solo-project doctrine). Its content is now
        # ``channel_text`` with a real status beside it.
        self.assertNotIn("transmission_channel", self._row(_assessment("established")))
        self.assertNotIn("transmission_channel", orchestrator._MAP_THEMES_COLUMNS)

    def test_the_typed_empty_schema_lists_every_new_column(self):
        # ``_MAP_THEMES_COLUMNS`` is the zero-candidate day's schema. A key that
        # exists only in ``_build_row`` makes a quiet day's parquet a different
        # shape from a normal day's.
        for column in (
            *channel_assessor.CHANNEL_ROW_COLUMNS,
            "shadow_strict_verdict",
            "shadow_strict_established_n",
            "shadow_strict_assessed_n",
            "shadow_strict_rule_version",
        ):
            self.assertIn(column, orchestrator._MAP_THEMES_COLUMNS)


class ChannelConfigVersionFollowsTheModelOverride(unittest.TestCase):
    """The stamped token must describe the run that produced the row.

    ``map_themes(model=...)`` overrides the assessor's model, and the freeze
    token composed for the parquet already accounts for it. If the per-row
    ``channel_config_version`` column were built from the default model instead,
    a model-override run would ship rows whose config column contradicted the
    freeze token stamped on the same rows.
    """

    def test_the_stamped_token_matches_the_model_the_run_used(self):
        model = "some/other-model"
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(orchestrator, "_resolve_catalyst", return_value=_catalyst()),
            mock.patch.object(
                orchestrator.theme_mapper,
                "propose_candidates",
                return_value=_mapper_result([{"ticker": "AAA", "confidence": 0.9}]),
            ),
            mock.patch.object(
                orchestrator.mcap_filter,
                "fetch_mcap",
                side_effect=_mcap_from({"AAA": 1_000_000_000.0}),
            ),
            mock.patch.object(
                orchestrator.channel_assessor,
                "assess_candidates",
                return_value=[_assessment("established")],
            ),
            mock.patch.object(orchestrator, "_gate_tenk", return_value=True),
            mock.patch.object(orchestrator, "_gate_press", return_value=False),
            mock.patch.object(orchestrator, "_gate_insider", return_value=False),
        ):
            out = Path(tmp)
            df = orchestrator.map_themes(
                themes=["quantum_computing"],
                asof=_ASOF,
                output_dir=out,
                keep_unverified=True,
                model=model,
            )
            funnel = pd.read_parquet(out / "proposal_funnel" / f"{_ASOF.isoformat()}.parquet")
            decisions = pd.read_parquet(out / "theme_decisions" / f"{_ASOF.isoformat()}.parquet")

        expected = channel_assessor.channel_config_version(model=model)
        self.assertEqual(set(df["channel_config_version"]), {expected})
        self.assertEqual(set(funnel["channel_config_version"]), {expected})
        self.assertEqual(set(decisions["channel_config_version"]), {expected})

    def test_the_empty_day_composes_its_token_from_the_run_s_model(self):
        # A zero-row frame holds NO value to read back, so asserting that the
        # column merely exists could never have failed — the column is a member
        # of _MAP_THEMES_COLUMNS and the typed empty frame carries it whatever
        # the token would have been. What is observable, and what the empty-day
        # path would get wrong, is the WIRING: the channel token must be built
        # from this run's model and must be the one folded into the freeze token.
        model = "some/other"
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                orchestrator.channel_assessor,
                "channel_config_version",
                wraps=channel_assessor.channel_config_version,
            ) as channel_token,
            mock.patch.object(
                orchestrator.theme_mapper,
                "mapper_config_version",
                wraps=theme_mapper.mapper_config_version,
            ) as freeze_token,
        ):
            out = Path(tmp)
            orchestrator.write_empty_candidates(asof=_ASOF, output_dir=out, model=model)
            df = pd.read_parquet(out / f"{_ASOF.isoformat()}.parquet")
        channel_token.assert_called_once_with(model=model)
        self.assertEqual(
            freeze_token.call_args.kwargs["channel_config_version"],
            channel_assessor.channel_config_version(model=model),
        )
        self.assertIn("channel_config_version", df.columns)
        self.assertEqual(len(df), 0)


class ShadowVerdictIsStampedOnEveryRowOfTheTheme(unittest.TestCase):
    def _frame(self, assessments):
        with tempfile.TemporaryDirectory() as tmp:
            return _RunMapThemes.run(
                out_dir=Path(tmp),
                proposed=[
                    {"ticker": "AAA", "confidence": 0.9},
                    {"ticker": "BBB", "confidence": 0.8},
                ],
                mcaps={"AAA": 1_000_000_000.0, "BBB": 2_000_000_000.0},
                assessments=assessments,
            )

    def test_one_established_candidate_keeps_the_whole_theme(self):
        df = self._frame([_assessment("established"), _assessment("not_established")])
        self.assertEqual(set(df["shadow_strict_verdict"]), {"keep"})
        self.assertEqual(set(df["shadow_strict_established_n"]), {1})
        self.assertEqual(set(df["shadow_strict_assessed_n"]), {2})

    def test_no_established_candidate_refuses_the_whole_theme(self):
        df = self._frame([_assessment("suggestive"), _assessment("not_established")])
        self.assertEqual(set(df["shadow_strict_verdict"]), {"refuse"})
        self.assertEqual(set(df["shadow_strict_established_n"]), {0})

    def test_the_refused_theme_still_produces_shippable_rows(self):
        # THE point of move 3. Under the old gate this theme produced nothing at
        # all, so the refused leg of a forward KEPT-vs-REFUSED test did not
        # exist in live data.
        df = self._frame([_assessment("not_established"), _assessment("not_established")])
        self.assertEqual(len(df), 2)


class OffBracketProposalsAreNotAssessed(unittest.TestCase):
    def test_an_off_bracket_proposal_is_recorded_as_not_assessed(self):
        # "not_assessed" and the bottom support level must never merge: the first says the
        # bracket dropped it, the second is a judgement about the world.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _RunMapThemes.run(
                out_dir=out,
                proposed=[
                    {"ticker": "AAA", "confidence": 0.9},
                    {"ticker": "MEGA", "confidence": 0.8},
                ],
                mcaps={"AAA": 1_000_000_000.0, "MEGA": 3_000_000_000_000.0},
                assessments=[_assessment("established")],
            )
            funnel = pd.read_parquet(out / "proposal_funnel" / f"{_ASOF.isoformat()}.parquet")
        by_ticker = funnel.set_index("ticker")["channel_support_status"].to_dict()
        self.assertEqual(by_ticker["AAA"], "established")
        self.assertEqual(by_ticker["MEGA"], "not_assessed")

    def test_an_off_bracket_proposal_reads_not_assessed_in_both_columns(self):
        # The model was never asked EITHER question, so neither column may carry
        # a verdict. A "grounded" here would claim an answer nobody gave.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _RunMapThemes.run(
                out_dir=out,
                proposed=[
                    {"ticker": "AAA", "confidence": 0.9},
                    {"ticker": "MEGA", "confidence": 0.8},
                ],
                mcaps={"AAA": 1_000_000_000.0, "MEGA": 3_000_000_000_000.0},
                assessments=[_assessment("established")],
            )
            funnel = pd.read_parquet(out / "proposal_funnel" / f"{_ASOF.isoformat()}.parquet")
        grounding = funnel.set_index("ticker")["channel_grounding_status"].to_dict()
        self.assertEqual(grounding["AAA"], "grounded")
        self.assertEqual(grounding["MEGA"], "not_assessed")

    def test_the_assessor_only_sees_in_bracket_candidates(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(orchestrator, "_resolve_catalyst", return_value=_catalyst()),
            mock.patch.object(
                orchestrator.theme_mapper,
                "propose_candidates",
                return_value=_mapper_result(
                    [
                        {"ticker": "AAA", "confidence": 0.9},
                        {"ticker": "MEGA", "confidence": 0.8},
                    ]
                ),
            ),
            mock.patch.object(
                orchestrator.mcap_filter,
                "fetch_mcap",
                side_effect=_mcap_from({"AAA": 1_000_000_000.0, "MEGA": 3_000_000_000_000.0}),
            ),
            mock.patch.object(
                orchestrator.channel_assessor,
                "assess_candidates",
                return_value=[_assessment("established")],
            ) as assess,
            mock.patch.object(orchestrator, "_gate_tenk", return_value=True),
            mock.patch.object(orchestrator, "_gate_press", return_value=False),
            mock.patch.object(orchestrator, "_gate_insider", return_value=False),
        ):
            orchestrator.map_themes(
                themes=["quantum_computing"],
                asof=_ASOF,
                output_dir=Path(tmp),
                keep_unverified=True,
            )
        sent = [c["ticker"] for c in assess.call_args.kwargs["candidates"]]
        self.assertEqual(sent, ["AAA"])


class ThemeDecisionsSidecar(unittest.TestCase):
    """A stage-A decline and a no-catalyst skip must leave a trace on disk."""

    def _decisions(self, out: Path) -> pd.DataFrame:
        return pd.read_parquet(out / "theme_decisions" / f"{_ASOF.isoformat()}.parquet")

    def test_a_theme_with_candidates_records_its_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _RunMapThemes.run(
                out_dir=out,
                proposed=[
                    {"ticker": "AAA", "confidence": 0.9},
                    {"ticker": "MEGA", "confidence": 0.8},
                ],
                mcaps={"AAA": 1_000_000_000.0, "MEGA": 3_000_000_000_000.0},
                assessments=[_assessment("established")],
            )
            row = self._decisions(out).iloc[0]
        self.assertEqual(row["theme"], "quantum_computing")
        self.assertEqual(row["n_proposed"], 2)
        self.assertEqual(row["n_in_bracket"], 1)
        self.assertEqual(row["n_established"], 1)
        self.assertEqual(row["n_grounded"], 1)
        self.assertEqual(row["n_theme_misroute"], 0)
        self.assertEqual(row["n_candidate_misfit"], 0)
        self.assertEqual(row["n_grounding_unknown"], 0)
        self.assertEqual(row["shadow_strict_verdict"], "keep")
        self.assertEqual(row["mapper_outcome"], "success")

    def test_a_misrouted_theme_records_its_grounding_counts_beside_the_shadow(self):
        # Grounding counts sit BESIDE the shadow verdict, never inside it: the
        # shadow replays the OLD gate, which had no grounding concept. Stamping
        # them here is what makes an offline re-cut possible without new calls.
        misroute = _assessment(
            "not_established", grounding=channel_assessor.GROUNDING_THEME_MISROUTE
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _RunMapThemes.run(
                out_dir=out,
                proposed=[
                    {"ticker": "AAA", "confidence": 0.9},
                    {"ticker": "BBB", "confidence": 0.8},
                ],
                mcaps={"AAA": 1_000_000_000.0, "BBB": 2_000_000_000.0},
                assessments=[misroute, misroute],
            )
            row = self._decisions(out).iloc[0]
        self.assertEqual(row["n_theme_misroute"], 2)
        self.assertEqual(row["n_grounded"], 0)
        # The shadow is UNTOUCHED by grounding — same estimand as before.
        self.assertEqual(row["shadow_strict_verdict"], "refuse")
        self.assertNotIn("theme_grounding_verdict", row.index)

    def test_a_declined_theme_still_leaves_a_row(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(orchestrator, "_resolve_catalyst", return_value=_catalyst()),
            mock.patch.object(
                orchestrator.theme_mapper,
                "propose_candidates",
                return_value=_mapper_result([], decline_reason="no_event"),
            ),
        ):
            out = Path(tmp)
            orchestrator.map_themes(themes=["quiet_theme"], asof=_ASOF, output_dir=out)
            row = self._decisions(out).iloc[0]
        self.assertEqual(row["mapper_outcome"], "declined")
        self.assertEqual(row["decline_reason"], "no_event")
        self.assertEqual(row["n_proposed"], 0)
        self.assertEqual(row["shadow_strict_verdict"], "refuse")
        # ``n_in_bracket`` is the shadow verdict's denominator on the sidecar.
        self.assertEqual(row["n_in_bracket"], 0)

    def test_a_theme_with_no_catalyst_still_leaves_a_row(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(orchestrator, "_resolve_catalyst", return_value=None),
        ):
            out = Path(tmp)
            orchestrator.map_themes(themes=["noisy_theme"], asof=_ASOF, output_dir=out)
            row = self._decisions(out).iloc[0]
        self.assertEqual(row["mapper_outcome"], "no_catalyst")
        self.assertEqual(row["n_proposed"], 0)

    def test_a_sidecar_write_failure_never_aborts_the_build(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                orchestrator, "write_parquet_atomic", side_effect=_fail_on_theme_decisions
            ),
        ):
            df = _RunMapThemes.run(
                out_dir=Path(tmp),
                proposed=[{"ticker": "AAA", "confidence": 0.9}],
                mcaps={"AAA": 1_000_000_000.0},
                assessments=[_assessment("established")],
            )
        self.assertEqual(len(df), 1)


def _fail_on_theme_decisions(frame, path, **kwargs):
    if "theme_decisions" in str(path):
        raise OSError("disk full")


class ChannelLogLine(unittest.TestCase):
    def test_the_existing_funnel_line_is_unchanged(self):
        # Operators grep `funnel —`. The channel counts go on a SECOND line
        # rather than being appended, so the pinned substrings stay put.
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertLogs("alphalens_pipeline.thematic.mapping.orchestrator", "INFO") as cm,
        ):
            _RunMapThemes.run(
                out_dir=Path(tmp),
                proposed=[
                    {"ticker": "AAA", "confidence": 0.9},
                    {"ticker": "MEGA", "confidence": 0.8},
                ],
                mcaps={"AAA": 1_000_000_000.0, "MEGA": 3_000_000_000_000.0},
                assessments=[_assessment("established")],
            )
        joined = "\n".join(cm.output)
        self.assertIn("funnel — proposed 2, in mcap bracket 1", joined)

    def test_a_channel_line_reports_the_status_split_and_the_shadow_verdict(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertLogs("alphalens_pipeline.thematic.mapping.orchestrator", "INFO") as cm,
        ):
            _RunMapThemes.run(
                out_dir=Path(tmp),
                proposed=[
                    {"ticker": "AAA", "confidence": 0.9},
                    {"ticker": "BBB", "confidence": 0.8},
                ],
                mcaps={"AAA": 1_000_000_000.0, "BBB": 2_000_000_000.0},
                assessments=[_assessment("established"), _assessment("not_established")],
            )
        joined = "\n".join(cm.output)
        self.assertIn("channel — established 1, suggestive 0, not_established 1, failed 0", joined)
        self.assertIn("shadow=keep", joined)

    def test_a_grounding_line_reports_the_split_on_its_own_line(self):
        # A THIRD line, not appended to either of the two above: the operator's
        # `grep 'channel —'` recipe and its pinned substrings must keep working.
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertLogs("alphalens_pipeline.thematic.mapping.orchestrator", "INFO") as cm,
        ):
            _RunMapThemes.run(
                out_dir=Path(tmp),
                proposed=[
                    {"ticker": "AAA", "confidence": 0.9},
                    {"ticker": "BBB", "confidence": 0.8},
                ],
                mcaps={"AAA": 1_000_000_000.0, "BBB": 2_000_000_000.0},
                assessments=[
                    _assessment("established"),
                    _assessment(
                        "not_established",
                        grounding=channel_assessor.GROUNDING_THEME_MISROUTE,
                    ),
                ],
            )
        joined = "\n".join(cm.output)
        self.assertIn(
            "grounding — grounded 1, theme_misroute 1, candidate_misfit 0, unknown 0", joined
        )

    def test_a_misrouted_majority_escalates_to_a_warning(self):
        # A pipeline-defect page, never a trading signal: the theme's answered
        # majority says the event is not about the theme it was routed under.
        misroute = _assessment(
            "not_established", grounding=channel_assessor.GROUNDING_THEME_MISROUTE
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertLogs("alphalens_pipeline.thematic.mapping.orchestrator", "WARNING") as cm,
        ):
            _RunMapThemes.run(
                out_dir=Path(tmp),
                proposed=[
                    {"ticker": "AAA", "confidence": 0.9},
                    {"ticker": "BBB", "confidence": 0.8},
                ],
                mcaps={"AAA": 1_000_000_000.0, "BBB": 2_000_000_000.0},
                assessments=[misroute, misroute],
            )
        joined = "\n".join(cm.output)
        self.assertIn("theme_misroute", joined)
        self.assertIn("upstream", joined)


class InstrumentFailuresLeaveTheShadowDenominator(unittest.TestCase):
    """An outage must not read as "no theme had a channel today".

    Every failed assessment carries the BOTTOM support level by construction,
    so counting failures inside ``shadow_strict_assessed_n`` would turn a 429
    storm into a healthy-looking all-``refuse`` day with a non-zero denominator
    — a failure that looks like an answer, one level up from the per-candidate
    outcome column. The per-theme failure count lives in the theme-decisions
    sidecar, which is written best-effort and can be absent for exactly the run
    that needs it, so the count is also stamped on every candidate row.
    """

    def _frame(self, assessments):
        with tempfile.TemporaryDirectory() as tmp:
            return _RunMapThemes.run(
                out_dir=Path(tmp),
                proposed=[
                    {"ticker": "AAA", "confidence": 0.9},
                    {"ticker": "BBB", "confidence": 0.8},
                ],
                mcaps={"AAA": 1_000_000_000.0, "BBB": 2_000_000_000.0},
                assessments=assessments,
            )

    def test_a_failed_assessment_is_outside_the_denominator_and_counted_apart(self):
        failed = _assessment(
            "not_established", outcome=channel_assessor.AssessmentOutcome.CALL_FAILED
        )
        df = self._frame([_assessment("not_established"), failed])
        self.assertEqual(set(df["shadow_strict_assessed_n"]), {1})
        self.assertEqual(set(df["shadow_strict_failed_n"]), {1})
        self.assertEqual(set(df["shadow_strict_verdict"]), {"refuse"})

    def test_a_total_outage_refuses_with_a_zero_denominator(self):
        failed = _assessment(
            "not_established", outcome=channel_assessor.AssessmentOutcome.CALL_FAILED
        )
        df = self._frame([failed, failed])
        self.assertEqual(set(df["shadow_strict_assessed_n"]), {0})
        self.assertEqual(set(df["shadow_strict_failed_n"]), {2})
        # Rows still ship — the assessment never drops anything.
        self.assertEqual(len(df), 2)

    def test_a_healthy_theme_reports_zero_failures(self):
        df = self._frame([_assessment("established"), _assessment("not_established")])
        self.assertEqual(set(df["shadow_strict_failed_n"]), {0})
        self.assertEqual(set(df["shadow_strict_assessed_n"]), {2})


class StageBIsCappedPerTheme(unittest.TestCase):
    """Stage B pays only for the candidates that can still ship.

    ``_verify_candidates_for_theme`` attempts the top
    ``_MAX_VERIFY_ATTEMPTS_PER_THEME`` by stage-A confidence and ships at most
    ``_MAX_CANDIDATES_PER_THEME``, so a candidate ranked below that can never
    reach a brief row. Without the cap the stage is bounded only by
    ``theme_mapper._MAX_CANDIDATES`` in bracket × ``_ASSESS_VOTES`` × the theme
    count, against a systemd ``TimeoutStartSec`` — and ``map_themes`` writes its
    parquet once, AFTER the theme loop, so an overrun leaves no file at all.
    """

    def _run(self, n_candidates):
        proposed = [
            {"ticker": f"T{i:02d}", "confidence": 0.9 - i / 100} for i in range(n_candidates)
        ]
        mcaps = {c["ticker"]: 1_000_000_000.0 for c in proposed}
        seen: list[list[str]] = []

        def _fake_assess(*, candidates, **_kwargs):
            seen.append([str(c["ticker"]) for c in candidates])
            return [_assessment("established") for _ in candidates]

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(orchestrator, "_resolve_catalyst", return_value=_catalyst()),
            mock.patch.object(
                orchestrator.theme_mapper,
                "propose_candidates",
                return_value=_mapper_result(proposed),
            ),
            mock.patch.object(
                orchestrator.mcap_filter, "fetch_mcap", side_effect=_mcap_from(mcaps)
            ),
            mock.patch.object(
                orchestrator.channel_assessor, "assess_candidates", side_effect=_fake_assess
            ),
            mock.patch.object(orchestrator, "_gate_tenk", return_value=True),
            mock.patch.object(orchestrator, "_gate_press", return_value=False),
            mock.patch.object(orchestrator, "_gate_insider", return_value=False),
        ):
            out = Path(tmp)
            df = orchestrator.map_themes(
                themes=["quantum_computing"],
                asof=_ASOF,
                output_dir=out,
                keep_unverified=True,
            )
            funnel = pd.read_parquet(out / "proposal_funnel" / f"{_ASOF.isoformat()}.parquet")
            decisions = pd.read_parquet(out / "theme_decisions" / f"{_ASOF.isoformat()}.parquet")
        return seen, df, funnel, decisions

    def test_only_the_top_confidence_candidates_reach_the_model(self):
        seen, _df, _funnel, _decisions = self._run(8)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]), orchestrator._MAX_ASSESS_PER_THEME)
        self.assertEqual(seen[0], ["T00", "T01", "T02", "T03", "T04"])

    def test_the_cap_never_drops_a_candidate(self):
        _seen, _df, funnel, _decisions = self._run(8)
        # Every proposal still gets a funnel row: the cap is an annotation, not
        # a filter — the whole point of the increment.
        self.assertEqual(len(funnel), 8)

    def test_a_capped_candidate_is_distinguishable_from_a_bracket_drop(self):
        _seen, _df, funnel, _decisions = self._run(8)
        outcomes = funnel.set_index("ticker")["channel_assessment_outcome"].to_dict()
        self.assertEqual(outcomes["T00"], "success")
        self.assertEqual(outcomes["T07"], "over_assess_cap")
        self.assertNotEqual(outcomes["T07"], "not_assessed")
        statuses = funnel.set_index("ticker")["channel_support_status"].to_dict()
        self.assertEqual(statuses["T07"], channel_assessor.NOT_ASSESSED)

    def test_a_capped_candidate_is_outside_the_shadow_denominator(self):
        _seen, df, _funnel, _decisions = self._run(8)
        self.assertEqual(set(df["shadow_strict_assessed_n"]), {5})

    def test_the_theme_decision_records_how_many_were_capped(self):
        _seen, _df, _funnel, decisions = self._run(8)
        self.assertEqual(list(decisions["n_over_assess_cap"]), [3])
        self.assertEqual(list(decisions["n_in_bracket"]), [8])

    def test_a_small_theme_is_not_capped(self):
        seen, _df, _funnel, decisions = self._run(2)
        self.assertEqual(seen[0], ["T00", "T01"])
        self.assertEqual(list(decisions["n_over_assess_cap"]), [0])

    def test_the_cap_is_logged_loudly_when_it_bites(self):
        with self.assertLogs("alphalens_pipeline.thematic.mapping.orchestrator", "WARNING") as cm:
            self._run(8)
        self.assertIn("past the assessment cap", "\n".join(cm.output))


class ChannelGaugeCountersComeFromARealRun(unittest.TestCase):
    """The six ``df.attrs`` counters the CLI publishes as Prometheus gauges.

    The CLI-side test patches ``map_themes`` wholesale, so it can only exercise
    ``attrs.get(name, 0)``. Nothing else asserted the values ``_channel_counts``
    actually produces from a real theme loop, which left a wrong sum, a swapped
    keep/refuse counter or a dropped key free to ship into Prometheus.
    """

    _NAMES = (
        "channel_established",
        "channel_suggestive",
        "channel_not_established",
        "channel_assess_failed",
        "channel_grounded",
        "channel_theme_misroute",
        "channel_candidate_misfit",
        "channel_grounding_unknown",
        "themes_misrouted",
        "themes_shadow_kept",
        "themes_shadow_refused",
    )

    def _run(self, per_theme_assessments):
        themes = [f"theme_{i}" for i in range(len(per_theme_assessments))]
        proposed = [
            {"ticker": "AAA", "confidence": 0.9},
            {"ticker": "BBB", "confidence": 0.8},
        ]
        mcaps = {"AAA": 1_000_000_000.0, "BBB": 2_000_000_000.0}
        batches = iter(per_theme_assessments)
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(orchestrator, "_resolve_catalyst", return_value=_catalyst()),
            mock.patch.object(
                orchestrator.theme_mapper,
                "propose_candidates",
                return_value=_mapper_result(proposed),
            ),
            mock.patch.object(
                orchestrator.mcap_filter, "fetch_mcap", side_effect=_mcap_from(mcaps)
            ),
            mock.patch.object(
                orchestrator.channel_assessor,
                "assess_candidates",
                side_effect=lambda **_kw: next(batches),
            ),
            mock.patch.object(orchestrator, "_gate_tenk", return_value=True),
            mock.patch.object(orchestrator, "_gate_press", return_value=False),
            mock.patch.object(orchestrator, "_gate_insider", return_value=False),
        ):
            return orchestrator.map_themes(
                themes=themes,
                asof=_ASOF,
                output_dir=Path(tmp),
                keep_unverified=True,
            )

    def test_a_known_status_mix_across_two_themes_sums_correctly(self):
        failed = _assessment(
            "not_established", outcome=channel_assessor.AssessmentOutcome.CALL_FAILED
        )
        df = self._run(
            [
                [_assessment("established"), _assessment("suggestive")],
                [_assessment("not_established"), failed],
            ]
        )
        self.assertEqual(
            {name: df.attrs[name] for name in self._NAMES},
            {
                "channel_established": 1,
                "channel_suggestive": 1,
                "channel_not_established": 1,
                "channel_assess_failed": 1,
                "channel_grounded": 3,
                "channel_theme_misroute": 0,
                "channel_candidate_misfit": 0,
                "channel_grounding_unknown": 1,
                "themes_misrouted": 0,
                "themes_shadow_kept": 1,
                "themes_shadow_refused": 1,
            },
        )

    def test_a_misrouted_theme_is_counted_at_the_theme_level(self):
        misroute = _assessment(
            "not_established", grounding=channel_assessor.GROUNDING_THEME_MISROUTE
        )
        df = self._run([[misroute, misroute], [_assessment("established")] * 2])
        self.assertEqual(df.attrs["channel_theme_misroute"], 2)
        self.assertEqual(df.attrs["themes_misrouted"], 1)

    def test_keep_and_refuse_are_not_transposed(self):
        df = self._run([[_assessment("established"), _assessment("established")]])
        self.assertEqual(df.attrs["themes_shadow_kept"], 1)
        self.assertEqual(df.attrs["themes_shadow_refused"], 0)


class ChannelNeverReachesSelectionOrderingOrTheBriefSort(unittest.TestCase):
    """Structural anti-rot guard, modelled on test_no_market_state_in_selection.

    The single largest long-term risk in this design is a later change that
    reads ``channel_support_status`` in a filter, a sort key or a score — which would
    recreate the rejected gate without a new pre-registration. Weakening this
    test is a defect, not a cleanup.
    """

    _TOKEN = re.compile(r"channel_[a-z_]*")

    def _source(self, module) -> str:
        return Path(module.__file__).read_text()

    def test_the_scorer_never_mentions_a_channel_column(self):
        self.assertIsNone(self._TOKEN.search(self._source(scorer)))

    def test_the_selection_score_never_mentions_a_channel_column(self):
        from alphalens_pipeline.thematic.screening import selection_score

        self.assertIsNone(self._TOKEN.search(self._source(selection_score)))

    def test_the_scan_would_catch_a_planted_channel_reference(self):
        # Positive control: without it the regex could rot to matching nothing
        # and this file would pass while guarding nothing.
        planted = 'weight = row["channel_support_status"] * 2\n'
        self.assertIsNotNone(self._TOKEN.search(planted))

    def test_the_scan_would_catch_a_planted_grounding_reference(self):
        # Second positive control, aimed at the field MOST likely to be turned
        # into a filter later, because "obviously a bug, why ship it" reads as
        # common sense. It is absent BY DESIGN, not by omission: gating on it
        # needs a stratified accuracy audit and its own pre-registration
        # (design memo §6). This is also why every grounding column keeps the
        # channel_ prefix — it is what puts them inside this regex at all.
        planted = 'if row["channel_grounding_status"] != "grounded": continue\n'
        self.assertIsNotNone(self._TOKEN.search(planted))

    def test_no_channel_column_is_a_candidate_sort_key(self):
        self.assertFalse([k for k in orchestrator._CANDIDATE_SORT_KEYS if k.startswith("channel_")])

    def test_no_channel_column_is_a_brief_sort_key(self):
        keys = [k for k, *_rest in brief_orchestrator._BRIEF_SORT_KEYS]
        self.assertFalse([k for k in keys if k.startswith("channel_")])

    def test_no_shadow_column_is_a_brief_sort_key(self):
        keys = [k for k, *_rest in brief_orchestrator._BRIEF_SORT_KEYS]
        self.assertFalse([k for k in keys if k.startswith("shadow_")])


if __name__ == "__main__":
    unittest.main()
