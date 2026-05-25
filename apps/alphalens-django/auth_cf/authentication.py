"""DRF authentication class for Cloudflare Access JWT.

Reads the ``Cf-Access-Jwt-Assertion`` header (or ``CF_Authorization`` cookie
fallback), verifies the assertion against the cached JWKS, and resolves the
claims to a Django ``User`` record. Users are auto-provisioned on first sight
because Cloudflare Access has already vouched for the identity — there is no
separate signup flow.

Two principal types:

* **Human identity** — claims carry ``email``. We look up / create a User
  whose ``username`` and ``email`` are that address.
* **Service token** — claims carry ``common_name`` instead of ``email``.
  Username is the ``cf-svc:<common_name>`` namespace to avoid collisions.

When ``CF_ACCESS_REQUIRED=false`` (dev default), a missing/empty JWT header
returns ``None`` — DRF falls through to the next authenticator. When
required (prod), the same case raises 401.
"""

from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth import get_user_model
from rest_framework import authentication, exceptions

from auth_cf import conf
from auth_cf.jwt_verifier import JWTError, verify

logger = logging.getLogger(__name__)
User = get_user_model()


class CloudflareAccessAuthentication(authentication.BaseAuthentication):
    """Verify CF Access JWT and resolve the principal."""

    def authenticate(self, request) -> tuple[Any, dict[str, Any]] | None:
        token = self._extract_token(request)
        if token is None:
            if conf.CF_ACCESS_REQUIRED:
                raise exceptions.NotAuthenticated("missing Cf-Access-Jwt-Assertion")
            return None

        try:
            claims = verify(token)
        except JWTError as exc:
            # Log the underlying jose/JWTError detail for operators (kid
            # mismatch, signature fail, expired, etc.) but return a
            # generic message to the client — JWTError text can carry
            # library-internal hints (CodeQL py/stack-trace-exposure).
            logger.warning("auth_cf: JWT verification failed: %s", exc)
            raise exceptions.AuthenticationFailed("invalid Cf-Access JWT") from exc

        user = self._principal_from_claims(claims)
        return user, claims

    def authenticate_header(self, request) -> str:
        return 'Bearer realm="cloudflare-access"'

    @staticmethod
    def _extract_token(request) -> str | None:
        header = request.META.get(conf.JWT_HEADER, "").strip()
        if header:
            return header
        # Browser-session fallback — Cloudflare also drops the JWT into a cookie.
        cookie = request.COOKIES.get(conf.JWT_COOKIE, "").strip()
        return cookie or None

    @staticmethod
    def _principal_from_claims(claims: dict[str, Any]):
        email = (claims.get("email") or "").strip().lower()
        common_name = (claims.get("common_name") or "").strip()

        if email:
            user, created = User.objects.get_or_create(
                username=email,
                defaults={"email": email, "is_active": True},
            )
            if created:
                logger.info("auth_cf: provisioned new user %s", email)
            return user

        if common_name:
            username = f"cf-svc:{common_name}"
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"is_active": True},
            )
            if created:
                logger.info("auth_cf: provisioned new service principal %s", username)
            return user

        raise exceptions.AuthenticationFailed("JWT has neither email nor common_name claim")
