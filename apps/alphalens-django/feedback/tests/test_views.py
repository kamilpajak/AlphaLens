"""End-to-end DRF tests for ``/v1/feedback/*``.

Each test overrides ``ALPHALENS_FEEDBACK_DB`` to a tmp path so the real
``~/.alphalens/feedback.db`` is never touched. Auth is bypassed in
``config.settings.dev`` via ``DEFAULT_PERMISSION_CLASSES = AllowAny``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.test import override_settings
from rest_framework.test import APIClient


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def feedback_db(tmp_path: Path):
    path = tmp_path / "feedback.db"
    # Also pin ALPHALENS_VIX_CACHE to a (by-default absent) tmp path so the
    # POST regime stamp is deterministically "unknown" unless a test seeds it
    # — never reads a real ~/.alphalens VIX cache on the dev machine.
    with override_settings(
        ALPHALENS_FEEDBACK_DB=str(path),
        ALPHALENS_VIX_CACHE=str(tmp_path / "vix_regime_cache.json"),
    ):
        yield path


def _post_interested(client: APIClient, **overrides):
    body = {
        "brief_date": "2026-05-28",
        "ticker": "NVDA",
        "theme": "ai_infrastructure",
        "surfaced_at": "2026-05-28T06:30:00+00:00",
        "action": "interested",
    }
    body.update(overrides)
    return client.post("/v1/feedback/decisions", body, format="json")


def _post_dismissed_wrong_theme(client: APIClient, **overrides):
    body = {
        "brief_date": "2026-05-28",
        "ticker": "NVDA",
        "theme": "ai_infrastructure",
        "surfaced_at": "2026-05-28T06:30:00+00:00",
        "action": "dismissed",
        "dismiss_category": "thesis_setup",
        "dismiss_reason": "wrong_theme",
    }
    body.update(overrides)
    return client.post("/v1/feedback/decisions", body, format="json")


class TestPostDecision:
    """POST /v1/feedback/decisions — create + upsert + validation."""

    def test_interested_returns_201_with_id(self, client, feedback_db):
        resp = _post_interested(client)
        assert resp.status_code == 201
        body = resp.json()
        assert "id" in body
        assert body["action"] == "interested"
        assert body["brief_date"] == "2026-05-28"

    def test_dismissed_with_valid_pair_returns_201(self, client, feedback_db):
        resp = _post_dismissed_wrong_theme(client)
        assert resp.status_code == 201
        body = resp.json()
        assert body["action"] == "dismissed"
        assert body["dismiss_category"] == "thesis_setup"
        assert body["dismiss_reason"] == "wrong_theme"

    def test_invalid_action_returns_400(self, client, feedback_db):
        resp = _post_interested(client, action="bookmark")
        assert resp.status_code == 400

    def test_dismissed_without_category_returns_400(self, client, feedback_db):
        body = {
            "brief_date": "2026-05-28",
            "ticker": "NVDA",
            "theme": "ai_infrastructure",
            "surfaced_at": "2026-05-28T06:30:00+00:00",
            "action": "dismissed",
        }
        resp = client.post("/v1/feedback/decisions", body, format="json")
        assert resp.status_code == 400

    def test_dismissed_with_mismatched_pair_returns_400(self, client, feedback_db):
        # wrong_theme belongs to thesis_setup, not risk_quality
        resp = _post_dismissed_wrong_theme(client, dismiss_category="risk_quality")
        assert resp.status_code == 400

    def test_dismissed_other_without_note_returns_400(self, client, feedback_db):
        resp = _post_dismissed_wrong_theme(
            client,
            dismiss_category="other",
            dismiss_reason="other",
        )
        assert resp.status_code == 400

    def test_dismiss_note_over_200_chars_returns_400(self, client, feedback_db):
        # Server-side max_length mirrors the SPA's maxlength=200 so an
        # oversized note is rejected symmetrically rather than persisted.
        resp = _post_dismissed_wrong_theme(
            client,
            dismiss_category="other",
            dismiss_reason="other",
            dismiss_note="x" * 201,
        )
        assert resp.status_code == 400

    def test_dismiss_note_at_200_chars_accepted(self, client, feedback_db):
        resp = _post_dismissed_wrong_theme(
            client,
            dismiss_category="other",
            dismiss_reason="other",
            dismiss_note="x" * 200,
        )
        assert resp.status_code == 201

    def test_watching_action_accepted(self, client, feedback_db):
        resp = _post_interested(client, action="watching")
        assert resp.status_code == 201
        assert resp.json()["action"] == "watching"

    def test_confidence_subjective_persisted(self, client, feedback_db):
        resp = _post_interested(client, confidence_subjective=4)
        assert resp.status_code == 201
        assert resp.json()["confidence_subjective"] == 4

    def test_confidence_subjective_out_of_range_returns_400(self, client, feedback_db):
        resp = _post_interested(client, confidence_subjective=10)
        assert resp.status_code == 400

    def test_upsert_flips_interested_to_dismissed(self, client, feedback_db):
        # Same (brief_date, ticker, theme) — second POST replaces first.
        # First call creates → 201. Second call upserts → 200 per REST
        # convention (zen pre-merge #5).
        r1 = _post_interested(client)
        assert r1.status_code == 201
        r2 = _post_dismissed_wrong_theme(client)
        assert r2.status_code == 200
        # id preserved across the upsert so the SPA's local undo reference
        # stays valid after flipping the action.
        assert r2.json()["id"] == r1.json()["id"]
        # GET back: exactly one row, now dismissed
        rows = client.get("/v1/feedback/decisions?brief_date=2026-05-28").json()
        assert len(rows["data"]) == 1
        assert rows["data"][0]["action"] == "dismissed"


class TestGetDecisions:
    """GET /v1/feedback/decisions — list by brief_date."""

    def test_empty_when_no_decisions(self, client, feedback_db):
        resp = client.get("/v1/feedback/decisions?brief_date=2026-05-28")
        assert resp.status_code == 200
        assert resp.json() == {"data": []}

    def test_lists_decisions_for_date(self, client, feedback_db):
        _post_interested(client)
        _post_interested(client, ticker="AMD", theme="ai_infrastructure")
        resp = client.get("/v1/feedback/decisions?brief_date=2026-05-28")
        assert resp.status_code == 200
        rows = resp.json()["data"]
        assert {r["ticker"] for r in rows} == {"NVDA", "AMD"}

    def test_missing_brief_date_param_returns_400(self, client, feedback_db):
        resp = client.get("/v1/feedback/decisions")
        assert resp.status_code == 400

    def test_malformed_brief_date_returns_400(self, client, feedback_db):
        resp = client.get("/v1/feedback/decisions?brief_date=not-a-date")
        assert resp.status_code == 400


class TestDeleteDecision:
    """DELETE /v1/feedback/decisions/<id> — idempotent undo."""

    def test_delete_existing_returns_204_and_removes(self, client, feedback_db):
        row = _post_interested(client).json()
        resp = client.delete(f"/v1/feedback/decisions/{row['id']}")
        assert resp.status_code == 204
        rows = client.get("/v1/feedback/decisions?brief_date=2026-05-28").json()
        assert rows["data"] == []

    def test_delete_unknown_id_returns_204(self, client, feedback_db):
        # Idempotent: SPA may double-fire undo on slow network.
        resp = client.delete("/v1/feedback/decisions/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 204


class TestOutcomeFields:
    """v2 outcome-join read-only fields on the decision envelope."""

    _OUTCOME_KEYS = {
        "outcome_plan_id",
        "fill_status",
        "exit_kind",
        "shadow_return",
        "realized_return",
        "outcome_computed_at",
    }

    def test_get_carries_nullable_outcome_fields(self, client, feedback_db):
        # A freshly-POSTed (not-yet-joined) decision exposes all outcome
        # fields as null so the SPA can render "outcome pending".
        _post_interested(client)
        rows = client.get("/v1/feedback/decisions?brief_date=2026-05-28").json()["data"]
        assert len(rows) == 1
        for key in self._OUTCOME_KEYS:
            assert key in rows[0], f"missing outcome key {key}"
            assert rows[0][key] is None

    def test_post_ignores_user_supplied_outcome_fields(self, client, feedback_db):
        # Outcome fields are job-set, never user-writable. A POST body
        # carrying fill_status must NOT persist it (the request serializer
        # has no such field, so it is dropped).
        resp = _post_interested(client, fill_status="FILLED", outcome_plan_id="hack-1")
        assert resp.status_code == 201
        body = resp.json()
        assert body["fill_status"] is None
        assert body["outcome_plan_id"] is None

    def test_serialiser_and_response_serializer_field_parity(self):
        # _serialise_decision (the actual JSON) and DecisionResponseSerializer
        # (the OpenAPI contract) are hand-mirrored; pin them so drf-spectacular
        # never emits a schema that disagrees with the wire output.
        import datetime as dt

        from feedback.serializers import DecisionResponseSerializer

        from alphalens_pipeline.feedback.store import Decision
        from feedback.views import _serialise_decision

        d = Decision(
            brief_date=dt.date(2026, 5, 28),
            ticker="NVDA",
            theme="ai",
            surfaced_at=dt.datetime(2026, 5, 28, 6, 30, tzinfo=dt.UTC),
            action="interested",
            action_at=dt.datetime(2026, 5, 28, 8, 0, tzinfo=dt.UTC),
        )
        wire_keys = set(_serialise_decision(d).keys())
        schema_keys = set(DecisionResponseSerializer().fields.keys())
        assert wire_keys == schema_keys


class TestPostDecisionWithVixCache:
    """v2 PR-2: market_regime_at_entry is stamped from the server-side VIX cache.

    The hot path reads a local JSON cache (no network) and degrades to
    "unknown" on any miss/stale/unreadable case — never blocking the decision.
    """

    def _seed_vix(self, tmp_path: Path, *, vix, age_hours: float = 0.0):
        import datetime as dt
        import json

        fetched_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=age_hours)
        (tmp_path / "vix_regime_cache.json").write_text(
            json.dumps(
                {
                    "observation_date": "2026-05-29",
                    "vix": vix,
                    "fetched_at": fetched_at.isoformat(),
                    "series": "VIXCLS",
                }
            )
        )

    def test_stamps_unknown_when_cache_absent(self, client, feedback_db, tmp_path):
        resp = _post_interested(client)
        assert resp.status_code == 201
        assert resp.json()["market_regime_at_entry"] == "unknown"

    def test_stamps_bucket_from_fresh_cache(self, client, feedback_db, tmp_path):
        self._seed_vix(tmp_path, vix=22.0)
        resp = _post_interested(client)
        assert resp.status_code == 201
        assert resp.json()["market_regime_at_entry"] == "mid"

    def test_stamps_unknown_when_cache_stale(self, client, feedback_db, tmp_path):
        self._seed_vix(tmp_path, vix=22.0, age_hours=97)
        resp = _post_interested(client)
        assert resp.status_code == 201
        assert resp.json()["market_regime_at_entry"] == "unknown"

    def test_does_not_block_on_unreadable_cache(self, client, feedback_db, tmp_path):
        (tmp_path / "vix_regime_cache.json").write_text("not json {")
        resp = _post_interested(client)
        # Never 500 — the decision still persists, regime degrades to unknown.
        assert resp.status_code == 201
        assert resp.json()["market_regime_at_entry"] == "unknown"


class TestTaxonomyEndpoint:
    """GET /v1/feedback/taxonomy — exposes the locked dismiss taxonomy."""

    def test_taxonomy_returns_4_categories(self, client):
        resp = client.get("/v1/feedback/taxonomy")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["categories"].keys()) == {
            "thesis_setup",
            "risk_quality",
            "portfolio_style",
            "other",
        }

    def test_taxonomy_locked_pairs_match_pipeline(self, client):
        resp = client.get("/v1/feedback/taxonomy")
        body = resp.json()
        assert body["categories"]["thesis_setup"] == [
            "wrong_theme",
            "too_expensive",
            "bad_setup",
        ]
        assert body["categories"]["portfolio_style"] == [
            "already_have_exposure",
            "liquidity_too_low",
            "not_my_style",
        ]
        assert body["actions"] == [
            "interested",
            "watching",
            "dismissed",
            "paper_traded",
            "live_traded",
        ]
