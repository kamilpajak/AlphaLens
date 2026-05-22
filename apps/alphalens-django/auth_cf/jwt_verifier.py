"""JWT verification against Cloudflare Access JWKS.

Cloudflare Access publishes the JWKS at
``https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`` and signs every
assertion with RS256. We fetch the JWKS on first use, cache it via the Django
cache framework (default TTL 1h), and use ``PyJWKClient`` semantics on top of
the cached blob so subsequent verifies don't go to the network.

The verifier is intentionally narrow: ``verify(token) → claims``. The DRF
authentication class consumes it and decides what to do with the claims.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import jwt
from django.core.cache import cache
from jwt import PyJWKSet
from jwt.exceptions import InvalidTokenError, PyJWTError

from auth_cf import conf

logger = logging.getLogger(__name__)


class JWKSFetchError(RuntimeError):
    """Raised when JWKS retrieval fails."""


def _fetch_jwks() -> dict[str, Any]:
    """One-shot JWKS HTTP fetch. Raises on network/HTTP error."""
    url = conf.jwks_url()
    logger.info("auth_cf: fetching JWKS from %s", url)
    try:
        response = httpx.get(url, timeout=10.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise JWKSFetchError(f"JWKS fetch failed: {exc}") from exc
    return response.json()


def get_jwks(*, refresh: bool = False) -> dict[str, Any]:
    """Cached JWKS lookup. ``refresh=True`` bypasses the cache for one call."""
    if not refresh:
        cached = cache.get(conf.JWKS_CACHE_KEY)
        if cached is not None:
            return cached
    jwks = _fetch_jwks()
    cache.set(conf.JWKS_CACHE_KEY, jwks, timeout=conf.CF_ACCESS_JWKS_CACHE_TTL)
    return jwks


def _signing_key_for_kid(jwks: dict[str, Any], kid: str):
    """Return the cryptography public key matching ``kid``."""
    keyset = PyJWKSet.from_dict(jwks)
    for key in keyset.keys:
        if key.key_id == kid:
            return key.key
    raise InvalidTokenError(f"no signing key for kid={kid!r} in JWKS")


def verify(token: str) -> dict[str, Any]:
    """Decode + verify a CF Access JWT. Returns the claims on success.

    Raises :class:`jwt.PyJWTError` (or a subclass) on any failure mode:
    expired signature, wrong audience, wrong issuer, missing kid, bad
    signature, etc. Callers map that to HTTP 401.

    On a kid-not-found error, refreshes the JWKS once and retries — covers
    the case where Cloudflare rotated keys between cache fetches.
    """
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if not kid:
        raise InvalidTokenError("JWT header missing kid")

    try:
        signing_key = _signing_key_for_kid(get_jwks(), kid)
    except InvalidTokenError:
        signing_key = _signing_key_for_kid(get_jwks(refresh=True), kid)

    return jwt.decode(
        token,
        signing_key,
        algorithms=conf.ALGORITHMS,
        audience=conf.CF_ACCESS_AUD,
        issuer=conf.issuer_url(),
        options={"require": ["exp", "iat", "iss", "aud"]},
    )


# Re-export the canonical exception so callers don't need to import jwt directly.
JWTError = PyJWTError
