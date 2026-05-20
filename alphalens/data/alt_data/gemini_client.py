"""Canonical Gemini (google-genai) client wrapper.

Single source of truth for every Gemini call in the project. Live sites
that route through this client today: thematic extraction (Flash), theme
→ beneficiary mapping (Pro), brief generation (Pro/Flash routed), and
the backtest LLM scorers.

What this client centralises:
- The ``from google import genai`` import boundary. The SDK is loaded
  once per process; the actionable "install google-genai" error message
  lives in one place rather than five.
- API-key resolution from ``GOOGLE_API_KEY`` via :meth:`from_env`.
- Underlying ``genai.Client`` construction. Multiple call sites sharing
  the singleton means one HTTP keepalive pool, one place to inject
  retries or quota tracking later.
- ``types.GenerateContentConfig`` construction via :meth:`build_config`
  so callers don't need to import the SDK's types module just to build
  a config dict.

What this client does NOT do:
- Prompt building / response parsing. Each adapter owns its own schema,
  ``json_repair`` fallback, finish-reason classification, and
  domain-specific error mapping.
- Retry orchestration. Gemini's quota model is high enough that the
  failure-mode each adapter wants (e.g. brief-generator → SAFETY vs
  TRUNCATED retry; backtest scorer → return uncertain LLMVerdict) is
  site-specific.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

API_KEY_ENV = "GOOGLE_API_KEY"

__all__ = [
    "API_KEY_ENV",
    "GeminiClient",
    "get_default_gemini_client",
]


# Module-level lazy SDK handle. Populated on first GeminiClient construction
# via _load_genai_sdk(); cleared between tests by _reset_sdk_cache_for_tests.
_GENAI_MODULE: Any | None = None
_GENAI_TYPES_MODULE: Any | None = None


def _load_genai_sdk() -> tuple[Any, Any]:
    """Import the google-genai SDK lazily; cache after first success.

    Raises ``RuntimeError`` with an actionable message if the SDK is not
    installed. The message and import path live HERE so the five live
    sites that used to each duplicate this block share one error path.
    """
    global _GENAI_MODULE, _GENAI_TYPES_MODULE  # noqa: PLW0603 — documented lazy cache
    if _GENAI_MODULE is not None and _GENAI_TYPES_MODULE is not None:
        return _GENAI_MODULE, _GENAI_TYPES_MODULE
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("google-genai SDK not installed. `uv add google-genai`.") from exc
    _GENAI_MODULE = genai
    _GENAI_TYPES_MODULE = types
    return genai, types


class GeminiClient:
    """Thin wrapper around ``google.genai.Client``.

    State: API key + the underlying SDK client. No throttle / retry — those
    are caller concerns when Gemini's per-key quota actually starts biting
    (today: high enough that the call sites don't need it).
    """

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError(f"Gemini requires a non-empty API key (env {API_KEY_ENV})")
        self._api_key = api_key
        genai, types_mod = _load_genai_sdk()
        self._types = types_mod
        # Build the underlying SDK client up-front so a missing key is
        # surfaced at construction, not at first call.
        self._sdk_client = genai.Client(api_key=api_key)

    @classmethod
    def from_env(cls) -> GeminiClient:
        """Build a client reading the API key from ``GOOGLE_API_KEY``."""
        api_key = os.environ.get(API_KEY_ENV)
        if not api_key:
            raise ValueError(f"{API_KEY_ENV} environment variable is not set.")
        return cls(api_key=api_key)

    @property
    def sdk_client(self) -> Any:
        """Underlying ``genai.Client`` instance. Escape hatch for adapters
        that need to call SDK features beyond ``generate_content``."""
        return self._sdk_client

    @property
    def types(self) -> Any:
        """The ``google.genai.types`` module. Escape hatch for adapters
        building configs unsupported by :meth:`build_config`."""
        return self._types

    def generate_content(
        self,
        *,
        model: str,
        contents: Any,
        config: Any | None = None,
    ) -> Any:
        """Call ``sdk_client.models.generate_content`` and return the
        response object unchanged. Adapters parse it themselves."""
        return self._sdk_client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )

    def build_config(self, **kwargs: Any) -> Any:
        """Construct a ``types.GenerateContentConfig`` with the given kwargs.

        Saves call sites from importing the SDK's types module just to
        build a config (the common case for JSON-schema responses,
        temperature, max_output_tokens).
        """
        return self._types.GenerateContentConfig(**kwargs)


# Module-level lazy singleton — one GeminiClient shared by every adapter
# that doesn't have its own injected client. First call reads
# GOOGLE_API_KEY from the environment; tests reset via
# _reset_default_client_for_tests().
_DEFAULT_CLIENT: GeminiClient | None = None


def get_default_gemini_client() -> GeminiClient:
    """Return the process-wide default GeminiClient (lazy-initialized).

    Raises ``ValueError`` if ``GOOGLE_API_KEY`` is unset at first call.
    Subsequent calls return the same instance; the underlying SDK client
    (HTTP keepalive pool, future quota tracker) is shared across all
    adapters in the process.
    """
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = GeminiClient.from_env()
    return _DEFAULT_CLIENT


def _reset_default_client_for_tests() -> None:
    """Test-only hook: clear the cached singleton so each test starts clean."""
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    _DEFAULT_CLIENT = None


def _reset_sdk_cache_for_tests() -> None:
    """Test-only hook: clear the cached SDK modules so the next client
    construction re-imports (e.g. after sys.modules manipulation)."""
    global _GENAI_MODULE, _GENAI_TYPES_MODULE  # noqa: PLW0603 — documented lazy cache
    _GENAI_MODULE = None
    _GENAI_TYPES_MODULE = None
