from django.apps import AppConfig


class AuthCfConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "auth_cf"

    def ready(self) -> None:
        # Register the drf-spectacular extension so it can describe the
        # CloudflareAccessAuthentication class in the generated OpenAPI
        # schema. Import is local so non-API code paths don't pay the
        # spectacular import cost at startup.
        from auth_cf import openapi  # noqa: F401
