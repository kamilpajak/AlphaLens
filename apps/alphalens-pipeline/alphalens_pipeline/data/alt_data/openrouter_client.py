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
import copy
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Provider routing. OpenRouter load-balances a model id across several
# upstream providers and enables fallbacks BY DEFAULT, so two calls with
# a byte-identical body can be served by different providers, different
# weights snapshots and different quantisations. That makes "same prompt,
# different answer" ambiguous: it looks the same in the logs whether the
# model was non-deterministic or the router simply sent us elsewhere.
#
# These env vars are the operator's pin. All four are OPTIONAL and
# unset means "behave exactly as before" — no ``provider`` block is sent.
# Pinning an order also fails CLOSED (``allow_fallbacks: false``): a
# pinned run that cannot reach its provider must error loudly rather
# than silently answer from a different backend, which is the whole
# point of pinning. Set ALLOW_FALLBACKS=1 to keep the order as a
# preference instead of a requirement.
PROVIDER_ORDER_ENV = "ALPHALENS_OPENROUTER_PROVIDER_ORDER"
PROVIDER_ALLOW_FALLBACKS_ENV = "ALPHALENS_OPENROUTER_ALLOW_FALLBACKS"
PROVIDER_QUANTIZATIONS_ENV = "ALPHALENS_OPENROUTER_QUANTIZATIONS"
PROVIDER_REQUIRE_PARAMETERS_ENV = "ALPHALENS_OPENROUTER_REQUIRE_PARAMETERS"

# ``require_parameters`` keeps routing to providers that DECLARE support for
# every parameter the request actually sends — for us ``response_format:
# json_object``, ``temperature`` and ``max_tokens``. OpenRouter's own default
# is ``false``, i.e. a provider that does not implement ``response_format``
# stays eligible and simply IGNORES the field: the model answers in prose,
# the call site's ``json_repair`` fallback fires, and the row degrades with
# nothing in the journal explaining why. That silent-degradation class is
# exactly what the provider pin exists to remove, so we default the flag ON
# whenever we emit a routing block at all, and require an explicit opt-out.
#
# The default is cheap, not a guess. Live endpoint census (2026-08-05) for our
# parameter set: deepseek-v4-pro 18 eligible endpoints → 17, deepseek-v4-flash
# 21 → 20. Only ``response_format`` excludes anyone; every endpoint declares
# ``temperature`` and ``max_tokens``. That one-endpoint cost is the number
# that applies unconditionally, i.e. whatever else the operator has set.
# Should the fp8 pin documented in ``deploy/systemd/README.md`` be applied,
# the marginal cost falls to zero — every fp8 endpoint in that census already
# declares ``response_format`` — but nothing in this repo sets it, so do not
# read the fp8 case as the operative one.
#
# On a REPLAY path (one provider, ``allow_fallbacks: false``) the flag earns
# more than it costs: a provider that cannot honour the parameters turns into
# a loud non-2xx instead of a measurement quietly served under different
# semantics.
#
# It buys ELIGIBILITY filtering, not enforcement: OpenRouter warns that a
# provider may advertise ``response_format`` and still treat it as a strong
# hint, so call sites keep their own JSON repair.
_REQUIRE_PARAMETERS_DEFAULT = True

_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSEY_ENV_VALUES = frozenset({"0", "false", "no", "off"})

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


def _split_env_list(raw: str | None) -> list[str]:
    """Split a comma-separated env var, dropping blanks.

    Guards the ``""`` case specifically: a naive ``"".split(",")`` yields
    ``[""]``, which would pin every call to a provider named empty
    string and fail the whole run. Blank in, empty list out.
    """
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY_ENV_VALUES


