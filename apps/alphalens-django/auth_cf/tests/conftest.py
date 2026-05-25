"""Shared fixtures for auth_cf tests.

Generates a single RSA keypair per test session, exposes it as both a JWKS
dict (for injection into the Django cache) and a signing key (for fabricating
test JWTs). ``conf`` module constants are monkey-patched to point at a fake
Cloudflare team / AUD so the verifier's iss/aud checks have something to
match against.
"""

from __future__ import annotations

import datetime as dt
import json
from base64 import urlsafe_b64encode

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.cache import cache

from auth_cf import conf

TEST_TEAM = "alphalens-test"
TEST_AUD = "test-aud-1234"
TEST_KID = "test-kid"


def _int_to_b64url(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.fixture(scope="session")
def rsa_keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private


@pytest.fixture(scope="session")
def jwks(rsa_keypair) -> dict:
    public_numbers = rsa_keypair.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": TEST_KID,
                "use": "sig",
                "alg": "RS256",
                "n": _int_to_b64url(public_numbers.n),
                "e": _int_to_b64url(public_numbers.e),
            }
        ]
    }


@pytest.fixture(autouse=True)
def _patch_cf_conf(monkeypatch, jwks):
    """Pin team/AUD to known test values and pre-populate the JWKS cache.

    Also force a clean DRF ``api_settings`` cache on test teardown — when
    a test runs ``override_settings(REST_FRAMEWORK=...)`` to flip into
    strict auth, the ``setting_changed`` signal that DRF listens for has
    inconsistent ordering with our autouse cleanup, so we re-trigger the
    reload by hand after the test exits.
    """
    from django.test.signals import setting_changed
    from django.conf import settings as dj_settings

    monkeypatch.setattr(conf, "CF_ACCESS_TEAM", TEST_TEAM)
    monkeypatch.setattr(conf, "CF_ACCESS_AUD", TEST_AUD)
    monkeypatch.setattr(conf, "CF_ACCESS_REQUIRED", True)
    cache.clear()
    cache.set(conf.JWKS_CACHE_KEY, jwks)
    yield
    cache.clear()
    # Force DRF to drop any cached REST_FRAMEWORK-derived attrs by replaying
    # the signal with the current (post-override) settings value.
    setting_changed.send(
        sender=dj_settings._wrapped.__class__,
        setting="REST_FRAMEWORK",
        value=getattr(dj_settings, "REST_FRAMEWORK", None),
        enter=False,
    )


@pytest.fixture
def make_jwt(rsa_keypair):
    """Factory: build a CF-shaped JWT with optional claim overrides."""

    def _make(
        *,
        email: str | None = "user@example.com",
        common_name: str | None = None,
        expires_in_seconds: int = 600,
        audience: str = TEST_AUD,
        issuer: str | None = None,
        kid: str = TEST_KID,
    ) -> str:
        now = dt.datetime.now(dt.UTC)
        claims: dict = {
            "iss": issuer if issuer is not None else f"https://{TEST_TEAM}.cloudflareaccess.com",
            "aud": audience,
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(seconds=expires_in_seconds)).timestamp()),
        }
        if email is not None:
            claims["email"] = email
        if common_name is not None:
            claims["common_name"] = common_name
        return jwt.encode(claims, rsa_keypair, algorithm="RS256", headers={"kid": kid})

    return _make


@pytest.fixture
def jwks_json(jwks) -> str:
    """JWKS as a JSON string — useful when stubbing HTTP responses."""
    return json.dumps(jwks)
