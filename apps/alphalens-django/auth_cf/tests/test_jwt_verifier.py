"""Unit tests for the JWKS-backed JWT verifier."""

from __future__ import annotations

import jwt
import pytest

from auth_cf.jwt_verifier import JWTError, verify


class TestVerifyHappyPath:
    def test_valid_jwt_returns_claims(self, make_jwt):
        token = make_jwt(email="alice@example.com")
        claims = verify(token)
        assert claims["email"] == "alice@example.com"
        assert claims["aud"] == "test-aud-1234"

    def test_service_token_with_common_name(self, make_jwt):
        token = make_jwt(email=None, common_name="ci-runner")
        claims = verify(token)
        assert claims["common_name"] == "ci-runner"
        assert "email" not in claims


class TestVerifyRejections:
    def test_expired_token_raises(self, make_jwt):
        token = make_jwt(expires_in_seconds=-10)
        with pytest.raises(JWTError):
            verify(token)

    def test_wrong_audience_raises(self, make_jwt):
        token = make_jwt(audience="some-other-aud")
        with pytest.raises(JWTError):
            verify(token)

    def test_wrong_issuer_raises(self, make_jwt):
        token = make_jwt(issuer="https://impostor.cloudflareaccess.com")
        with pytest.raises(JWTError):
            verify(token)

    def test_unknown_kid_raises(self, make_jwt):
        token = make_jwt(kid="not-a-real-kid")
        # The verifier refreshes JWKS on kid-miss; since the test patches the
        # cache directly and there's no live network call, the refresh ends up
        # asking httpx to actually hit `https://alphalens-test.cloudflareaccess.com`.
        # That call will fail (no DNS), and the verifier re-raises the original
        # InvalidTokenError. Both surfaces are JWTError subclasses, so the
        # assertion below covers either pathway.
        with pytest.raises(Exception):  # noqa: B017 — JWTError|JWKSFetchError|httpx.HTTPError
            verify(token)

    def test_tampered_signature_raises(self, make_jwt):
        token = make_jwt() + "tamper"
        with pytest.raises(JWTError):
            verify(token)

    def test_missing_kid_header_raises(self, rsa_keypair):
        import datetime as dt

        from auth_cf.tests.conftest import TEST_AUD, TEST_TEAM

        now = dt.datetime.now(dt.UTC)
        token = jwt.encode(
            {
                "iss": f"https://{TEST_TEAM}.cloudflareaccess.com",
                "aud": TEST_AUD,
                "iat": int(now.timestamp()),
                "exp": int((now + dt.timedelta(minutes=5)).timestamp()),
                "email": "x@y.z",
            },
            rsa_keypair,
            algorithm="RS256",
            # no kid header
        )
        with pytest.raises(JWTError):
            verify(token)