def _env_tristate(name: str) -> bool | None:
    """Read a flag that has to tell "unset" apart from "explicitly off".

    ``_env_flag`` cannot serve a knob whose default is True: there, an
    unrecognised value silently collapsing to False would flip the
    behaviour, and ``=disabled`` would end up meaning ENABLED. So blank
    stays ``None`` (caller applies its own default) and garbage raises
    rather than picking one side of a knob the operator clearly meant to
    set.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return None
    if raw in _TRUTHY_ENV_VALUES:
        return True
    if raw in _FALSEY_ENV_VALUES:
        return False
    accepted = ", ".join(sorted(_TRUTHY_ENV_VALUES | _FALSEY_ENV_VALUES))
    raise ValueError(f"{name}={raw!r} is not a boolean. Use one of: {accepted} (or leave unset).")


def provider_routing_from_env() -> dict[str, Any] | None:
    """Build the OpenRouter ``provider`` routing block from the environment.

    Returns ``None`` when nothing is configured, so the request body is
    byte-identical to the pre-pinning shape. See the env-var constants
    above for why this exists.
    """
    order = _split_env_list(os.environ.get(PROVIDER_ORDER_ENV))
    quantizations = _split_env_list(os.environ.get(PROVIDER_QUANTIZATIONS_ENV))
    require_parameters = _env_tristate(PROVIDER_REQUIRE_PARAMETERS_ENV)
    # ``require_parameters`` is normally a modifier on a block the other
    # knobs trigger, but switching it ON alone is a meaningful pin of its
    # own ("only providers that honour my parameters"), and silently
    # dropping an env var the operator explicitly set is worse than either
    # answer. Switching it OFF alone stays a no-op: opting out of a
    # restriction is no reason to start sending a ``provider`` block.
    if not order and not quantizations and require_parameters is not True:
        return None
    routing: dict[str, Any] = {}
    if order:
        routing["order"] = order
        # Only meaningful alongside an order — with no order pinned there
        # is nothing to fail closed against, so the switch stays absent.
        routing["allow_fallbacks"] = _env_flag(PROVIDER_ALLOW_FALLBACKS_ENV)
    if quantizations:
        routing["quantizations"] = quantizations
    if require_parameters is None:
        require_parameters = _REQUIRE_PARAMETERS_DEFAULT
    if require_parameters:
        # Omitted rather than sent as ``false`` when disabled: ``false`` is
        # OpenRouter's own default, so the two are identical on the wire and
        # the shorter body keeps "what did we actually pin" readable in a
        # captured request.
        routing["require_parameters"] = True
    return routing


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
    # ``usage`` (prompt_tokens / completion_tokens / total_tokens) is surfaced
    # as a first-class attribute so the brief truncation-retry ladder can log the
    # real completion_tokens and a live probe can size the token cap from data
    # rather than a guess. ``None`` when absent (older shape / empty-choices).
    usage = payload.get("usage")
    # Routing telemetry, promoted out of ``_raw`` to first-class fields.
    # ``provider`` is the upstream that actually served this call and is
    # the fact that separates "the model was non-deterministic" from
    # "the router sent us to a different backend". ``served_model`` is
    # what OpenRouter says it ran, which can differ from the requested id
    # after a fallback. All three are ``None`` when absent rather than
    # raising — telemetry must never break a working call.
    provider = payload.get("provider")
    if provider is not None and not isinstance(provider, str):
        # Documented as a string upstream. If that ever changes, suppress it
        # here rather than leaking a dict to every consumer that annotates
        # the field ``str | None`` — one warning beats N confusing TypeErrors.
        logger.warning("OpenRouter returned a non-string provider %r; suppressing", provider)
        provider = None
    generation_id = payload.get("id")
    served_model = payload.get("model")
    choices = payload.get("choices") or []
    if not choices:
        empty_candidate = SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))
        return SimpleNamespace(
            text="",
            candidates=[empty_candidate],
            usage=usage,
            provider=provider,
            generation_id=generation_id,
            served_model=served_model,
            _raw=payload,
        )
    choice = choices[0]
    content = choice.get("message", {}).get("content", "") or ""
    raw_reason = choice.get("finish_reason")
    # ``.get(..., _UNKNOWN_FINISH_REASON)`` — unknown OpenRouter values
    # land on ``"UNKNOWN"`` rather than silently degrading to ``"STOP"``.
    gemini_name = _FINISH_REASON_MAP.get(raw_reason, _UNKNOWN_FINISH_REASON)
    candidate = SimpleNamespace(finish_reason=SimpleNamespace(name=gemini_name))
    return SimpleNamespace(
        text=content,
        candidates=[candidate],
        usage=usage,
        provider=provider,
        generation_id=generation_id,
        served_model=served_model,
        _raw=payload,
    )


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
        provider_routing: dict[str, Any] | None = None,
    ):
        if not api_key:
            raise ValueError(f"OpenRouter requires a non-empty API key (env {API_KEY_ENV})")
        self._api_key = api_key
        self._base_url = base_url
        # Literal value, NOT read from the environment here: a constructor
        # that silently picked up env vars would make a test's outcome
        # depend on the developer's shell. ``from_env`` does the reading,
        # which is the same split already used for the API key.
        #
        # Deep-copied because the block goes on the wire on every call: a
        # caller keeping a reference could otherwise re-route live traffic
        # long after construction. A shallow copy would not do — ``order``
        # and ``quantizations`` are lists.
        self._provider_routing = copy.deepcopy(provider_routing)
        # Last provider seen per requested model, so a routing change gets
        # one log line instead of one per call. Lock-guarded because the
        # default client is a process-wide singleton and several research
        # scripts drive it from a thread pool: unsynchronised, two threads
        # seeing alternating providers ping-pong the stored value and log a
        # spurious change on every call. Nothing but log volume is at risk —
        # requests and responses never touch this dict.
        self._serving_provider_by_model: dict[str, str] = {}
        self._serving_provider_lock = threading.Lock()
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
        return cls(api_key=api_key, provider_routing=provider_routing_from_env())

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
        if self._provider_routing is not None:
            if "provider" in body:
                # ``build_config(**extra)`` is a documented forward-compat
                # channel, so it can carry ``provider``. Two disagreeing
                # sources of routing must fail loudly: silently preferring
                # one of them produces a run whose actual routing does not
                # match either the env or the call site.
                raise ValueError(
                    "provider routing set twice: the environment pins "
                    f"{PROVIDER_ORDER_ENV} and the call passed provider=... to "
                    "build_config. Unset one of them."
                )
            body["provider"] = copy.deepcopy(self._provider_routing)

        response = self._http.post("/chat/completions", json=body)
        response.raise_for_status()
        payload = response.json()
        wrapped = _wrap_response(payload)
        self._log_serving_provider(model, wrapped.provider)
        return wrapped

    def _log_serving_provider(self, model: str, provider: str | None) -> None:
        """Log the first sighting of a serving provider, and every change.

        Deliberately NOT one line per call: extraction alone makes
        hundreds of calls per run and would drown the journal. Per model,
        because Flash and Pro route independently — a Pro sighting must
        not mask a Flash switch.
        """
        if provider is None:
            return
        with self._serving_provider_lock:
            previous = self._serving_provider_by_model.get(model)
            if previous == provider:
                return
            self._serving_provider_by_model[model] = provider
        # Logging happens outside the lock — no I/O while holding it.
        if previous is None:
            logger.info(
                "OpenRouter: %s served by provider %r (first call this process)", model, provider
            )
        else:
            logger.info(
                "OpenRouter: %s provider CHANGED %r -> %r — the two answers came from "
                "different backends, so a differing result is not necessarily model non-determinism",
                model,
                previous,
                provider,
            )

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
# Guards first-call construction. Without it, concurrent first callers
# all see ``_DEFAULT_CLIENT is None``, each build a client + httpx pool,
# and each call ``atexit.register`` — leaking pools and registering
# duplicate close hooks. Double-checked locking collapses the race to a
# single build (same idiom as ``paper.calendar._calendar``).
_DEFAULT_CLIENT_LOCK = threading.Lock()


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

    Construction is thread-safe via double-checked locking: the fast
    path skips the lock once the singleton exists; the first concurrent
    callers serialise on ``_DEFAULT_CLIENT_LOCK`` and the inner re-check
    guarantees exactly one build + one ``atexit.register``.
    """
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    if _DEFAULT_CLIENT is None:
        with _DEFAULT_CLIENT_LOCK:
            if _DEFAULT_CLIENT is None:
                client = OpenRouterClient.from_env()
                atexit.register(client._http.close)
                _DEFAULT_CLIENT = client
    return _DEFAULT_CLIENT


def _reset_default_client_for_tests() -> None:
    """Test-only hook: clear the cached singleton so each test starts clean."""
    global _DEFAULT_CLIENT  # noqa: PLW0603 — lazy singleton is the documented pattern
    _DEFAULT_CLIENT = None
