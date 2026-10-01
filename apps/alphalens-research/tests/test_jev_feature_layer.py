"""Contracts of the System One feature builder.

The builder reads no outcome column, so it is not a look and nothing here
concerns the ledger. What it DOES need pinning is the set of ways a future edit
could silently change the numbers in the store without anyone noticing:

* the answer space drifting away from the live taxonomy,
* the version token failing to move when the instrument changes,
* the mapper's own `rationale` leaking into the candidate state, which would
  hand the model the answer it is being asked to produce,
* a row from an older version counting as already done.
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
import pyarrow as pa

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ml" / "2026_10_jev_feature_layer.py"


def _load():
    spec = importlib.util.spec_from_file_location("_jev_feature_layer", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


layer = _load()


class TestTheAnswerSpaceTracksTheLiveTaxonomy(unittest.TestCase):
    """The Choice options must be the event types the pipeline actually uses.

    A second hand-maintained copy would drift, and a drifted copy does not fail
    loudly: the model is simply never offered the right answer and picks the
    nearest wrong one, which looks like a model error rather than a config bug.
    """

    def test_every_live_event_type_is_offered(self):
        from alphalens_pipeline.thematic.screening.catalyst_signals import (
            EVENT_TYPE_TIER,
            NOISE_EVENT_TYPES,
        )

        offered = set(layer.ARTICLE_QUESTIONS["event_type"]["criteria"])
        self.assertEqual(offered, set(EVENT_TYPE_TIER) | set(NOISE_EVENT_TYPES))

    def test_a_type_with_no_description_is_refused(self):
        with mock.patch.dict(layer._EVENT_TYPE_DESCRIPTIONS, clear=False) as patched:
            patched.pop("m_and_a")
            with self.assertRaises(ValueError) as caught:
                layer._event_type_criteria()
        self.assertIn("m_and_a", str(caught.exception))

    def test_a_description_for_a_type_the_pipeline_does_not_know_is_refused(self):
        with mock.patch.dict(layer._EVENT_TYPE_DESCRIPTIONS, {"invented": "x"}):
            with self.assertRaises(ValueError) as caught:
                layer._event_type_criteria()
        self.assertIn("invented", str(caught.exception))

    def test_every_option_carries_a_description_not_just_its_own_name(self):
        # An option described by its own name gives the model nothing to go on.
        for name, text in layer.ARTICLE_QUESTIONS["event_type"]["criteria"].items():
            self.assertNotEqual(name, text)
            self.assertGreater(len(text), 20, name)

    def test_the_materiality_levels_are_ordered_and_more_than_two(self):
        # A Score with two levels is a Noul wearing a rubric; the whole point is
        # a position on a spectrum.
        levels = layer.ARTICLE_QUESTIONS["materiality"]["criteria"]
        self.assertIsInstance(levels, list)
        self.assertGreaterEqual(len(levels), 3)


class TestTheVersionTokenMovesWithTheInstrument(unittest.TestCase):
    """Rows produced by different instruments must not pool.

    This is the same hazard `catalyst_config_version` exists to prevent: a
    reworded question produces different numbers under an unchanged token, and a
    later analysis then averages two instruments.
    """

    def test_rewording_a_question_moves_the_token(self):
        before = layer.jev_feature_version()
        reworded = json.loads(json.dumps(layer.ARTICLE_QUESTIONS))
        reworded["concrete_fact"]["instructions"] += " Answer strictly."
        with mock.patch.object(layer, "ARTICLE_QUESTIONS", reworded):
            self.assertNotEqual(layer.jev_feature_version(), before)

    def test_changing_the_body_cap_moves_the_token(self):
        before = layer.jev_feature_version()
        with mock.patch.object(layer, "BODY_CHAR_CAP", layer.BODY_CHAR_CAP + 1):
            self.assertNotEqual(layer.jev_feature_version(), before)

    def test_changing_the_model_moves_the_token(self):
        before = layer.jev_feature_version()
        with mock.patch.object(layer, "MODEL", "typesafe/jev-9.99"):
            self.assertNotEqual(layer.jev_feature_version(), before)

    def test_changing_a_candidate_question_moves_the_token(self):
        before = layer.jev_feature_version()
        reworded = json.loads(json.dumps(layer.CANDIDATE_QUESTIONS))
        reworded["company_gain"]["criteria"]["true"] += " Be strict."
        with mock.patch.object(layer, "CANDIDATE_QUESTIONS", reworded):
            self.assertNotEqual(layer.jev_feature_version(), before)

    def test_reordering_the_questions_does_not_move_the_token(self):
        # A reordering changes no number. A token that drifted on it would end a
        # cohort for nothing, which teaches the reader to ignore the token.
        before = layer.jev_feature_version()
        reversed_questions = dict(reversed(list(layer.ARTICLE_QUESTIONS.items())))
        with mock.patch.object(layer, "ARTICLE_QUESTIONS", reversed_questions):
            self.assertEqual(layer.jev_feature_version(), before)

    def test_the_token_names_its_schema(self):
        self.assertTrue(layer.jev_feature_version().startswith("jev-v"))


class TestTheCandidateStateDoesNotLeakTheMappersConclusion(unittest.TestCase):
    """`rationale` was written while deciding this company fits this article.

    Supplying it back would hand the model the answer and inflate exactly the
    discrimination this layer is supposed to measure independently. The guard is
    a source gate because the leak would be a one-word addition to a dict.
    """

    def test_the_candidate_state_carries_exactly_the_six_objective_fields(self):
        row = pd.Series(
            {
                "ticker": "acme",
                "company_name": "Acme Corp",
                "industry_name": "Investment Advice",
                "sector_name": "Finance",
                "rationale": "A pure play on exactly the thing this article is about.",
            }
        )
        article = pd.Series({"title": "T", "body": "B"})
        state = layer.candidate_state(row, article)
        self.assertEqual(
            set(state),
            {
                "article_title",
                "article_body",
                "ticker",
                "company_name",
                "company_industry",
                "company_sector",
            },
        )
        # The discriminating assertion: the leak is one key away, and the fixture
        # supplies a rationale that would plainly give the answer away.
        self.assertNotIn("rationale", state)
        for value in state.values():
            self.assertNotIn("pure play", value)
        self.assertEqual(state["ticker"], "ACME")

    def test_the_article_state_sends_title_and_a_capped_body(self):
        row = pd.Series({"title": "T", "body": "x" * (layer.BODY_CHAR_CAP + 500)})
        state = layer.article_state(row)
        self.assertEqual(set(state), {"article_title", "article_body"})
        self.assertEqual(len(state["article_body"]), layer.BODY_CHAR_CAP)

    def test_a_missing_body_sends_an_empty_string_not_the_word_nan(self):
        # A pandas missing value is NaN, and NaN is TRUTHY, so `str(v or "")`
        # yields the literal "nan" and the model reads it as the article. 20 % of
        # stored articles have no body, so this is the common path.
        for missing in (None, float("nan")):
            state = layer.article_state(pd.Series({"title": "T", "body": missing}))
            self.assertEqual(state["article_body"], "", repr(missing))
        row = pd.Series(
            {
                "ticker": "A",
                "company_name": None,
                "industry_name": float("nan"),
                "sector_name": None,
            }
        )
        cand = layer.candidate_state(row, pd.Series({"title": None, "body": None}))
        self.assertEqual(
            [v for v in cand.values() if v == "nan"], [], f"a field became the text 'nan': {cand}"
        )

    def test_the_omission_is_explained_in_prose_not_just_absent(self):
        # An unexplained omission gets "fixed" by the next reader.
        self.assertIn("rationale", _SCRIPT.read_text())
        self.assertIn("hand the model the answer", _SCRIPT.read_text())


class TestWhatCountsAsAlreadyDone(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "d.parquet"

    def _write(self, rows):
        pd.DataFrame(rows).to_parquet(self.path, index=False)

    def test_a_row_at_the_current_version_counts(self):
        version = layer.jev_feature_version()
        self._write([{"news_id": "a", "jev_feature_version": version}])
        self.assertEqual(layer._already_done(self.path, ["news_id"], version), {("a",)})

    def test_a_row_from_an_older_version_does_not_count(self):
        # A version bump exists precisely to re-ask; counting old rows as done
        # would make the bump silently do nothing.
        self._write([{"news_id": "a", "jev_feature_version": "jev-v1-oldoldoldol"}])
        self.assertEqual(
            layer._already_done(self.path, ["news_id"], layer.jev_feature_version()), set()
        )

    def test_a_missing_file_means_nothing_is_done(self):
        self.assertEqual(
            layer._already_done(self.path, ["news_id"], layer.jev_feature_version()), set()
        )

    def test_an_unreadable_file_means_nothing_is_done_rather_than_crashing(self):
        self.path.write_bytes(b"not a parquet")
        self.assertEqual(
            layer._already_done(self.path, ["news_id"], layer.jev_feature_version()), set()
        )

    def test_a_file_written_before_the_version_column_existed_means_nothing_is_done(self):
        self._write([{"news_id": "a"}])
        self.assertEqual(
            layer._already_done(self.path, ["news_id"], layer.jev_feature_version()), set()
        )


class TestTheWriteIsSafeToRepeat(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "d.parquet"

    def _row(self, news_id: str, materiality: float) -> dict:
        return {
            "date": "2026-08-19",
            "news_id": news_id,
            "url": "u",
            "source": "rss",
            "jev_model": "m",
            "jev_event_type": "earnings",
            "jev_event_type_confidence": 0.5,
            "jev_event_type_probs_json": "{}",
            "jev_materiality": materiality,
            "jev_materiality_confidence": 0.5,
            "jev_materiality_probs_json": "{}",
            "jev_concrete_fact": 0.5,
            "jev_body_chars_sent": 10,
            "jev_cost_usd": 0.0,
            "jev_feature_version": layer.jev_feature_version(),
            "jev_computed_at": "now",
        }

    def test_the_declared_schema_is_used_so_an_all_null_column_keeps_its_type(self):
        # Without a declared schema a date whose every call was refused writes
        # null-typed columns and a later dataset read across the store fails.
        row = self._row("a", 1.0)
        row["jev_materiality"] = None
        layer._write(self.path, [row], layer._ARTICLE_SCHEMA, ["news_id"])
        self.assertEqual(
            pa.parquet.read_schema(self.path).field("jev_materiality").type, pa.float64()
        )

    def test_a_second_write_of_the_same_key_replaces_it_rather_than_duplicating(self):
        layer._write(self.path, [self._row("a", 1.0)], layer._ARTICLE_SCHEMA, ["news_id"])
        layer._write(self.path, [self._row("a", 2.0)], layer._ARTICLE_SCHEMA, ["news_id"])
        out = pd.read_parquet(self.path)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out["jev_materiality"].iloc[0], 2.0)

    def test_writing_no_rows_leaves_an_existing_file_untouched(self):
        layer._write(self.path, [self._row("a", 1.0)], layer._ARTICLE_SCHEMA, ["news_id"])
        before = self.path.read_bytes()
        self.assertEqual(layer._write(self.path, [], layer._ARTICLE_SCHEMA, ["news_id"]), 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_no_temp_file_is_left_behind(self):
        layer._write(self.path, [self._row("a", 1.0)], layer._ARTICLE_SCHEMA, ["news_id"])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])


class TestTheDateSelector(unittest.TestCase):
    AVAILABLE = ["2026-05-18", "2026-06-01", "2026-07-05", "2026-09-30"]

    def test_no_span_means_every_date(self):
        self.assertEqual(layer._select_dates(self.AVAILABLE, None), self.AVAILABLE)

    def test_both_ends_are_inclusive(self):
        self.assertEqual(
            layer._select_dates(self.AVAILABLE, "2026-06-01:2026-07-05"),
            ["2026-06-01", "2026-07-05"],
        )

    def test_an_open_end_runs_to_the_edge_of_the_store(self):
        self.assertEqual(
            layer._select_dates(self.AVAILABLE, "2026-07-05:"), ["2026-07-05", "2026-09-30"]
        )
        self.assertEqual(
            layer._select_dates(self.AVAILABLE, ":2026-06-01"), ["2026-05-18", "2026-06-01"]
        )


class TestSpendAccounting(unittest.TestCase):
    def test_a_refusal_is_counted_and_costs_nothing(self):
        spend = layer._Spend()
        spend.add(0.001)
        spend.refuse()
        self.assertEqual((spend.calls, spend.refused), (1, 1))
        self.assertAlmostEqual(spend.usd, 0.001)

    def test_a_call_with_no_reported_cost_still_counts_as_a_call(self):
        # A batch that reports 0 calls because the vendor omitted usage would
        # look like a batch that did nothing.
        spend = layer._Spend()
        spend.add(None)
        self.assertEqual(spend.calls, 1)
        self.assertAlmostEqual(spend.usd, 0.0)


class TestTheRowKeysMatchTheDeclaredSchema(unittest.TestCase):
    """A key in the row dict that the schema does not declare is SILENTLY DROPPED.

    Measured with pyarrow: a key the schema declares and the dict omits raises
    `KeyError`, and a wrong dtype raises `ArrowInvalid` — both fail loudly. But an
    EXTRA key just vanishes, so adding a feature here and forgetting the schema
    would discard it with no error anywhere. These tests pin the parity in both
    directions, which is the only direction that cannot fail on its own.
    """

    class _Answer:
        def __init__(self, **kw):
            self.type = kw.get("type", "noul")
            self.noul = kw.get("noul")
            self.choice = kw.get("choice")
            self.score = kw.get("score")
            self.probabilities = kw.get("probabilities")
            self.confidence = kw.get("confidence")

    class _Out:
        def __init__(self, answers):
            self.model = "typesafe/jev-1.13-20260917"
            self.answers = answers
            self.cost_usd = 1e-05

    def test_an_article_row_has_exactly_the_declared_fields(self):
        out = self._Out(
            {
                "event_type": self._Answer(
                    type="choice",
                    choice="earnings",
                    confidence=0.7,
                    probabilities={"earnings": 0.7},
                ),
                "materiality": self._Answer(
                    type="score", score=1.5, confidence=0.6, probabilities={"1": 0.5}
                ),
                "concrete_fact": self._Answer(noul=0.8),
            }
        )
        row = layer.article_row(
            pd.Series({"id": "n1", "url": "u", "source": "rss"}),
            out,
            date="2026-08-19",
            version="v",
            body_chars=10,
        )
        self.assertEqual(set(row), set(layer._ARTICLE_SCHEMA.names))

    def test_a_candidate_row_has_exactly_the_declared_fields(self):
        out = self._Out(
            {"touches_industry": self._Answer(noul=0.8), "company_gain": self._Answer(noul=0.3)}
        )
        article = pd.Series({"id": "n1", "title": "t", "body": "b"}, name="https://example.test/a")
        row = layer.candidate_row(
            pd.Series({"ticker": "acme"}),
            article,
            out,
            date="2026-08-19",
            version="v",
            body_chars=10,
        )
        self.assertEqual(set(row), set(layer._CANDIDATE_SCHEMA.names))

    def test_a_row_whose_every_answer_is_absent_still_writes_under_the_schema(self):
        # A refusal is skipped entirely, but a 200 that answered only some of the
        # questions must still produce a well-typed row rather than a crash.
        row = layer.article_row(
            pd.Series({"id": "n1", "url": "u", "source": "rss"}),
            self._Out({}),
            date="2026-08-19",
            version="v",
            body_chars=0,
        )
        self.assertEqual(set(row), set(layer._ARTICLE_SCHEMA.names))
        layer._write(self.path, [row], layer._ARTICLE_SCHEMA, ["news_id"])
        self.assertEqual(len(pd.read_parquet(self.path)), 1)

    def setUp(self):
        import tempfile

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "d.parquet"


if __name__ == "__main__":
    unittest.main()
