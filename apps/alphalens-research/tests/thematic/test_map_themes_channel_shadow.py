"""The channel assessment is pure enrichment, and the shadow verdict is data.

Two properties are load-bearing and everything else here supports them:

1. **Assessment never shrinks the candidate list.** Whatever the assessor says —
   all-unverified, a total outage, an off-vocabulary answer — exactly the same
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


def _assessment(status: str, *, outcome=None, channel_type="customer_demand"):
    return channel_assessor.ChannelAssessment(
        status=status,
        channel_type=channel_type if status in ("verified", "partial") else "none",
        text="a -> b -> c" if status in ("verified", "partial") else "",
        evidence="the event states a contract award" if status != "unverified" else "",
        falsifier="the 10-K names no federal customer" if status != "unverified" else "",
        confidence=0.6,
        votes=3,
        valid_n=3,
        dispersion=0,
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

    def test_all_verified_keeps_both_rows(self):
        self.assertEqual(self._row_count([_assessment("verified")] * 2), 2)

    def test_all_unverified_keeps_both_rows(self):
        # The case the old gate dropped to zero.
        self.assertEqual(self._row_count([_assessment("unverified")] * 2), 2)

    def test_a_total_assessor_outage_keeps_both_rows(self):
        failed = _assessment("unverified", outcome=channel_assessor.AssessmentOutcome.CALL_FAILED)
        self.assertEqual(self._row_count([failed] * 2), 2)

    def test_the_row_count_is_identical_across_every_outcome(self):
        counts = {
            self._row_count([_assessment(s)] * 2) for s in ("verified", "partial", "unverified")
        }
        self.assertEqual(counts, {2})


class BuildRowStampsTheChannelFields(unittest.TestCase):
    def _row(self, assessment, shadow=("keep", 1, 2)):
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

    def test_every_channel_column_lands_on_the_row(self):
        row = self._row(_assessment("partial"))
        for column in channel_assessor.CHANNEL_ROW_COLUMNS:
            self.assertIn(column, row)
        self.assertEqual(row["channel_status"], "partial")
        self.assertEqual(row["channel_type"], "customer_demand")
        self.assertEqual(row["channel_vote_dispersion"], 0)

    def test_the_shadow_verdict_and_its_denominator_land_on_the_row(self):
        row = self._row(_assessment("verified"), shadow=("keep", 1, 3))
        self.assertEqual(row["shadow_strict_verdict"], "keep")
        self.assertEqual(row["shadow_strict_verified_n"], 1)
        self.assertEqual(row["shadow_strict_assessed_n"], 3)
        self.assertEqual(
            row["shadow_strict_rule_version"], channel_assessor.SHADOW_STRICT_RULE_VERSION
        )

    def test_the_free_text_transmission_channel_column_is_gone(self):
        # No alias, no shim (solo-project doctrine). Its content is now
        # ``channel_text`` with a real status beside it.
        self.assertNotIn("transmission_channel", self._row(_assessment("verified")))
        self.assertNotIn("transmission_channel", orchestrator._MAP_THEMES_COLUMNS)

    def test_the_typed_empty_schema_lists_every_new_column(self):
        # ``_MAP_THEMES_COLUMNS`` is the zero-candidate day's schema. A key that
        # exists only in ``_build_row`` makes a quiet day's parquet a different
        # shape from a normal day's.
        for column in (
            *channel_assessor.CHANNEL_ROW_COLUMNS,
            "shadow_strict_verdict",
            "shadow_strict_verified_n",
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
                return_value=[_assessment("verified")],
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

    def test_the_empty_day_parquet_carries_the_same_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            orchestrator.write_empty_candidates(asof=_ASOF, output_dir=out, model="some/other")
            df = pd.read_parquet(out / f"{_ASOF.isoformat()}.parquet")
        self.assertIn("channel_config_version", df.columns)


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

    def test_one_verified_candidate_keeps_the_whole_theme(self):
        df = self._frame([_assessment("verified"), _assessment("unverified")])
        self.assertEqual(set(df["shadow_strict_verdict"]), {"keep"})
        self.assertEqual(set(df["shadow_strict_verified_n"]), {1})
        self.assertEqual(set(df["shadow_strict_assessed_n"]), {2})

    def test_no_verified_candidate_refuses_the_whole_theme(self):
        df = self._frame([_assessment("partial"), _assessment("unverified")])
        self.assertEqual(set(df["shadow_strict_verdict"]), {"refuse"})
        self.assertEqual(set(df["shadow_strict_verified_n"]), {0})

    def test_the_refused_theme_still_produces_shippable_rows(self):
        # THE point of move 3. Under the old gate this theme produced nothing at
        # all, so the refused leg of a forward KEPT-vs-REFUSED test did not
        # exist in live data.
        df = self._frame([_assessment("unverified"), _assessment("unverified")])
        self.assertEqual(len(df), 2)


class OffBracketProposalsAreNotAssessed(unittest.TestCase):
    def test_an_off_bracket_proposal_is_recorded_as_not_assessed(self):
        # "not_assessed" and "unverified" must never merge: the first says the
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
                assessments=[_assessment("verified")],
            )
            funnel = pd.read_parquet(out / "proposal_funnel" / f"{_ASOF.isoformat()}.parquet")
        by_ticker = funnel.set_index("ticker")["channel_status"].to_dict()
        self.assertEqual(by_ticker["AAA"], "verified")
        self.assertEqual(by_ticker["MEGA"], "not_assessed")

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
                return_value=[_assessment("verified")],
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
                assessments=[_assessment("verified")],
            )
            row = self._decisions(out).iloc[0]
        self.assertEqual(row["theme"], "quantum_computing")
        self.assertEqual(row["n_proposed"], 2)
        self.assertEqual(row["n_in_bracket"], 1)
        self.assertEqual(row["n_verified"], 1)
        self.assertEqual(row["shadow_strict_verdict"], "keep")
        self.assertEqual(row["mapper_outcome"], "success")

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
                assessments=[_assessment("verified")],
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
                assessments=[_assessment("verified")],
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
                assessments=[_assessment("verified"), _assessment("unverified")],
            )
        joined = "\n".join(cm.output)
        self.assertIn("channel — verified 1, partial 0, unverified 1, failed 0", joined)
        self.assertIn("shadow=keep", joined)


class ChannelNeverReachesSelectionOrderingOrTheBriefSort(unittest.TestCase):
    """Structural anti-rot guard, modelled on test_no_market_state_in_selection.

    The single largest long-term risk in this design is a later change that
    reads ``channel_status`` in a filter, a sort key or a score — which would
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
        planted = 'weight = row["channel_status"] * 2\n'
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
