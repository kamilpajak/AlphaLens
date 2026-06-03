"""Canonical OpenRouter (DeepSeek v4) client wrapper.

Single source of truth for every OpenRouter call in the project.
Currently routes DeepSeek v4 Flash + Pro for the thematic pipeline
(PR-G replaced the earlier Gemini Flash/Pro backend across extract,
mapper, brief). The class exposes a minimal ``generate_content(model=,
contents=)`` surface so adapters at call sites stay backend-agnostic
and a future model swap is a one-line import + model-name change.

What this client centralises:

* The Bearer-auth boundary. ``OPENROUTER_API_KEY`` lives in one place;
  the actionable "set OPENROUTER_API_KEY" error message likewise.
* The OpenAI-compatible /v1/chat/completions request shape. Adapters
  pass a single ``contents`` string and the wrapper
  builds the ``messages=[{...}]`` array, including an auto-synthesised
  system message when JSON output is requested.
* The response-shape translation. OpenRouter returns
  ``choices[0].message.content``; this wrapper exposes it as ``.text``
  so existing call sites that read ``response.text`` (Gemini's shape)
  do not branch on the LLM backend.
* The httpx client lifecycle. One ``httpx.Client()`` per wrapper
  instance → shared TCP/TLS keepalive across calls. The lazy default
  singleton means the whole process shares one pool.

What this client does NOT do:

* Retry / backoff. OpenRouter sometimes returns transient 5xx;
  caller adapters classify per their own failure-mode taxonomy
  (the brief generator already retries on TRUNCATED finish_reason;
  extract / mapper degrade per-row to ``None``).
* Prompt building or response parsing. Each adapter owns its own
  schema + ``json_repair`` fallback + finish-reason classification.
* Structured-output schema enforcement. OpenRouter supports both
  ``json_object`` (free-form valid JSON) and ``json_schema`` (strict
  validation), but the latter is provider-routing-dependent on
  DeepSeek and we already JSON-repair at the call sites. JSON mode
  + schema-embedded-in-system-message is the safer baseline; switch
  to ``json_schema`` if a specific call site needs strict validation.

**DeepSeek JSON-mode hard requirements** (per
https://api-docs.deepseek.com/guides/json_mode):
  1. ``response_format = {"type": "json_object"}``
  2. The literal word "json" appears somewhere in the prompt
  3. Reasonable ``max_tokens`` (else the model truncates mid-object)

This wrapper enforces (1) and (2) automatically when the caller
passes Gemini-style ``response_mime_type="application/json"`` +
``response_schema=...`` to :meth:`build_config`. (3) is the caller's
responsibility, same as for the Gemini client.

**Pricing era (2026-05-30 PR-G snapshot)**: DeepSeek v4-pro on
OpenRouter is mid-promo at $0.435/M input + $0.87/M output. The promo
expires **2026-05-31 16:00 UTC**, reverting to $1.74/M + $3.48/M. v4-flash
is $0.10/M + $0.20/M (no promo). See
``docs/research/polygon_quota_6x_per_day_2026_05_30.md`` §Cost for the
full projection at 6× thematic cadence.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Attribution headers — OpenRouter's per-app dashboard groups requests
# by HTTP-Referer + X-Title for cost attribution. Setting both helps
# the operator see "AlphaLens spent $X today" without having to dig
# through individual gen IDs.
_HTTP_REFERER = "https://github.com/kamilpajak/AlphaLens"
_APP_TITLE = "AlphaLens"

# Default HTTP timeouts. read=60s covers DeepSeek v4-pro's worst-case
# 4-5s typical generation time × ~10× safety margin (some prompts
# generate long JSON). connect=10s catches DNS / TLS issues quickly.
_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)

__all__ = [
    "API_KEY_ENV",
    "OPENROUTER_BASE_URL",
    "OpenRouterClient",
    "OpenRouterConfig",
    "get_default_openrouter_client",
]


# OpenRouter (OpenAI-compatible) → Gemini finish_reason mapping. The
# brief generator's ``_classify_finish_reason`` reads
# ``response.candidates[0].finish_reason.name`` and switches on the
# string. To keep that classifier backend-agnostic we synthesise the
# same shape on top of the OpenRouter response.
#
# A missing key in the lookup (e.g. OpenRouter introduces ``"error"``
# or ``"safety"`` later) MUST NOT silently degrade to ``"STOP"`` — that
# would mask a generation failure as a clean success and skip the
# brief-generator retry. Instead we map unknowns to ``"UNKNOWN"`` so
# the classifier's switch defaults to ``None`` and the downstream
# unparseable-JSON path logs at WARNING, surfacing the regression.
# Zen pre-merge review of PR-G pinned this defence.
_FINISH_REASON_MAP = {
    "stop": "STOP",
    "length": "MAX_TOKENS",  # brief retries on this; same retry-on-truncation policy applies
    "content_filter": "SAFETY",
    "tool_calls": "TOOL_CALLS",  # tool-calling not used today but pass through
    "function_call": "TOOL_CALLS",
    None: "STOP",  # absent field → assume clean stop (most lenient)
}
_UNKNOWN_FINISH_REASON = "UNKNOWN"


def _wrap_response(payload: dict[str, Any]) -> SimpleNamespace:
    """Wrap OpenRouter ``/chat/completions`` JSON into a Gemini-shaped
    response object.

    Exposes both surfaces:

    * ``.text`` — ``choices[0].message.content`` (matches Gemini's
      ``response.text`` shortcut used by ``parse_extraction`` callers).
    * ``.candidates[0].finish_reason.name`` — translated OpenRouter
      ``finish_reason`` (e.g. ``"length"`` → ``"MAX_TOKENS"``). The
      brief generator's ``_classify_finish_reason`` reads this and
      switches on the string; the translation keeps that classifier
      unchanged across the LLM-backend swap.
    * ``._raw`` — full upstream payload for debugging / cost
      attribution (the OpenRouter ``usage`` field carries token
      counts + dollar amount).

    DeepSeek v4-pro occasionally returns ``choices=[]`` under JSON
    mode (documented quirk, see
    https://api-docs.deepseek.com/guides/json_mode). Exposing
    ``.text == ""`` lets the adapter's existing
    "unparseable JSON" branch log + skip rather than crash.
    """
    choices = payload.get("choices") or []
    if not choices:
        empty_candidate = SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))
        return SimpleNamespace(text="", candidates=[empty_candidate], _raw=payload)
    choice = choices[0]
    content = choice.get("message", {}).get("content", "") or ""
    raw_reason = choice.get("finish_reason")
    # ``.get(..., _UNKNOWN_FINISH_REASON)`` — unknown OpenRouter values
    # land on ``"UNKNOWN"`` rather than silently degrading to ``"STOP"``.
    gemini_name = _FINISH_REASON_MAP.get(raw_reason, _UNKNOWN_FINISH_REASON)
    candidate = SimpleNamespace(finish_reason=SimpleNamespace(name=gemini_name))
    return SimpleNamespace(text=content, candidates=[candidate], _raw=payload)


@dataclass
class OpenRouterConfig:
    """Translated config — Gemini-style kwargs in, OpenAI-style fields out.

    Built by :meth:`OpenRouterClient.build_config` and consumed by
    :meth:`OpenRouterClient.generate_content`. The split keeps the
    translation in one place so future model migrations (DeepSeek v5,
    Qwen, etc.) only need to update the build_config logic.
    """

    response_format: dict[str, Any] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    system_message: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _build_system_message_for_json_schema(schema: dict[str, Any]) -> str:
    """Synthesise a system message that nudges the LLM toward the
    requested JSON schema.

    Two requirements the message satisfies:

    * Contains the word "json" (DeepSeek JSON-mode hard requirement).
    * Embeds the schema verbatim so the model has a concrete target.

    A more sophisticated implementation could collapse the schema or
    convert it to natural-language prose; for now we ship the raw
    schema. The call sites already JSON-repair the output so minor
    deviations are tolerated.
    """
    schema_json = json.dumps(schema, indent=2, sort_keys=True)
    return (
        "You are a JSON generator. Respond with a single valid JSON object "
        "that conforms exactly to this schema. Do NOT include any prose, "
        "markdown fences, or extra fields. Output only the JSON object.\n\n"
        f"Schema:\n{schema_json}"
    )


class OpenRouterClient:
    """Thin wrapper around OpenRouter's OpenAI-compatible REST API.

    State: API key + a long-lived ``httpx.Client`` (shared TCP pool).
    No throttle / retry — those are caller concerns when DeepSeek's
    per-key quota actually starts biting.

    The ``_transport=`` constructor arg is a test-only seam (leading
    underscore signals "do not pass in production"):
    ``httpx.MockTransport(handler)`` lets tests intercept requests
    without touching the network. Production callers pass nothing
    and get the default httpx transport.
    """

    def __init__(
        self,
        api_key: str,
        *,
        _transport: httpx.BaseTransport | None = None,
        base_url: str = OPENROUTER_BASE_URL,
    ):
        if not api_key:
            raise ValueError(f"OpenRouter requires a non-empty API key (env {API_KEY_ENV})")
        self._api_key = api_key
        self._base_url = base_url
        self._http = httpx.Client(
            base_url=base_url,
            timeout=_DEFAULT_TIMEOUT,
            transport=_transport,
            headers={
                # Auth header lives here so every call inherits it without
                # the caller needing to remember. Bearer NEVER goes in
                # the URL or body (would leak via access logs / `docker
                # inspect` if the body got captured).
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # OpenRouter's attribution headers — see module docstring.
                "HTTP-Referer": _HTTP_REFERER,
                "X-Title": _APP_TITLE,
            },
        )

    @classmethod
    def from_env(cls) -> OpenRouterClient:
        """Build a client reading the API key from ``OPENROUTER_API_KEY``."""
        api_key = os.environ.get(API_KEY_ENV)
        if not api_key:
            raise ValueError(f"{API_KEY_ENV} environment variable is not set.")
        return cls(api_key=api_key)

    def build_config(
        self,
        *,
        response_mime_type: str | None = None,
        response_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        **extra: Any,
    ) -> OpenRouterConfig:
        """Translate generation kwargs into an :class:`OpenRouterConfig`.

        Keeps a stable ``build_config`` surface so call sites stay
        backend-agnostic. Specifically:

        * ``response_mime_type="application/json"`` →
          ``response_format={"type": "json_object"}``
        * ``response_schema=DICT`` → embedded in synthesised system message
        * ``max_output_tokens=N`` → ``max_tokens=N`` (OpenAI convention)
        * ``temperature=T`` → ``temperature=T``

        ``**extra`` accepts other OpenRouter fields (top_p, frequency_penalty,
        etc.) for forward-compat without code change.
        """
        response_format = None
        system_message = None
        if response_mime_type == "application/json":
            response_format = {"type": "json_object"}
            if response_schema is not None:
                system_message = _build_system_message_for_json_schema(response_schema)
        return OpenRouterConfig(
            response_format=response_format,
            temperature=temperature,
            max_tokens=max_output_tokens,
            system_message=system_message,
            extra=extra,
        )

    def generate_content(
        self,
        *,
        model: str,
        contents: str,
        config: OpenRouterConfig | None = None,
    ) -> SimpleNamespace:
        """Call /v1/chat/completions and return a Gemini-shaped response.

        ``.text`` exposes ``choices[0].message.content`` (or ``""`` if
        the response carried no choices — a documented DeepSeek
        JSON-mode quirk). Call-site adapters parse ``.text`` themselves
        and handle empty / malformed JSON via their own ``json_repair``
        fallbacks.

        Raises ``httpx.HTTPStatusError`` on non-2xx — callers decide
        whether to retry (brief generator does on TRUNCATED) or
        degrade (extract / mapper return ``None`` on any exception).
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": self._build_messages(contents, config),
        }
        if config is not None:
            if config.response_format is not None:
                body["response_format"] = config.response_format
            if config.temperature is not None:
                body["temperature"] = config.temperature
            if config.max_tokens is not None:
                body["max_tokens"] = config.max_tokens
            body.update(config.extra)

        response = self._http.post("/chat/completions", json=body)
        response.raise_for_status()
        payload = response.json()
        return _wrap_response(payload)

    @staticmethod
    def _build_messages(contents: str, config: OpenRouterConfig | None) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if config is not None and config.system_message is not None:
            messages.append({"role": "system", "content": config.system_message})
        messages.append({"role": "user", "content": contents})
        return messages


# Module-level lazy singleton — one OpenRouterClient (and one httpx
# connection pool) shared by every adapter that doesn't have its own
# injected client. First call reads OPENROUTER_API_KEY from the
# environment; tests reset via _reset_default_client_for_tests.
_DEFAULT_CLIENT: OpenRouterClient | None = None


def get_default_openrouter_client() -> OpenRouterClient:
    """Return the process-wide default OpenRouterClient.

    Raises ``ValueError`` if ``OPENROUTER_API_KEY`` is unset at first
    call. Subsequent calls return the same instance; the underlying
    httpx connection pool is shared across all adapters in the process.

    On first construction we register an ``atexit`` hook to close the
    ``httpx.Client`` so a long-running daemon does not leak the
    connection pool. Cron-style processes exit immediately and Python's
    GC handles it anyway, but the explicit close is defence-in-depth
    (zen pre-merge review of PR-G flagged the leak surface).
    """
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = OpenRouterClient.from_env()
        atexit.register(_DEFAULT_CLIENT._http.close)
    return _DEFAULT_CLIENT


def _reset_default_client_for_tests() -> None:
    """Test-only hook: clear the cached singleton so each test starts clean."""
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    _DEFAULT_CLIENT = None
