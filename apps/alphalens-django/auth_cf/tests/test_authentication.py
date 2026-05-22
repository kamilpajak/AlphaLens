"""DRF + middleware integration tests for Cloudflare Access auth."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from auth_cf import conf
from auth_cf.authentication import CloudflareAccessAuthentication

User = get_user_model()

STRICT_REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "auth_cf.authentication.CloudflareAccessAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "PAGE_SIZE": 50,
}


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.mark.django_db
class TestDRFAuth:
    """Each test wraps the request in its own ``override_settings`` block.

    Using a fixture-level override leaked state across test files even with
    ``override_settings`` context-manager teardown: DRF caches resolved
    permission classes in ``api_settings`` and the ``setting_changed``
    signal flush wasn't reliable across the file boundary. Inline blocks
    make enter/exit boundaries explicit and confine the override to one
    request each.
    """

    def test_valid_jwt_provisions_user_and_returns_200(self, client, make_jwt):
        token = make_jwt(email="alice@example.com")
        with override_settings(REST_FRAMEWORK=STRICT_REST_FRAMEWORK):
            resp = client.get("/v1/days", HTTP_CF_ACCESS_JWT_ASSERTION=token)
        assert resp.status_code == 200
        assert User.objects.filter(username="alice@example.com").exists()

    def test_missing_jwt_returns_401(self, client):
        with override_settings(REST_FRAMEWORK=STRICT_REST_FRAMEWORK):
            resp = client.get("/v1/days")
        assert resp.status_code == 401

    def test_invalid_jwt_returns_401(self, client):
        with override_settings(REST_FRAMEWORK=STRICT_REST_FRAMEWORK):
            resp = client.get("/v1/days", HTTP_CF_ACCESS_JWT_ASSERTION="not-a-jwt")
        assert resp.status_code == 401

    def test_expired_jwt_returns_401(self, client, make_jwt):
        token = make_jwt(expires_in_seconds=-10)
        with override_settings(REST_FRAMEWORK=STRICT_REST_FRAMEWORK):
            resp = client.get("/v1/days", HTTP_CF_ACCESS_JWT_ASSERTION=token)
        assert resp.status_code == 401

    def test_service_token_principal_is_namespaced(self, client, make_jwt):
        token = make_jwt(email=None, common_name="ci-runner")
        with override_settings(REST_FRAMEWORK=STRICT_REST_FRAMEWORK):
            resp = client.get("/v1/days", HTTP_CF_ACCESS_JWT_ASSERTION=token)
        assert resp.status_code == 200
        assert User.objects.filter(username="cf-svc:ci-runner").exists()

    def test_repeated_auth_does_not_duplicate_user(self, client, make_jwt):
        token = make_jwt(email="bob@example.com")
        with override_settings(REST_FRAMEWORK=STRICT_REST_FRAMEWORK):
            for _ in range(3):
                assert client.get("/v1/days", HTTP_CF_ACCESS_JWT_ASSERTION=token).status_code == 200
        assert User.objects.filter(username="bob@example.com").count() == 1

    def test_email_normalized_lowercase(self, client, make_jwt):
        token = make_jwt(email="Carol@Example.com")
        with override_settings(REST_FRAMEWORK=STRICT_REST_FRAMEWORK):
            resp = client.get("/v1/days", HTTP_CF_ACCESS_JWT_ASSERTION=token)
        assert resp.status_code == 200
        assert User.objects.filter(username="carol@example.com").exists()

    def test_jwt_in_cookie_fallback_authenticates(self, client, make_jwt):
        token = make_jwt(email="cookie@example.com")
        client.cookies[conf.JWT_COOKIE] = token
        with override_settings(REST_FRAMEWORK=STRICT_REST_FRAMEWORK):
            resp = client.get("/v1/days")
        assert resp.status_code == 200


@pytest.mark.django_db
class TestOptionalMode:
    """When CF_ACCESS_REQUIRED=false, missing JWT yields None (not 401).

    Tested directly against the auth class to bypass settings interaction —
    the DRF integration is already covered by the strict-mode tests.
    """

    def test_missing_jwt_returns_none_when_optional(self, rf, monkeypatch):
        monkeypatch.setattr(conf, "CF_ACCESS_REQUIRED", False)
        request = rf.get("/v1/days")
        result = CloudflareAccessAuthentication().authenticate(request)
        assert result is None

    def test_missing_jwt_raises_when_required(self, rf):
        # conftest autouse already sets REQUIRED=True
        from rest_framework.exceptions import NotAuthenticated

        request = rf.get("/v1/days")
        with pytest.raises(NotAuthenticated):
            CloudflareAccessAuthentication().authenticate(request)


@pytest.mark.django_db
class TestPrincipalResolution:
    """Direct authenticate() call; bypasses DRF view machinery."""

    def test_no_email_no_common_name_raises(self, rf, make_jwt):
        token = make_jwt(email=None, common_name=None)
        request = rf.get("/v1/days", HTTP_CF_ACCESS_JWT_ASSERTION=token)
        from rest_framework.exceptions import AuthenticationFailed

        with pytest.raises(AuthenticationFailed):
            CloudflareAccessAuthentication().authenticate(request)
