"""drf-spectacular extension: describe Cloudflare Access JWT in OpenAPI.

Without this, drf-spectacular emits 12 warnings ("could not resolve
authenticator <CloudflareAccessAuthentication>...") on every schema
generation. The extension teaches it how to render the security scheme.

We describe the scheme as an API-key header (Cf-Access-Jwt-Assertion)
rather than ``http-bearer`` because the token never carries the ``Bearer``
prefix at the wire level — Cloudflare injects it verbatim.
"""

from __future__ import annotations

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class CloudflareAccessScheme(OpenApiAuthenticationExtension):
    target_class = "auth_cf.authentication.CloudflareAccessAuthentication"
    name = "CloudflareAccessJWT"

    def get_security_definition(self, auto_schema) -> dict:
        return {
            "type": "apiKey",
            "in": "header",
            "name": "Cf-Access-Jwt-Assertion",
            "description": (
                "Cloudflare Access service token / SSO JWT, injected by the "
                "Cloudflare edge after the user passes the configured "
                "identity provider (Google SSO). Verified server-side "
                "against the team's JWKS."
            ),
        }
