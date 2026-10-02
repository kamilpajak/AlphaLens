"""Contracts of the burnt-panel discovery pass for the System One features.

The script reads BURNT outcome values, so it is a look even though it charges 0, and
what has to be pinned is everything that could make the look mean something other
than what its ledger row says:

* the candidate list, frozen before the run, so a later addition is a visible diff
  and not a quiet extra chance;
* the instrument it reads, so it cannot silently pool two feature versions;
* the anchor, which is READ and never recomputed;
* `catalyst_mass`, which must track the live noise vocabulary and must not turn an
  unreadable distribution into a confident zero.
"""

from __future__ import annotations

import ast
import contextlib
import datetime as dt
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ml" / "2026_10_jev_discovery_burnt.py"


def _load():
    spec = importlib.util.spec_from_file_location("_jev_discovery", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


disc = _load()


class TestTheFrozenSpecification(unittest.TestCase):
    """Freezing the list before the run is the whole difference between a discovery
    pass and a fishing trip. These tests make a change to it visible in review."""

    EXPECTED = (
        "jev_materiality",
        "jev_catalyst_mass",
        "jev_concrete_fact",
        "jev_company_gain",
        "jev_type_confidence",
        "jev_touches_industry",
    )

    def test_the_six_candidates_are_the_ones_the_ledger_row_names(self):
        self.assertEqual(tuple(n for _, n in disc.CANDIDATES), self.EXPECTED)

    def test_the_reference_line_matches_the_candidate_count(self):
        # Six candidates reported against a plain 0.05 would be six chances at it.
        self.assertAlmostEqual(disc.REFERENCE_BAR, 0.05 / len(disc.CANDIDATES))

    def test_the_controls_are_the_two_already_known_to_be_real(self):
        # Keeping them as CONTROLS is what makes a candidate's coefficient an
        # increment over known structure rather than a restatement of it.
        self.assertEqual(tuple(n for _, n in disc.CONTROLS), ("atr", "ma50_dist"))

    def test_the_cutoff_is_the_burnt_window(self):
        self.assertEqual(disc.BURNT_CUTOFF, "2026-07-05")

    def test_the_instrument_is_pinned_to_one_feature_version(self):
        # Reading two versions would average two different instruments.
        self.assertRegex(disc.FEATURE_VERSION, r"^jev-v\d+-[0-9a-f]{12}$")

    def test_the_actionable_floor_is_the_one_1227_froze(self):
        self.assertAlmostEqual(disc.ACTIONABLE_DELTA, 0.10)


class TestTheAnchorIsReadAndNeverRecomputed(unittest.TestCase):
    """Owner decision D4 fixes the anchor at the first session AFTER the brief date.
    `session_on_or_after` returns the brief date itself when that date is a session,
    and the two disagreed on 296 of 466 burnt rows. A test that only checked for an
    `arrival` column would pass either way, so this reads the source."""

    @staticmethod
    def _called_names(source: str) -> set[str]:
        called = set()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
        return called

    def test_the_module_never_calls_session_on_or_after(self):
        self.assertNotIn("session_on_or_after", self._called_names(_SCRIPT.read_text()))

    def test_the_gate_reads_calls_and_not_prose(self):
        # Positive control both ways: a substring check would fire on the comment
        # that explains the rule, and would miss a call written as an attribute.
        self.assertIn("session_on_or_after", _SCRIPT.read_text())
        self.assertIn("f", self._called_names("f(1)"))
        self.assertIn("g", self._called_names("mod.g(1)"))
        self.assertNotIn("h", self._called_names('"""h is forbidden"""'))

    def test_the_module_does_read_the_stored_anchor(self):
        # A file that dropped the anchor entirely would satisfy the gate above.
        self.assertIn("anchor_session", _SCRIPT.read_text())


class TestCatalystMass(unittest.TestCase):
    def test_all_noise_is_zero_and_all_catalyst_is_one(self):
        self.assertAlmostEqual(
            disc.catalyst_mass(json.dumps({"opinion": 0.7, "listicle": 0.3})), 0.0
        )
        self.assertAlmostEqual(
            disc.catalyst_mass(json.dumps({"m_and_a": 0.9, "earnings": 0.1})), 1.0
        )

    def test_a_split_distribution_is_the_non_noise_share(self):
        self.assertAlmostEqual(
            disc.catalyst_mass(json.dumps({"opinion": 0.4, "earnings": 0.6})), 0.6
        )

    def test_an_unreadable_distribution_is_None_and_not_a_confident_zero(self):
        # A row we could not read is not a row whose catalyst mass is zero. Imputing
        # one would put a confident value where there is no measurement, and the fit
        # would treat it as evidence.
        for bad in (None, "", "not json", "[1,2]", json.dumps("a string"), 7):
            self.assertIsNone(disc.catalyst_mass(bad), repr(bad))

    def test_it_tracks_the_LIVE_noise_vocabulary(self):
        # A hand-copied noise list would drift from the pipeline's, and a drifted copy
        # does not fail: the mass is just computed over the wrong set.
        from alphalens_pipeline.thematic.screening.catalyst_signals import NOISE_EVENT_TYPES

        for noise_type in NOISE_EVENT_TYPES:
            self.assertAlmostEqual(
                disc.catalyst_mass(json.dumps({noise_type: 1.0})), 0.0, msg=noise_type
            )

    def test_a_non_noise_type_the_pipeline_adds_later_counts_as_catalyst(self):
        self.assertAlmostEqual(disc.catalyst_mass(json.dumps({"some_future_type": 1.0})), 1.0)

    def test_the_result_is_clamped_to_the_unit_interval(self):
        # Probabilities that do not sum to one must not produce a negative mass.
        self.assertAlmostEqual(disc.catalyst_mass(json.dumps({"opinion": 1.4})), 0.0)


class TestTheLoadersRefuseAnEmptyStore(unittest.TestCase):
    """Running this before the builder must say so, not return an empty panel that
    reads as 'the features carry nothing'."""

    def test_the_article_loader_names_the_builder_to_run(self):
        with mock.patch.object(disc.glob, "glob", return_value=[]):
            with self.assertRaises(RuntimeError) as caught:
                disc.load_jev_article()
        self.assertIn("2026_10_jev_feature_layer.py", str(caught.exception))

    def test_the_candidate_loader_names_the_builder_to_run(self):
        with mock.patch.object(disc.glob, "glob", return_value=[]):
            with self.assertRaises(RuntimeError) as caught:
                disc.load_jev_candidate()
        self.assertIn("2026_10_jev_feature_layer.py", str(caught.exception))


class TestStandardise(unittest.TestCase):
    def test_a_standardised_column_has_zero_mean_and_unit_sd(self):
        frame = pd.DataFrame({"x": [1.0, 2.0, 3.0, 4.0]})
        out = disc.standardise(frame, ["x"])
        self.assertAlmostEqual(out["x"].mean(), 0.0)
        self.assertAlmostEqual(out["x"].std(ddof=0), 1.0)

    def test_a_constant_column_becomes_nan_rather_than_dividing_by_zero(self):
        # A feature with no variance cannot have a coefficient; NaN drops the rows
        # from the fit instead of producing an infinity.
        out = disc.standardise(pd.DataFrame({"x": [2.0, 2.0, 2.0]}), ["x"])
        self.assertTrue(out["x"].isna().all())


class TestTheMinimumDetectableEffect(unittest.TestCase):
    """The deliverable. A coefficient without it cannot distinguish 'no effect' from
    'no power', and on 2026-10-01 a null of mine was the latter."""

    def test_the_z_multiplier_is_two_sided_five_percent_at_eighty_percent_power(self):
        self.assertAlmostEqual(disc._MDE_Z, 1.959964 + 0.841621, places=5)

    def test_the_multiplier_is_the_one_that_reproduces_the_published_bound(self):
        # Reproduces the 2026-10-01 self-grade row: se 0.0102, sd(label) 0.1713 gave
        # an MDE of 0.167 standardised. If the multiplier ever changes, that published
        # number stops being reproducible from this code.
        self.assertAlmostEqual(disc._MDE_Z * 0.0102 / 0.1713, 0.167, places=3)


if __name__ == "__main__":
    unittest.main()


class TestTheJoinKeyIsNormalised(unittest.TestCase):
    """The quiet bug class: keys that print identically and differ by type.

    Measured on the first run of this script: the builder writes `brief_date` as the
    parquet filename stem, a STRING, while the label store carries `datetime.date`.
    The tuple key missed 391 of 391 rows, two of the six candidates arrived all-null,
    and a pass that printed only coefficients would have read that as "they carry
    nothing".
    """

    def test_every_form_a_date_arrives_in_normalises_to_one_string(self):
        import datetime as dt

        forms = [
            dt.date(2026, 5, 19),
            "2026-05-19",
            pd.Timestamp("2026-05-19"),
            pd.Timestamp("2026-05-19 13:30:00"),
            dt.datetime(2026, 5, 19, 13, 30),
        ]
        self.assertEqual({disc.iso_date(f) for f in forms}, {"2026-05-19"})

    def test_a_missing_date_is_empty_rather_than_the_text_none(self):
        self.assertEqual(disc.iso_date(None), "")

    def test_two_keys_that_differ_only_by_type_now_match(self):
        import datetime as dt

        self.assertEqual(disc.iso_date(dt.date(2026, 5, 19)), disc.iso_date("2026-05-19"))


class TestATotalJoinMissIsRefused(unittest.TestCase):
    """A join that matches NOTHING is a key bug, never a property of the data.

    Without this the pass reports full coverage of the controls, zero coverage of two
    candidates, and a reader who skips the coverage block concludes the features are
    worthless. The first run of this script produced exactly that shape.
    """

    def test_the_message_says_it_is_a_key_bug_and_names_the_side(self):
        source = _SCRIPT.read_text()
        self.assertIn("join-key bug", source)
        self.assertIn("no_candidate_feature", source)
        self.assertIn("no_article_feature", source)

    def test_the_guard_compares_the_miss_count_against_the_row_count(self):
        # A guard that fired on "some misses" would refuse a legitimately sparse
        # join; it must fire only when EVERY row missed.
        self.assertIn("missed == len(rows)", _SCRIPT.read_text())


def _bdates(count: int, *, step: int = 10, start: str = "2025-07-01") -> list[dt.date]:
    """`count` business days spaced `step` sessions apart, so none of them chain.

    The episode rule collapses same-ticker rows whose arrival sessions sit within five
    trading sessions of each other, so a spacing above that keeps one row per episode
    and lets a test assert the collapse only where it builds one on purpose.

    The step clears the window with room to spare because a gap counted in calendar
    business days is SHORTER in trading sessions: every exchange holiday inside the gap
    removes one session from it. At six the first half of 2026 holds four holidays and
    24 of 120 rows collapsed on their own.
    """
    grid = pd.bdate_range(start=start, periods=count * step)
    return [d.date() for d in grid[::step][:count]]


def _url(day: dt.date, ticker: str) -> str:
    return f"https://example.invalid/{day.isoformat()}/{ticker}"


class _SyntheticStore:
    """A four-store `~/.alphalens` small enough to drive the whole pass in a test.

    The pass reads four stores that key the same row four ways, which is where its one
    measured bug lived, so a test that fakes a single already-joined frame would not be
    able to catch that class of defect again.
    """

    def __init__(
        self,
        root: Path,
        *,
        n_dates: int = 20,
        tickers: tuple[str, ...] = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"),
        article_version: str | None = None,
        candidate_date_as_text: bool = True,
    ) -> None:
        self.root = root
        self.dates = _bdates(n_dates)
        self.tickers = tickers
        self.article_version = article_version or disc.FEATURE_VERSION
        self.candidate_date_as_text = candidate_date_as_text
        self.label_rows: list[dict] = []
        self.brief_rows: dict[dt.date, list[dict]] = {}
        self.article_rows: list[dict] = []
        self.candidate_rows: list[dict] = []
        self._fill()

    def _fill(self) -> None:
        # Every generated date must sit inside the burnt window, or the pass drops the
        # tail at the cutoff and the row counts stop matching the fixture. Asserting it
        # here turns a spacing change into one clear failure instead of three count
        # mismatches in unrelated tests.
        assert self.dates[-1].isoformat() <= disc.BURNT_CUTOFF, (
            f"the fixture runs to {self.dates[-1]}, past the burnt cutoff "
            f"{disc.BURNT_CUTOFF}; move the start date or shorten the spacing"
        )
        rng = __import__("numpy").random.default_rng(7)
        sessions = pd.bdate_range(start=self.dates[0], periods=400)
        for di, day in enumerate(self.dates):
            anchor = next(s.date() for s in sessions if s.date() > day)
            for ti, ticker in enumerate(self.tickers):
                gain = float(rng.normal())
                noise = float(rng.uniform(0.05, 0.6))
                self.label_rows.append(
                    {
                        "ticker": ticker,
                        "brief_date": day,
                        disc.LABEL_STATUS: "ok",
                        # the label moves with one candidate, so the fit is not degenerate
                        disc.LABEL: 0.01 * gain + 0.02 * float(rng.normal()),
                        "anchor_session": anchor,
                    }
                )
                self.brief_rows.setdefault(day, []).append(
                    {
                        "ticker": ticker,
                        "technical_atr_pct": 2.0 + 0.5 * float(rng.normal()),
                        "technical_ma50_distance_pct": float(rng.normal()),
                        "source_event_url": _url(day, ticker),
                    }
                )
                self.article_rows.append(
                    {
                        "url": _url(day, ticker),
                        "jev_materiality": float(rng.uniform(0, 1)),
                        "jev_concrete_fact": float(rng.uniform(0, 1)),
                        "jev_event_type_confidence": float(rng.uniform(0, 1)),
                        # the noise share has to VARY: a constant column standardises to
                        # all-NaN by design, and every complete case would then be dropped
                        "jev_event_type_probs_json": json.dumps(
                            {"earnings": 1.0 - noise, "opinion": noise}
                        ),
                        "jev_feature_version": self.article_version,
                    }
                )
                self.candidate_rows.append(
                    {
                        "brief_date": day.isoformat() if self.candidate_date_as_text else day,
                        "ticker": ticker,
                        "jev_touches_industry": float(rng.uniform(0, 1)),
                        "jev_company_gain": gain,
                        "jev_feature_version": disc.FEATURE_VERSION,
                    }
                )
                _ = di, ti

    def write(self) -> Path:
        labels = self.root / "selection_labels"
        briefs = self.root / "thematic_briefs"
        article = self.root / "jev_features" / "article"
        candidate = self.root / "jev_features" / "candidate"
        for folder in (labels, briefs, article, candidate):
            folder.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(self.label_rows).to_parquet(labels / "labels.parquet")
        for day, rows in self.brief_rows.items():
            pd.DataFrame(rows).to_parquet(briefs / f"{day.isoformat()}.parquet")
        pd.DataFrame(self.article_rows).to_parquet(article / "a.parquet")
        pd.DataFrame(self.candidate_rows).to_parquet(candidate / "c.parquet")
        return self.root


class _StoreCase(unittest.TestCase):
    """Base for the tests that need the four stores on disk."""

    def store(self, **kwargs) -> _SyntheticStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        built = _SyntheticStore(Path(tmp.name), **kwargs)
        return built

    def use(self, built: _SyntheticStore) -> None:
        root = built.write()
        patcher = mock.patch.object(disc.edge_stores, "HOME", root)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 10 000 draws per coefficient is the published setting and far too slow for a
        # test; the number under test is the plumbing, never a p-value's last digit.
        cheap = mock.patch.object(disc, "N_BOOT", 49)
        cheap.start()
        self.addCleanup(cheap.stop)


class TestThePanelIsBuiltFromFourStores(_StoreCase):
    def test_every_store_reaches_the_panel(self):
        built = self.store()
        self.use(built)
        panel, diag = disc.build_panel()
        self.assertEqual(diag["pre_join"], len(built.label_rows))
        self.assertEqual(diag["joined"], len(built.label_rows))
        for column in ("atr_source_check", "jev_materiality", "jev_company_gain"):
            if column == "atr_source_check":
                self.assertIn("technical_atr_pct", panel.columns)
                continue
            self.assertIn(column, panel.columns)
            self.assertTrue(panel[column].notna().all(), f"{column} arrived all-null")

    def test_a_label_row_with_no_brief_is_counted_and_dropped(self):
        built = self.store()
        orphan = built.dates[0]
        built.brief_rows[orphan] = [r for r in built.brief_rows[orphan] if r["ticker"] != "AAA"]
        self.use(built)
        _, diag = disc.build_panel()
        self.assertEqual(diag["no_brief"], 1)
        self.assertEqual(diag["joined"], diag["pre_join"] - 1)

    def test_a_brief_date_past_the_cutoff_never_enters_the_panel(self):
        built = self.store()
        late = dt.date(2026, 8, 3)
        built.label_rows.append(
            {
                "ticker": "ZZZ",
                "brief_date": late,
                disc.LABEL_STATUS: "ok",
                disc.LABEL: 0.5,
                "anchor_session": dt.date(2026, 8, 4),
            }
        )
        built.brief_rows[late] = [
            {
                "ticker": "ZZZ",
                "technical_atr_pct": 2.0,
                "technical_ma50_distance_pct": 0.0,
                "source_event_url": _url(late, "ZZZ"),
            }
        ]
        self.use(built)
        panel, _ = disc.build_panel()
        self.assertNotIn("ZZZ", set(panel["ticker"]))

    def test_a_row_whose_status_is_not_ok_never_enters_the_panel(self):
        built = self.store()
        built.label_rows[0][disc.LABEL_STATUS] = "immature"
        self.use(built)
        _, diag = disc.build_panel()
        self.assertEqual(diag["pre_join"], len(built.label_rows) - 1)

    def test_the_arrival_is_the_stored_anchor_and_not_the_brief_date(self):
        built = self.store()
        planted = dt.date(2026, 12, 31)
        built.label_rows[0]["anchor_session"] = planted
        self.use(built)
        panel, _ = disc.build_panel()
        row = panel[
            (panel["ticker"] == built.label_rows[0]["ticker"])
            & (panel["brief_date"] == built.label_rows[0]["brief_date"])
        ]
        self.assertEqual(len(row), 1)
        self.assertEqual(row["arrival"].iloc[0], planted)

    def test_the_candidate_join_matches_although_the_stores_key_the_date_differently(self):
        # The label store carries datetime.date and the builder writes the ISO stem, so
        # the two never compare equal without the normaliser. Measured cost of getting
        # this wrong: 391 of 391 candidate joins missed, in silence.
        built = self.store(candidate_date_as_text=True)
        self.use(built)
        _, diag = disc.build_panel()
        self.assertEqual(diag["no_candidate_feature"], 0)

    def test_a_feature_written_under_another_version_is_not_pooled(self):
        built = self.store()
        for row in built.article_rows[:10]:
            row["jev_feature_version"] = "jev-v1-deadbeefcafe"
        self.use(built)
        panel, diag = disc.build_panel()
        self.assertEqual(diag["no_article_feature"], 10)
        self.assertEqual(int(panel["jev_materiality"].isna().sum()), 10)

    def test_a_store_holding_only_another_version_is_refused_as_a_key_bug(self):
        # Every row missing is never a property of the data. Reporting it as coverage
        # would let the pass print "carries nothing" about columns it never read.
        built = self.store(article_version="jev-v1-deadbeefcafe")
        self.use(built)
        with self.assertRaises(RuntimeError) as caught:
            disc.build_panel()
        message = str(caught.exception)
        self.assertIn("article", message)
        self.assertIn("join-key bug", message)

    def test_two_brief_rows_for_one_key_are_refused_by_name(self):
        built = self.store()
        day = built.dates[0]
        built.brief_rows[day].append(dict(built.brief_rows[day][0]))
        self.use(built)
        with self.assertRaises(AssertionError) as caught:
            disc.build_panel()
        message = str(caught.exception)
        self.assertIn("more than one row", message)
        self.assertIn(repr(day), message)
        self.assertIn("AAA", message)

    def test_repeat_appearances_inside_the_window_collapse_into_one_episode(self):
        built = self.store()
        first = built.dates[0]
        close = (pd.Timestamp(first) + pd.offsets.BDay(2)).date()
        built.label_rows.append(
            {
                "ticker": "AAA",
                "brief_date": close,
                disc.LABEL_STATUS: "ok",
                disc.LABEL: 0.03,
                "anchor_session": (pd.Timestamp(close) + pd.offsets.BDay(1)).date(),
            }
        )
        built.brief_rows[close] = [
            {
                "ticker": "AAA",
                "technical_atr_pct": 2.0,
                "technical_ma50_distance_pct": 0.0,
                "source_event_url": _url(close, "AAA"),
            }
        ]
        self.use(built)
        _, diag = disc.build_panel()
        self.assertEqual(diag["joined"], len(built.label_rows))
        self.assertEqual(diag["episodes"], diag["joined"] - 1)
        self.assertEqual(diag["clusters"], len({r["anchor_session"] for r in built.label_rows}) - 1)


class TestTheJointFit(_StoreCase):
    def _panel(self):
        built = self.store()
        self.use(built)
        panel, _ = disc.build_panel()
        for src, name in disc.CONTROLS + disc.CANDIDATES:
            panel[name] = pd.to_numeric(panel.get(src), errors="coerce")
        return disc.standardise(panel, [n for _, n in disc.CONTROLS + disc.CANDIDATES])

    def test_too_few_complete_cases_returns_None_rather_than_a_coefficient(self):
        panel = self._panel().head(19)
        self.assertIsNone(disc.joint_fit(panel, ["atr"]))

    def test_one_row_per_regressor_each_carrying_a_clustered_p_value(self):
        panel = self._panel()
        out = disc.joint_fit(panel, ["atr", "ma50_dist", "jev_materiality"])
        self.assertIsNotNone(out)
        self.assertEqual([r["name"] for r in out], ["atr", "ma50_dist", "jev_materiality"])
        for row in out:
            self.assertEqual(row["clusters"], panel["arrival"].astype(str).nunique())
            self.assertEqual(row["n"], len(panel))
            self.assertGreater(row["p_wcb"], 0.0)
            self.assertLessEqual(row["p_wcb"], 1.0)

    def test_the_constant_is_fitted_but_never_reported(self):
        out = disc.joint_fit(self._panel(), ["atr"])
        self.assertEqual(len(out), 1)
        self.assertNotIn("const", [r["name"] for r in out])

    def test_the_standard_error_is_the_one_the_reported_t_implies(self):
        row = disc.joint_fit(self._panel(), ["atr", "jev_company_gain"])[1]
        self.assertAlmostEqual(row["se"], abs(row["beta"] / row["t_cr2"]), places=12)


class TestTheWholePass(_StoreCase):
    def _run(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = disc.run()
        return code, buffer.getvalue()

    def test_it_exits_zero_and_names_the_instrument_and_the_cutoff(self):
        self.use(self.store())
        code, text = self._run()
        self.assertEqual(code, 0)
        self.assertIn(disc.FEATURE_VERSION, text)
        self.assertIn(disc.BURNT_CUTOFF, text)

    def test_it_says_on_its_own_face_that_it_charges_nothing_and_proves_nothing(self):
        # ADR 0013 R4 pays 0 only while nothing is cited as evidence for what ships, so
        # the output has to carry that sentence wherever it is pasted.
        self.use(self.store())
        _, text = self._run()
        self.assertIn("charges 0", text)
        self.assertIn("ADR 0013 R4", text)

    def test_it_reports_every_candidate_in_both_the_fit_and_the_mde_table(self):
        self.use(self.store())
        _, text = self._run()
        for _, name in disc.CANDIDATES:
            self.assertGreaterEqual(text.count(name), 2, f"{name} missing from a section")
        self.assertIn("powered?", text)

    def test_it_prints_the_build_counts_so_a_silent_join_miss_is_visible(self):
        self.use(self.store())
        _, text = self._run()
        self.assertIn("no brief", text)
        self.assertIn("no article feature", text)
        self.assertIn("no candidate feature", text)
        self.assertIn("arrival clusters", text)

    def test_it_reports_the_reference_line_as_descriptive_and_not_as_a_bar(self):
        self.use(self.store())
        _, text = self._run()
        self.assertIn("NOT a bar", text)
        self.assertIn(f"{disc.REFERENCE_BAR:.4f}", text)

    def test_a_panel_too_small_to_fit_exits_non_zero_and_says_so(self):
        self.use(self.store(n_dates=2, tickers=("AAA",)))
        code, text = self._run()
        self.assertEqual(code, 1)
        self.assertIn("too few complete cases", text)

    def test_the_run_flag_is_required_because_the_pass_reads_burnt_values(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                disc.main([])
        self.assertNotEqual(caught.exception.code, 0)

    def test_main_with_the_flag_drives_the_pass(self):
        self.use(self.store())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(disc.main(["--run"]), 0)


class TestACandidateItCouldNotEstimate(_StoreCase):
    """A candidate with too few complete cases must never read as a measured zero."""

    def test_the_pass_stops_rather_than_reporting_a_number_for_it(self):
        built = self.store()
        for row in built.candidate_rows[2:]:
            row["jev_touches_industry"] = float("nan")
        self.use(built)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = disc.run()
        text = buffer.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("too few complete cases to fit", text)
        # the coverage line still names the shortfall, so the reason is on the page
        self.assertRegex(text, r"jev_touches_industry\s+2 / 120")
        # and no candidate got a coefficient out of a panel that could not be fitted
        self.assertNotIn("p_wcb", text)

    def test_a_per_candidate_fit_can_never_have_fewer_cases_than_the_joint_one(self):
        """Why `run` has no per-candidate 'could not fit' path that fires.

        Each per-candidate fit regresses on the two controls plus one candidate, a
        SUBSET of the joint fit's columns, and `joint_fit` drops a row only when one of
        its own columns is missing. So the per-candidate complete-case count is always
        at least the joint one, and `run` returns 1 at the joint fit before it could
        reach a per-candidate shortfall. Pinning the relationship records that as a
        measured fact rather than leaving it to be re-derived.
        """
        built = self.store()
        for row in built.candidate_rows[:40]:
            row["jev_touches_industry"] = float("nan")
        self.use(built)
        panel, _ = disc.build_panel()
        for src, name in disc.CONTROLS + disc.CANDIDATES:
            panel[name] = pd.to_numeric(panel.get(src), errors="coerce")
        std = disc.standardise(panel, [n for _, n in disc.CONTROLS + disc.CANDIDATES])
        joint = disc.joint_fit(std, [n for _, n in disc.CONTROLS + disc.CANDIDATES])
        self.assertIsNotNone(joint, "the joint fit must succeed for the claim to bite")
        for _, name in disc.CANDIDATES:
            one = disc.joint_fit(std, [n for _, n in disc.CONTROLS] + [name])
            self.assertIsNotNone(one, f"{name} fitted alone where the joint fit worked")
            self.assertGreaterEqual(one[0]["n"], joint[0]["n"])
