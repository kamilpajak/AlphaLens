"""Cloudflare Access auth — env-driven configuration.

Reads three env vars at startup; tests inject values through
``django.test.override_settings`` indirectly by patching this module's
constants. Production env supplies the real values.

Variables:

* ``CF_ACCESS_TEAM`` — Cloudflare team subdomain (``acme`` → ``acme.cloudflareaccess.com``).
* ``CF_ACCESS_AUD`` — application AUD tag, copied from the CF Access app config.
* ``CF_ACCESS_JWKS_CACHE_TTL`` — JWKS cache TTL in seconds. Default 3600.
* ``CF_ACCESS_REQUIRED`` — when truthy, missing/invalid JWT returns 401. When
  falsy (dev default), missing JWT just skips the auth class — useful for
  local API exploration without a Cloudflare tunnel.
"""

from __future__ import annotations

import os

CF_ACCESS_TEAM = os.environ.get("CF_ACCESS_TEAM", "").strip()
CF_ACCESS_AUD = os.environ.get("CF_ACCESS_AUD", "").strip()
CF_ACCESS_JWKS_CACHE_TTL = int(os.environ.get("CF_ACCESS_JWKS_CACHE_TTL", "3600"))
CF_ACCESS_REQUIRED = os.environ.get("CF_ACCESS_REQUIRED", "false").lower() in {"1", "true", "yes"}

JWT_HEADER = "HTTP_CF_ACCESS_JWT_ASSERTION"  # ``Cf-Access-Jwt-Assertion`` request header
JWT_COOKIE = "CF_Authorization"  # fallback when called from a browser session

JWKS_CACHE_KEY = "auth_cf:jwks:v1"

ALGORITHMS = ["RS256"]


def issuer_url() -> str:
    """Construct the JWT ``iss`` claim from ``CF_ACCESS_TEAM``."""
    if not CF_ACCESS_TEAM:
        raise RuntimeError("CF_ACCESS_TEAM is not configured")
    return f"https://{CF_ACCESS_TEAM}.cloudflareaccess.com"


def jwks_url() -> str:
    return f"{issuer_url()}/cdn-cgi/access/certs"
