"""End-to-end DRF tests: ingest a fake parquet directory, then exercise /v1/*.

Each test uses ``DRFAPIClient`` and verifies both HTTP status and the envelope
shape so the frontend cutover (F6) finds the JSON identical to the legacy
FastAPI it replaces.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from rest_framework.test import APIClient

from briefs.api.serializers import CandidateDetailSerializer, CandidateSerializer
from briefs.ingest.parquet import rebuild_from_parquet


def _write_parquet(directory: Path, iso_date: str, rows: list[dict]) -> Path:
    path = directory / f"{iso_date}.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def _two_day_fixture(tmp_path: Path) -> None:
    """Two days × multiple themes; covers list, range, theme group, ticker history."""
    _write_parquet(
        tmp_path,
        "2026-05-21",
        [
            {
                "ticker": "NVDA",
                "theme": "ai-infra",
                "layer4_weighted_score": 12,
                "gates_passed": ["pe"],
                "n_gates_passed": 1,
            },
            {
                "ticker": "AVGO",
                "theme": "ai-infra",
                "layer4_weighted_score": 8,
                "gates_passed": [],
                "n_gates_passed": 0,
            },
            {
                "ticker": "ASML",
                "theme": "lithography",
                "layer4_weighted_score": 5,
                "gates_passed": ["pe", "fcff"],
                "n_gates_passed": 2,
            },
        ],
    )
    _write_parquet(
        tmp_path,
        "2026-05-22",
        [
            {
                "ticker": "NVDA",
                "theme": "ai-infra",
                "layer4_weighted_score": 15,
                "gates_passed": ["pe", "fcff"],
                "n_gates_passed": 2,
            },
            {
                "ticker": "QUBT",
                "theme": "quantum",
                "layer4_weighted_score": 9,
                "gates_passed": [],
                "n_gates_passed": 0,
            },
        ],
    )
    rebuild_from_parquet(briefs_dir=tmp_path)


def _rank_tie_fixture(tmp_path: Path) -> None:
    """Two candidates with the SAME ``layer4_weighted_score`` but a ``rank_in_day``
    order that disagrees with alphabetical ticker order.

    Reproduces the badge-vs-position bug: the pipeline breaks the score tie with
    its full sort chain and stamps ``rank_in_day`` (SAIC = 1); a secondary sort
    on ``ticker`` breaks the same tie alphabetically (ANET first), so a card
    labelled "RANK 01" renders at list position 2. The API must honour the
    pre-computed ``rank_in_day`` so badge and position agree.
    """
    _write_parquet(
        tmp_path,
        "2026-05-30",
        [
            {
                "ticker": "SAIC",
                "theme": "defense",
                "layer4_weighted_score": 10,
                "rank_in_day": 1,
                "cohort_size_in_day": 2,
                "gates_passed": [],
                "n_gates_passed": 0,
            },
            {
                "ticker": "ANET",
                "theme": "defense",
                "layer4_weighted_score": 10,
                "rank_in_day": 2,
                "cohort_size_in_day": 2,
                "gates_passed": [],
                "n_gates_passed": 0,
            },
        ],
    )
    rebuild_from_parquet(briefs_dir=tmp_path)


def _mixed_null_rank_fixture(tmp_path: Path) -> None:
    """One day mixing ranked and unranked rows. The unranked row carries the
    HIGHEST ``layer4_weighted_score`` and sorts first alphabetically, so only a
    correct ``nulls_last`` keeps it below the ranked rows (any pure score/ticker
    order would float it to the top). Ranked rows come first in rank order; the
    null row falls to the bottom and is ordered by the score/ticker fallback.
    """
    _write_parquet(
        tmp_path,
        "2026-05-31",
        [
            {
                "ticker": "AAPL",  # alpha-first AND highest score, but unranked
                "theme": "defense",
                "layer4_weighted_score": 99,
                "cohort_size_in_day": 3,
                "gates_passed": [],
                "n_gates_passed": 0,
            },
            {
                "ticker": "TSLA",
                "theme": "defense",
                "layer4_weighted_score": 5,
                "rank_in_day": 1,
                "cohort_size_in_day": 3,
                "gates_passed": [],
                "n_gates_passed": 0,
            },
            {
                "ticker": "NVDA",
                "theme": "defense",
                "layer4_weighted_score": 5,
                "rank_in_day": 2,
                "cohort_size_in_day": 3,
                "gates_passed": [],
                "n_gates_passed": 0,
            },
        ],
    )
    rebuild_from_parquet(briefs_dir=tmp_path)


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.mark.django_db
class TestDaysEndpoint:
    def test_list_returns_envelope(self, client, tmp_path):
        _two_day_fixture(tmp_path)
        resp = client.get("/v1/days")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"data", "meta"}
        assert body["meta"] == {"total": 2, "limit": 50, "offset": 0}
        # Most-recent first
        assert [d["date"] for d in body["data"]] == ["2026-05-22", "2026-05-21"]

    def test_retrieve_single_day(self, client, tmp_path):
        _two_day_fixture(tmp_path)
        resp = client.get("/v1/days/2026-05-22")
        assert resp.status_code == 200
        body = resp.json()
        assert body["date"] == "2026-05-22"
        assert body["n_candidates"] == 2
        assert body["top_theme"] == "ai-infra"
        assert body["theme_counts"] == {"ai-infra": 1, "quantum": 1}
        # Candidates sorted by score desc
        tickers = [c["ticker"] for c in body["candidates"]]
        assert tickers == ["NVDA", "QUBT"]
        # JSONField round-trip
        assert body["candidates"][0]["gates_passed"] == ["pe", "fcff"]

    def test_retrieve_orders_by_rank_in_day_not_ticker(self, client, tmp_path):
        _rank_tie_fixture(tmp_path)
        body = client.get("/v1/days/2026-05-30").json()
        # rank_in_day order (SAIC=1, ANET=2), NOT alphabetical (ANET < SAIC).
        tickers = [c["ticker"] for c in body["candidates"]]
        assert tickers == ["SAIC", "ANET"]
        assert [c["rank_in_day"] for c in body["candidates"]] == [1, 2]

    def test_day_candidates_order_by_rank_in_day(self, client, tmp_path):
        _rank_tie_fixture(tmp_path)
        body = client.get("/v1/days/2026-05-30/candidates").json()
        tickers = [c["ticker"] for c in body["data"]]
        assert tickers == ["SAIC", "ANET"]

    def test_retrieve_nulls_last_keeps_unranked_below_ranked(self, client, tmp_path):
        _mixed_null_rank_fixture(tmp_path)
        body = client.get("/v1/days/2026-05-31").json()
        tickers = [c["ticker"] for c in body["candidates"]]
        # Ranked rows first (rank 1, 2); the unranked row falls last despite its
        # higher score and alpha-first ticker — proving nulls_last.
        assert tickers == ["TSLA", "NVDA", "AAPL"]
        assert [c["rank_in_day"] for c in body["candidates"]] == [1, 2, None]

    def test_retrieve_missing_date_404(self, client, tmp_path):
        resp = client.get("/v1/days/2099-01-01")
        assert resp.status_code == 404

    def test_retrieve_bad_date_404(self, client):
        resp = client.get("/v1/days/garbage")
        assert resp.status_code == 404

    def test_day_candidates_with_theme_filter(self, client, tmp_path):
        _two_day_fixture(tmp_path)
        resp = client.get("/v1/days/2026-05-22/candidates", {"theme": "quantum"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["total"] == 1
        assert body["data"][0]["ticker"] == "QUBT"

    def test_day_candidates_min_score(self, client, tmp_path):
        _two_day_fixture(tmp_path)
        resp = client.get("/v1/days/2026-05-22/candidates", {"min_score": "10"})
        assert resp.status_code == 200
        tickers = [c["ticker"] for c in resp.json()["data"]]
        assert tickers == ["NVDA"]

    def test_list_date_range(self, client, tmp_path):
        _two_day_fixture(tmp_path)
        resp = client.get("/v1/days", {"from": "2026-05-22"})
        assert resp.status_code == 200
        assert [d["date"] for d in resp.json()["data"]] == ["2026-05-22"]


@pytest.mark.django_db
class TestThemesEndpoint:
    def test_list_aggregates(self, client, tmp_path):
        _two_day_fixture(tmp_path)
        resp = client.get("/v1/themes")
        assert resp.status_code == 200
        body = resp.json()
        themes = {t["theme"]: t for t in body["data"]}
        assert themes["ai-infra"]["n_candidates"] == 3
        assert themes["ai-infra"]["n_days"] == 2
        assert themes["ai-infra"]["first_seen"] == "2026-05-21"
        assert themes["ai-infra"]["last_seen"] == "2026-05-22"
        assert themes["quantum"]["n_candidates"] == 1

    def test_theme_candidates(self, client, tmp_path):
        _two_day_fixture(tmp_path)
        resp = client.get("/v1/themes/ai-infra/candidates")
        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["total"] == 3
        # Most recent + highest score first
        assert body["data"][0]["ticker"] == "NVDA"
        assert body["data"][0]["date"] == "2026-05-22"


@pytest.mark.django_db
class TestCandidatesEndpoint:
    def test_single_candidate(self, client, tmp_path):
        _two_day_fixture(tmp_path)
        resp = client.get("/v1/candidates/2026-05-22/NVDA")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "NVDA"
        assert body["layer4_weighted_score"] == 15

    def test_missing_returns_404(self, client, tmp_path):
        resp = client.get("/v1/candidates/2099-01-01/ZZZZ")
        assert resp.status_code == 404

    def test_lowercase_ticker_uppercased(self, client, tmp_path):
        _two_day_fixture(tmp_path)
        resp = client.get("/v1/candidates/2026-05-22/nvda")
        assert resp.status_code == 200
        assert resp.json()["ticker"] == "NVDA"


@pytest.mark.django_db
class TestTickerHistory:
    def test_history_lists_all_appearances(self, client, tmp_path):
        _two_day_fixture(tmp_path)
        resp = client.get("/v1/tickers/NVDA/history")
        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["total"] == 2
        assert [c["date"] for c in body["data"]] == ["2026-05-22", "2026-05-21"]

    def test_history_respects_date_range(self, client, tmp_path):
        _two_day_fixture(tmp_path)
        resp = client.get("/v1/tickers/NVDA/history", {"from": "2026-05-22"})
        assert resp.status_code == 200
        assert resp.json()["meta"]["total"] == 1

    def test_history_unknown_ticker_empty(self, client, tmp_path):
        _two_day_fixture(tmp_path)
        resp = client.get("/v1/tickers/ZZZZ/history")
        assert resp.status_code == 200
        assert resp.json()["meta"]["total"] == 0


@pytest.mark.django_db
class TestStats:
    def test_stats_payload(self, client, tmp_path):
        _two_day_fixture(tmp_path)
        resp = client.get("/v1/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["n_days"] == 2
        assert body["n_candidates"] == 5
        assert body["n_themes"] == 3  # ai-infra, lithography, quantum
        assert body["earliest_date"] == "2026-05-21"
        assert body["latest_date"] == "2026-05-22"
        assert body["last_rebuild_at"] is not None
        # ai-infra is the most common theme
        assert body["top_themes"][0]["theme"] == "ai-infra"
        assert body["top_themes"][0]["n_candidates"] == 3

    def test_stats_empty_db(self, client):
        resp = client.get("/v1/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "n_days": 0,
            "n_candidates": 0,
            "n_themes": 0,
            "earliest_date": None,
            "latest_date": None,
            "last_rebuild_at": None,
            "top_themes": [],
        }


@pytest.mark.django_db
class TestQueryValidation:
    def test_bad_from_date_400(self, client):
        resp = client.get("/v1/days", {"from": "not-a-date"})
        assert resp.status_code == 400

    def test_pagination_clamps_limit(self, client, tmp_path):
        _two_day_fixture(tmp_path)
        resp = client.get("/v1/days", {"limit": "10000"})
        assert resp.status_code == 200
        # max_limit=200 enforced by EnvelopePagination
        assert resp.json()["meta"]["limit"] == 200


@pytest.mark.django_db
class TestOpenAPISchema:
    def test_schema_renders_with_all_endpoints(self, client):
        resp = client.get("/api/schema/")
        assert resp.status_code == 200
        text = resp.content.decode()
        for path in [
            "/v1/days",
            "/v1/days/{date}",
            "/v1/themes",
            "/v1/themes/{theme}/candidates",
            "/v1/tickers/{ticker}/history",
            "/v1/candidates/{date}/{ticker}",
            "/v1/stats",
        ]:
            assert path in text, f"missing path in OpenAPI: {path}"


def _buffett_fixture(tmp_path: Path) -> None:
    """One day, one candidate carrying a Buffett qualitative value so the assembled
    expert_assessments blob is a real (non-null) dict."""
    _write_parquet(
        tmp_path,
        "2026-05-22",
        [
            {
                "ticker": "NVDA",
                "theme": "ai-infra",
                "layer4_weighted_score": 15,
                "buffett_moat_type": "brand",
                "buffett_understandable": True,
                "buffett_qual_config_version": "buffett-pre-registry-v0",
            }
        ],
    )
    rebuild_from_parquet(briefs_dir=tmp_path)


class TestExpertAssessmentsInBulkLists:
    """PR-5a reverses the PR-4 wire-split: the SPA card is blob-driven, so the
    expert_assessments blob ships IN the bulk candidate lists (the always-visible
    chip needs it) AND on the single-candidate detail endpoint."""

    def test_blob_on_both_serializers(self):
        # Both serializers now carry the blob (the bulk list serves the card chip).
        assert "expert_assessments" in CandidateSerializer().fields
        assert "expert_assessments" in CandidateDetailSerializer().fields
        # Identical field sets today (the detail serializer is kept for headroom).
        assert set(CandidateSerializer().fields) == set(CandidateDetailSerializer().fields)

    @pytest.mark.django_db
    def test_detail_endpoint_includes_blob(self, client, tmp_path):
        _buffett_fixture(tmp_path)
        body = client.get("/v1/candidates/2026-05-22/NVDA").json()
        assert body["expert_assessments"]["buffett"]["buffett_moat_type"] == "brand"

    @pytest.mark.django_db
    def test_bulk_lists_include_blob(self, client, tmp_path):
        _buffett_fixture(tmp_path)
        # Day brief, per-day candidates, per-theme candidates, ticker history — all
        # carry the blob so the card chip renders from c.expert_assessments.buffett.
        day = client.get("/v1/days/2026-05-22").json()
        assert day["candidates"][0]["expert_assessments"]["buffett"]["buffett_moat_type"] == "brand"
        for url in (
            "/v1/days/2026-05-22/candidates",
            "/v1/themes/ai-infra/candidates",
            "/v1/tickers/NVDA/history",
        ):
            cand = client.get(url).json()["data"][0]
            assert cand["expert_assessments"]["buffett"]["buffett_moat_type"] == "brand", url

    @pytest.mark.django_db
    def test_oneil_and_panel_surface_on_api(self, client, tmp_path):
        # PR-8a: the oneil + panel blob keys ride the same serializer, so they reach
        # the API the moment ingest assembles them — the SPA reads them in PR-8b.
        _write_parquet(
            tmp_path,
            "2026-05-22",
            [
                {
                    "ticker": "NVDA",
                    "theme": "ai-infra",
                    "layer4_weighted_score": 15,
                    "oneil_score": 72.0,
                    "oneil_new_high_split_suspected": 1.0,
                    "expert_spread": 47.0,
                    "panel_config_version": "panel-v1-absdiff-2x",
                }
            ],
        )
        rebuild_from_parquet(briefs_dir=tmp_path)
        ea = client.get("/v1/days/2026-05-22").json()["candidates"][0]["expert_assessments"]
        assert ea["oneil"]["oneil_score"] == 72.0
        assert ea["oneil"]["oneil_new_high_split_suspected"] is True
        assert ea["panel"]["expert_spread"] == 47.0
        assert ea["panel"]["panel_config_version"] == "panel-v1-absdiff-2x"


@pytest.mark.django_db
class TestAtrTiltFieldsOnAPI:
    """PR-2 contract guard: selection_score, atr_penalty, scorer_config_version must
    appear as keys in every candidate-bearing API response.  The serializers use
    Meta.exclude=("pk",), so all three fields are auto-exposed; these tests act as
    regression guards against a future _LIST_EXCLUDE / _DETAIL_EXCLUDE change or
    a model refactor silently dropping the fields (the SPA in PR-3 depends on them).
    """

    @staticmethod
    def _atr_fixture(tmp_path: "Path") -> None:
        _write_parquet(
            tmp_path,
            "2026-05-22",
            [
                {
                    "ticker": "NVDA",
                    "theme": "ai-infra",
                    "layer4_weighted_score": 15,
                    "selection_score": 72.5,
                    "atr_penalty": 3.1,
                    "scorer_config_version": "atr-tilt-v1",
                }
            ],
        )
        from briefs.ingest.parquet import rebuild_from_parquet

        rebuild_from_parquet(briefs_dir=tmp_path)

    def test_atr_fields_present_on_candidate_list(self, client, tmp_path):
        """LIST endpoint (/v1/days/<date>/candidates) exposes all three fields."""
        self._atr_fixture(tmp_path)
        body = client.get("/v1/days/2026-05-22/candidates").json()
        cand = body["data"][0]
        assert "selection_score" in cand
        assert "atr_penalty" in cand
        assert "scorer_config_version" in cand

    def test_atr_fields_present_on_candidate_detail(self, client, tmp_path):
        """DETAIL endpoint (/v1/candidates/<date>/<ticker>) exposes all three fields."""
        self._atr_fixture(tmp_path)
        body = client.get("/v1/candidates/2026-05-22/NVDA").json()
        assert "selection_score" in body
        assert "atr_penalty" in body
        assert "scorer_config_version" in body

    def test_atr_field_values_round_trip_correctly(self, client, tmp_path):
        """Values written in the parquet arrive unchanged on the detail endpoint."""
        self._atr_fixture(tmp_path)
        body = client.get("/v1/candidates/2026-05-22/NVDA").json()
        assert body["selection_score"] == pytest.approx(72.5)
        assert body["atr_penalty"] == pytest.approx(3.1)
        assert body["scorer_config_version"] == "atr-tilt-v1"

    def test_atr_fields_present_in_both_serializers(self):
        """CandidateSerializer and CandidateDetailSerializer both expose the fields
        (they share _LIST_EXCLUDE / _DETAIL_EXCLUDE = ('pk',) today, but pin this
        explicitly so a future split cannot silently drop the fields from one)."""
        assert "selection_score" in CandidateSerializer().fields
        assert "atr_penalty" in CandidateSerializer().fields
        assert "scorer_config_version" in CandidateSerializer().fields
        assert "selection_score" in CandidateDetailSerializer().fields
        assert "atr_penalty" in CandidateDetailSerializer().fields
        assert "scorer_config_version" in CandidateDetailSerializer().fields


# silence linter when datetime isn't used in this file path-wise
_ = dt
