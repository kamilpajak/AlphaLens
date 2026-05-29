"""Tests for the `/auth/start` re-auth trampoline.

The view itself is a thin 302 redirector — the real work is the
Cloudflare Access SSO that fires BEFORE the request lands here. These
tests cover the post-Access portion: the open-redirect guard, the
default-target fallback, and the HTTP method contract.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from django.test import Client
from django.urls import reverse


@pytest.fixture
def client() -> Client:
    return Client()


SPA_ORIGIN = "https://app.alphalens.kamilpajak.pl"
OTHER_ALLOWED_ORIGIN = "https://app-preview.alphalens.kamilpajak.pl"


class TestAuthStartHappyPath:
    @pytest.fixture(autouse=True)
    def _settings(self, settings):
        settings.CORS_ALLOWED_ORIGINS = [SPA_ORIGIN, OTHER_ALLOWED_ORIGIN]

    def test_absolute_allowed_origin_redirects_to_return_to(self, client: Client):
        url = reverse("auth-start") + f"?return_to={SPA_ORIGIN}/brief/2026-05-27"
        response = client.get(url)
        assert response.status_code == 302
        assert response["Location"] == f"{SPA_ORIGIN}/brief/2026-05-27"

    def test_relative_path_is_allowed(self, client: Client):
        # A bare `/brief/...` relative URL is same-origin per Django's
        # url_has_allowed_host_and_scheme. The SPA never links to a
        # bare path here; this branch protects against accidental
        # same-origin redirects (e.g. tests, manual operator hits).
        response = client.get(reverse("auth-start") + "?return_to=/brief/2026-05-27")
        assert response.status_code == 302
        assert response["Location"] == "/brief/2026-05-27"

    def test_return_to_with_query_string_and_fragment_preserved(self, client: Client):
        target = f"{SPA_ORIGIN}/experiments?filter=P14#status"
        response = client.get(reverse("auth-start") + f"?return_to={quote(target)}")
        assert response.status_code == 302
        assert response["Location"] == target

    def test_second_allowed_origin_also_redirects(self, client: Client):
        # CORS_ALLOWED_ORIGINS may carry multiple SPA origins (preview
        # deployments, staging). All of them are valid return targets.
        response = client.get(reverse("auth-start") + f"?return_to={OTHER_ALLOWED_ORIGIN}/")
        assert response.status_code == 302
        assert response["Location"] == f"{OTHER_ALLOWED_ORIGIN}/"


class TestAuthStartOpenRedirectGuard:
    @pytest.fixture(autouse=True)
    def _settings(self, settings):
        settings.CORS_ALLOWED_ORIGINS = [SPA_ORIGIN]

    def test_third_party_origin_rejected(self, client: Client):
        response = client.get(reverse("auth-start") + "?return_to=https://evil.com/")
        assert response.status_code == 400

    def test_userinfo_smuggling_rejected(self, client: Client):
        # Classic open-redirect bypass: `https://<allowed>@evil.com/` — the
        # netloc is `evil.com`, not the allowed host. url_has_allowed_host_and_scheme
        # parses correctly and rejects.
        response = client.get(
            reverse("auth-start") + "?return_to=https://app.alphalens.kamilpajak.pl@evil.com/"
        )
        assert response.status_code == 400

    def test_protocol_relative_url_rejected(self, client: Client):
        # `//evil.com/path` is protocol-relative — the browser would
        # treat it as `https://evil.com/path`. Django's guard rejects.
        response = client.get(reverse("auth-start") + "?return_to=//evil.com/")
        assert response.status_code == 400

    def test_http_target_rejected_when_https_required(self, client: Client):
        # SPA origin in CORS_ALLOWED_ORIGINS is HTTPS; an HTTP variant
        # is a downgrade attack vector.
        response = client.get(
            reverse("auth-start") + "?return_to=http://app.alphalens.kamilpajak.pl/"
        )
        assert response.status_code == 400

    def test_javascript_scheme_rejected(self, client: Client):
        response = client.get(reverse("auth-start") + "?return_to=javascript:alert(1)")
        assert response.status_code == 400

    def test_data_uri_rejected(self, client: Client):
        response = client.get(
            reverse("auth-start") + "?return_to=data:text/html,<script>alert(1)</script>"
        )
        assert response.status_code == 400


class TestAuthStartDefaults:
    @pytest.fixture(autouse=True)
    def _settings(self, settings):
        settings.CORS_ALLOWED_ORIGINS = [SPA_ORIGIN]

    def test_missing_return_to_redirects_to_first_cors_origin(self, client: Client):
        # An operator hitting /auth/start directly (no return_to) should
        # not land on the API JSON root — bounce to the SPA so the UX is
        # consistent.
        response = client.get(reverse("auth-start"))
        assert response.status_code == 302
        assert response["Location"] == SPA_ORIGIN

    def test_empty_return_to_treated_as_missing(self, client: Client):
        response = client.get(reverse("auth-start") + "?return_to=")
        assert response.status_code == 302
        assert response["Location"] == SPA_ORIGIN


class TestAuthStartNoSpaConfigured:
    @pytest.fixture(autouse=True)
    def _settings(self, settings):
        settings.CORS_ALLOWED_ORIGINS = []

    def test_missing_return_to_falls_back_to_root(self, client: Client):
        # Dev / misconfigured deploy: no CORS allowlist means no known
        # SPA origin. Fall back to "/" (the API root) — not ideal UX
        # but safe and matches the documented contract.
        response = client.get(reverse("auth-start"))
        assert response.status_code == 302
        assert response["Location"] == "/"


class TestAuthStartMethodContract:
    @pytest.fixture(autouse=True)
    def _settings(self, settings):
        settings.CORS_ALLOWED_ORIGINS = [SPA_ORIGIN]

    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    def test_non_get_methods_rejected(self, client: Client, method: str):
        response = getattr(client, method)(reverse("auth-start"))
        assert response.status_code == 405
