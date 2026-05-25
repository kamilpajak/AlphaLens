"""Django middleware that mirrors the DRF auth class for non-DRF views.

DRF's authentication class only runs for views routed through DRF (the
``/v1/*`` API). The Django admin and ``/healthz`` / ``/readyz`` are plain
Django views, so they need a middleware path to populate ``request.user``
from the CF Access JWT.

The middleware is *non-fatal*: if the header is missing or invalid, it
quietly leaves ``request.user`` as ``AnonymousUser``. Whether that
anonymous user gets through is up to the view's own decorator
(``staff_member_required`` for admin, ``AllowAny`` for healthz).
"""

from __future__ import annotations

import logging
from typing import Callable

from django.contrib.auth import get_user_model
from django.http import HttpRequest, HttpResponse

from auth_cf import conf
from auth_cf.jwt_verifier import JWTError, verify

logger = logging.getLogger(__name__)
User = get_user_model()


class CloudflareAccessMiddleware:
    """Verify CF Access JWT and attach the user to non-DRF requests."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        token = (
            request.META.get(conf.JWT_HEADER, "").strip()
            or request.COOKIES.get(conf.JWT_COOKIE, "").strip()
        )

        if token:
            try:
                claims = verify(token)
            except JWTError as exc:
                logger.warning("auth_cf middleware: invalid JWT: %s", exc)
            else:
                user = self._resolve_user(claims)
                if user is not None:
                    request.user = user

        return self.get_response(request)

    @staticmethod
    def _resolve_user(claims: dict):
        email = (claims.get("email") or "").strip().lower()
        if email:
            user, _ = User.objects.get_or_create(
                username=email,
                defaults={"email": email, "is_active": True},
            )
            return user
        common_name = (claims.get("common_name") or "").strip()
        if common_name:
            user, _ = User.objects.get_or_create(
                username=f"cf-svc:{common_name}",
                defaults={"is_active": True},
            )
            return user
        return None
