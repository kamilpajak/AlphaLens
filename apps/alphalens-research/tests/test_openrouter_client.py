"""Canonical OpenRouter client — round-trip + structured-output + auth.

PR-G (epic #295 follow-up): replaced the earlier Gemini client at three
call sites in the thematic pipeline (extract Flash, mapper Pro, brief
generator Pro/Flash). The client keeps a minimal, backend-agnostic public
surface so the swap at call sites was a one-line import + model-name
change, NOT a contract change.

Key contracts pinned here:

* **Stable public surface** — ``from_env()``,
  ``generate_content(*, model, contents, config)``, ``build_config(**kw)``
  — so adapters can stay agnostic to the LLM backend.
* **``response.text`` exposes the completion text** — adapters read
  ``response.text``; OpenRouter returns ``choices[0].message.content``.
  The wrapper exposes it as ``.text`` so call sites don't branch.
* **JSON-mode + schema-in-prompt** — DeepSeek's JSON mode requires
  (a) ``response_format={'type': 'json_object'}`` and (b) the literal
  word "json" in the prompt. The wrapper enforces both when the caller
  passes ``response_mime_type="application/json"`` +
  ``response_schema=...``. Schema is embedded in a synthesised system
  message; output is free-form JSON (we already JSON-repair at call
  sites, so strict ``json_schema`` mode is not required).
* **Bearer auth + dedicated env var** — ``OPENROUTER_API_KEY`` (NOT
  the zen-codereview key — that's a separate OpenRouter account/key
  for billing isolation). Auth never lives in URL or body.
* **Lazy singleton** — one HTTP connection pool per process, parallel
  to the other ``get_default_*_client()`` singletons.

Tests use ``httpx.MockTransport`` to intercept the HTTPS call without
hitting OpenRouter's servers. The transport asserts the request shape
(URL, headers, body) so a future "let's simplify the auth header"
refactor cannot silently drop the Bearer token.
"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from unittest import mock

import httpx
from alphalens_pipeline.data.alt_data import openrouter_client as _orc_module
from alphalens_pipeline.data.alt_data.openrouter_client import (
    API_KEY_ENV,
    OPENROUTER_BASE_URL,
    OpenRouterClient,
    _reset_default_client_for_tests,
    get_default_openrouter_client,
)

_DUMMY_KEY = "sk-or-v1-test-not-real-key"


def _mock_chat_response(content: str) -> dict:
    """Shape of OpenRouter /v1/chat/completions success response."""
    return {
        "id": "gen-test-123",
        "model": "deepseek/deepseek-v4-flash",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


class TestOpenRouterClientConstruction(unittest.TestCase):
    """The client refuses to construct without a key — fail loud, never
    silently degrade to anonymous (which OpenRouter would 401 on)."""

    def test_init_rejects_empty_key(self) -> None:
        with self.assertRaisesRegex(ValueError, API_KEY_ENV):
            OpenRouterClient(api_key="")

    def test_init_rejects_none_key(self) -> None:
        with self.assertRaisesRegex(ValueError, API_KEY_ENV):
            OpenRouterClient(api_key=None)  # type: ignore[arg-type]

    def test_from_env_reads_OPENROUTER_API_KEY(self) -> None:
        with mock.patch.dict(os.environ, {API_KEY_ENV: _DUMMY_KEY}, clear=False):
            client = OpenRouterClient.from_env()
        self.assertIsInstance(client, OpenRouterClient)

    def test_from_env_raises_when_unset(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != API_KEY_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, API_KEY_ENV):
                OpenRouterClient.from_env()


class TestGenerateContentRequestShape(unittest.TestCase):
    """Request shape pinned: Bearer auth, POST /v1/chat/completions,
    JSON body with model + messages + response_format."""

    def _client_with_transport(self, handler) -> OpenRouterClient:
        transport = httpx.MockTransport(handler)
        return OpenRouterClient(api_key=_DUMMY_KEY, _transport=transport)

    def test_post_to_v1_chat_completions(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            return httpx.Response(200, json=_mock_chat_response('{"ok": true}'))

        client = self._client_with_transport(handler)
        client.generate_content(model="deepseek/deepseek-v4-flash", contents="hello")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], f"{OPENROUTER_BASE_URL}/chat/completions")

    def test_bearer_auth_header(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=_mock_chat_response('{"x": 1}'))

        client = self._client_with_transport(handler)
        client.generate_content(model="deepseek/deepseek-v4-flash", contents="hi")
        self.assertEqual(
            captured["auth"],
            f"Bearer {_DUMMY_KEY}",
            "Auth MUST be sent as a Bearer header — never in URL/body. "
            "OpenRouter would 401 on missing/malformed auth.",
        )

    def test_api_key_never_appears_in_url(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json=_mock_chat_response("{}"))

        client = self._client_with_transport(handler)
        client.generate_content(model="deepseek/deepseek-v4-flash", contents="hi")
        self.assertNotIn(_DUMMY_KEY, captured["url"])
        self.assertNotIn("api_key", captured["url"])

    def test_messages_carry_user_prompt(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_mock_chat_response("{}"))

        client = self._client_with_transport(handler)
        client.generate_content(model="deepseek/deepseek-v4-flash", contents="prompt-body")
        msgs = captured["body"]["messages"]
        # User message MUST appear last (DeepSeek processes the latest
        # user turn as the active query).
        user_msgs = [m for m in msgs if m["role"] == "user"]
        self.assertGreaterEqual(len(user_msgs), 1)
        self.assertEqual(user_msgs[-1]["content"], "prompt-body")

    def test_attribution_headers_for_openrouter_dashboard(self) -> None:
        # OpenRouter's per-app dashboard groups requests by HTTP-Referer
        # + X-Title. Sending both helps cost attribution debugging.
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json=_mock_chat_response("{}"))

        client = self._client_with_transport(handler)
        client.generate_content(model="deepseek/deepseek-v4-flash", contents="hi")
        self.assertIn("http-referer", captured["headers"])
        self.assertIn("x-title", captured["headers"])
        self.assertIn("alphalens", captured["headers"]["x-title"].lower())


class TestGenerateContentResponseShape(unittest.TestCase):
    """The wrapper exposes ``.text`` matching Gemini's response shape so
    call sites stay backend-agnostic. ``choices[0].message.content`` is
    the OpenRouter field we lift."""

    def test_text_attribute_returns_message_content(self) -> None:
        payload = '{"event_type": "earnings", "confidence": 0.8}'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_mock_chat_response(payload))

        client = OpenRouterClient(api_key=_DUMMY_KEY, _transport=httpx.MockTransport(handler))
        response = client.generate_content(
            model="deepseek/deepseek-v4-flash", contents="extract this"
        )
        self.assertEqual(
            response.text,
            payload,
            ".text MUST return choices[0].message.content unchanged so "
            "adapters can parse_extraction(response.text).",
        )

    def test_empty_choices_returns_empty_text(self) -> None:
        # DeepSeek occasionally returns empty content under JSON mode
        # (documented quirk). The wrapper MUST return "" rather than
        # raise so the adapter's existing "unparseable JSON" path kicks
        # in and logs at WARNING — no need for the adapter to also
        # guard for an exception class.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "x", "model": "y", "choices": []})

        client = OpenRouterClient(api_key=_DUMMY_KEY, _transport=httpx.MockTransport(handler))
        response = client.generate_content(model="deepseek/deepseek-v4-flash", contents="anything")
        self.assertEqual(response.text, "")

    def test_usage_attribute_exposes_token_counts(self) -> None:
        # The truncation-retry ladder + the live budget probe need to see how
        # many completion tokens a response actually consumed (to size the cap
        # from data, not a guess). _wrap_response dropped usage; expose it.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_mock_chat_response('{"ok": 1}'))

        client = OpenRouterClient(api_key=_DUMMY_KEY, _transport=httpx.MockTransport(handler))
        response = client.generate_content(model="deepseek/deepseek-v4-flash", contents="hi")
        self.assertIsNotNone(response.usage)
        self.assertEqual(response.usage["completion_tokens"], 50)
        self.assertEqual(response.usage["prompt_tokens"], 100)

    def test_usage_is_none_when_absent(self) -> None:
        # A response with no usage block (the empty-choices quirk / an older
        # shape) must expose usage=None, never raise — the caller degrades.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "x", "model": "y", "choices": []})

        client = OpenRouterClient(api_key=_DUMMY_KEY, _transport=httpx.MockTransport(handler))
        response = client.generate_content(model="deepseek/deepseek-v4-flash", contents="hi")
        self.assertIsNone(response.usage)

    def test_5xx_raises_with_status(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="upstream busy")

        client = OpenRouterClient(api_key=_DUMMY_KEY, _transport=httpx.MockTransport(handler))
        with self.assertRaisesRegex(httpx.HTTPStatusError, "503"):
            client.generate_content(model="deepseek/deepseek-v4-flash", contents="hi")


class TestBuildConfigGeminiCompat(unittest.TestCase):
    """``build_config(**kwargs)`` accepts Gemini-style kwargs so the
    three existing call sites can pass exactly what they pass today:

        client.build_config(
            response_mime_type="application/json",
            response_schema=EVENT_RESPONSE_SCHEMA,
            temperature=0.0,
            max_output_tokens=8000,
        )

    The wrapper translates internally:
      * response_mime_type=application/json → response_format={"type":"json_object"}
      * response_schema=DICT → embedded in synthesised system message
      * max_output_tokens → max_tokens (OpenAI convention)
      * temperature → temperature (no change)
    """

    def test_config_carries_json_object_response_format(self) -> None:
        client = OpenRouterClient(api_key=_DUMMY_KEY)
        cfg = client.build_config(
            response_mime_type="application/json",
            response_schema={"type": "object"},
            temperature=0.0,
            max_output_tokens=8000,
        )
        self.assertEqual(cfg.response_format, {"type": "json_object"})

    def test_config_translates_max_output_tokens_to_max_tokens(self) -> None:
        client = OpenRouterClient(api_key=_DUMMY_KEY)
        cfg = client.build_config(max_output_tokens=4096, temperature=0.1)
        self.assertEqual(cfg.max_tokens, 4096)

    def test_config_carries_temperature(self) -> None:
        client = OpenRouterClient(api_key=_DUMMY_KEY)
        cfg = client.build_config(temperature=0.7)
        self.assertEqual(cfg.temperature, 0.7)

    def test_config_embeds_schema_in_system_message(self) -> None:
        schema = {
            "type": "object",
            "properties": {"event_type": {"type": "string", "enum": ["earnings", "m_and_a"]}},
            "required": ["event_type"],
        }
        client = OpenRouterClient(api_key=_DUMMY_KEY)
        cfg = client.build_config(
            response_mime_type="application/json",
            response_schema=schema,
        )
        # The synthesised system message MUST contain the literal word
        # "json" (DeepSeek's JSON-mode hard requirement) AND a
        # serialisation of the schema so the model can self-validate.
        self.assertIn("json", cfg.system_message.lower())
        self.assertIn("event_type", cfg.system_message)
        self.assertIn("earnings", cfg.system_message)


class TestGenerateContentWithConfig(unittest.TestCase):
    """End-to-end: config built via ``build_config`` is honoured in the
    HTTP request. System message synthesised from schema lands in the
    messages list; response_format passes through; temperature +
    max_tokens land in the body."""

    def test_config_response_format_in_body(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_mock_chat_response("{}"))

        client = OpenRouterClient(api_key=_DUMMY_KEY, _transport=httpx.MockTransport(handler))
        cfg = client.build_config(
            response_mime_type="application/json",
            response_schema={"type": "object"},
        )
        client.generate_content(model="deepseek/deepseek-v4-flash", contents="prompt", config=cfg)
        self.assertEqual(captured["body"].get("response_format"), {"type": "json_object"})

    def test_config_temperature_and_max_tokens_in_body(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_mock_chat_response("{}"))

        client = OpenRouterClient(api_key=_DUMMY_KEY, _transport=httpx.MockTransport(handler))
        cfg = client.build_config(temperature=0.0, max_output_tokens=8000)
        client.generate_content(model="deepseek/deepseek-v4-flash", contents="prompt", config=cfg)
        self.assertEqual(captured["body"]["temperature"], 0.0)
        self.assertEqual(captured["body"]["max_tokens"], 8000)

    def test_config_schema_lands_in_system_message(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_mock_chat_response("{}"))

        client = OpenRouterClient(api_key=_DUMMY_KEY, _transport=httpx.MockTransport(handler))
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        cfg = client.build_config(response_mime_type="application/json", response_schema=schema)
        client.generate_content(model="deepseek/deepseek-v4-flash", contents="prompt", config=cfg)
        msgs = captured["body"]["messages"]
        system_msgs = [m for m in msgs if m["role"] == "system"]
        self.assertEqual(len(system_msgs), 1)
        self.assertIn("json", system_msgs[0]["content"].lower())
        # The schema field MUST surface in the system message; users
        # can grep for property names to verify the prompt drift.
        self.assertIn('"x"', system_msgs[0]["content"])


class TestFinishReasonGeminiCompat(unittest.TestCase):
    """The brief generator's ``_classify_finish_reason`` reads
    ``response.candidates[0].finish_reason.name`` and switches on the
    Gemini string enum (``MAX_TOKENS`` / ``SAFETY`` / ``STOP``). The
    wrapper translates OpenRouter's lowercase OpenAI strings so the
    classifier stays unchanged across the LLM-backend swap.
    """

    def _client(self, payload: dict) -> OpenRouterClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        return OpenRouterClient(api_key=_DUMMY_KEY, _transport=httpx.MockTransport(handler))

    def test_openrouter_length_maps_to_gemini_MAX_TOKENS(self) -> None:
        payload = {
            "choices": [
                {"message": {"content": '{"x":1}'}, "finish_reason": "length"},
            ],
        }
        resp = self._client(payload).generate_content(
            model="deepseek/deepseek-v4-pro", contents="hi"
        )
        self.assertEqual(resp.candidates[0].finish_reason.name, "MAX_TOKENS")

    def test_openrouter_stop_maps_to_gemini_STOP(self) -> None:
        payload = {
            "choices": [
                {"message": {"content": '{"x":1}'}, "finish_reason": "stop"},
            ],
        }
        resp = self._client(payload).generate_content(
            model="deepseek/deepseek-v4-pro", contents="hi"
        )
        self.assertEqual(resp.candidates[0].finish_reason.name, "STOP")

    def test_openrouter_content_filter_maps_to_gemini_SAFETY(self) -> None:
        payload = {
            "choices": [
                {"message": {"content": ""}, "finish_reason": "content_filter"},
            ],
        }
        resp = self._client(payload).generate_content(
            model="deepseek/deepseek-v4-pro", contents="hi"
        )
        self.assertEqual(resp.candidates[0].finish_reason.name, "SAFETY")

    def test_unknown_finish_reason_maps_to_UNKNOWN_not_STOP(self) -> None:
        # Zen pre-merge review of PR-G: if OpenRouter introduces a new
        # status (e.g. ``"error"``, ``"safety"``), the wrapper MUST
        # surface it as ``UNKNOWN`` so the brief generator's classifier
        # defaults to None and the downstream unparseable-JSON path
        # logs at WARNING. Silently degrading to ``STOP`` would mask
        # a generation failure as a clean success.
        payload = {
            "choices": [
                {"message": {"content": ""}, "finish_reason": "error"},
            ],
        }
        resp = self._client(payload).generate_content(
            model="deepseek/deepseek-v4-pro", contents="hi"
        )
        self.assertEqual(
            resp.candidates[0].finish_reason.name,
            "UNKNOWN",
            "Unknown OpenRouter finish_reason MUST map to 'UNKNOWN', "
            "not 'STOP' (would silently swallow a generation failure).",
        )

    def test_empty_choices_synthesises_STOP_candidate(self) -> None:
        # The brief generator's classifier reads candidates[0]
        # unconditionally — a missing candidate would IndexError.
        # The wrapper synthesises a fake STOP candidate so the
        # downstream "" content path triggers MALFORMED_JSON
        # classification, not a crash.
        payload = {"choices": []}
        resp = self._client(payload).generate_content(
            model="deepseek/deepseek-v4-pro", contents="hi"
        )
        self.assertEqual(resp.text, "")
        self.assertEqual(len(resp.candidates), 1)
        self.assertEqual(resp.candidates[0].finish_reason.name, "STOP")


class TestLazyDefaultSingleton(unittest.TestCase):
    """``get_default_openrouter_client()`` returns the same instance on
    repeat calls so the HTTP keepalive pool is shared. Test-only
    ``_reset_default_client_for_tests`` clears between tests."""

    def setUp(self) -> None:
        _reset_default_client_for_tests()

    def tearDown(self) -> None:
        _reset_default_client_for_tests()

    def test_singleton_identity(self) -> None:
        with mock.patch.dict(os.environ, {API_KEY_ENV: _DUMMY_KEY}, clear=False):
            a = get_default_openrouter_client()
            b = get_default_openrouter_client()
        self.assertIs(a, b)

    def test_reset_hook_clears_singleton(self) -> None:
        with mock.patch.dict(os.environ, {API_KEY_ENV: _DUMMY_KEY}, clear=False):
            a = get_default_openrouter_client()
            _reset_default_client_for_tests()
            b = get_default_openrouter_client()
        self.assertIsNot(a, b)


class TestDefaultSingletonThreadSafety(unittest.TestCase):
    """Concurrent first-call must build exactly ONE client and register
    the ``atexit`` close hook exactly ONCE.

    Without double-checked locking, N threads racing the first call all
    see ``_DEFAULT_CLIENT is None``, each build a client (and each
    ``httpx`` connection pool), and each call ``atexit.register`` — so
    the process leaks N-1 pools and registers N close hooks. The lock
    serialises construction; the inner re-check inside the lock collapses
    the race to a single build.
    """

    def setUp(self) -> None:
        _reset_default_client_for_tests()

    def tearDown(self) -> None:
        _reset_default_client_for_tests()

    def test_concurrent_first_call_builds_one_instance(self) -> None:
        n_threads = 16
        # All workers rendezvous here, then call the factory together, so
        # every thread races the same first-call ``is None`` check.
        start = threading.Barrier(n_threads)
        register_calls: list[object] = []
        register_lock = threading.Lock()

        original_from_env = OpenRouterClient.from_env

        def slow_from_env() -> OpenRouterClient:
            # Hold the (single) builder inside construction long enough
            # that, without the lock, every other thread would also pass
            # the outer ``is None`` check and build its own client.
            time.sleep(0.05)
            return original_from_env()

        real_register = _orc_module.atexit.register

        def counting_register(func: object, *args: object, **kwargs: object) -> object:
            # The patched ``atexit.register`` is the process-global one, so
            # unrelated libraries (httpx etc.) may register during the test
            # window too. Record every call but assert only on OUR close
            # hook below. Still forward to the real registrar so nothing
            # leaks behaviour.
            with register_lock:
                register_calls.append(func)
            return real_register(func, *args, **kwargs)

        results: list[OpenRouterClient] = []
        errors: list[BaseException] = []
        results_lock = threading.Lock()

        def worker() -> None:
            try:
                start.wait(timeout=5)
                client = get_default_openrouter_client()
            except BaseException as exc:  # surface in the assert, not a hang
                with results_lock:
                    errors.append(exc)
                return
            with results_lock:
                results.append(client)

        with mock.patch.dict(os.environ, {API_KEY_ENV: _DUMMY_KEY}, clear=False):
            with (
                mock.patch.object(OpenRouterClient, "from_env", staticmethod(slow_from_env)),
                mock.patch.object(_orc_module.atexit, "register", counting_register),
            ):
                threads = [threading.Thread(target=worker) for _ in range(n_threads)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), n_threads)
        # Exactly one instance shared by every thread.
        self.assertEqual(len({id(c) for c in results}), 1)
        # Exactly one httpx pool gets an atexit close hook. The factory
        # registers ``client._http.close`` (a bound method). The patched
        # registrar is process-global so it also sees unrelated httpx
        # registrations — filter to our close hooks by name, then count
        # DISTINCT owning pools. Broken (unguarded) builds N clients → N
        # distinct pools registered; fixed builds one.
        close_hook_owners = {
            id(f.__self__)
            for f in register_calls
            if getattr(f, "__name__", None) == "close"
            and isinstance(getattr(f, "__self__", None), httpx.Client)
        }
        self.assertEqual(
            len(close_hook_owners),
            1,
            f"expected exactly one httpx pool registered for atexit close, "
            f"got {len(close_hook_owners)}",
        )


class TestProviderRouting(unittest.TestCase):
    """OpenRouter load-balances across providers and enables fallbacks by
    DEFAULT, so a stable model id does NOT imply the same provider,
    weights or quantisation between two calls. Pinning makes the request
    reproducible; leaving it unset must keep today's behaviour exactly.
    """

    def _capture_body(self, client: OpenRouterClient, captured: dict) -> None:
        client.generate_content(model="deepseek/deepseek-v4-flash", contents="say json")

    def _client_capturing(self, captured: dict, **kwargs) -> OpenRouterClient:
        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=_mock_chat_response("{}"))

        return OpenRouterClient(
            api_key=_DUMMY_KEY, _transport=httpx.MockTransport(handler), **kwargs
        )

    def test_no_provider_block_when_unconfigured(self) -> None:
        captured: dict = {}
        client = self._client_capturing(captured, provider_routing=None)
        self._capture_body(client, captured)
        self.assertNotIn("provider", captured)

    def test_pinned_order_lands_in_body_and_disables_fallbacks(self) -> None:
        captured: dict = {}
        client = self._client_capturing(
            captured, provider_routing={"order": ["DeepInfra"], "allow_fallbacks": False}
        )
        self._capture_body(client, captured)
        self.assertEqual(captured["provider"]["order"], ["DeepInfra"])
        self.assertFalse(captured["provider"]["allow_fallbacks"])

    def test_env_order_pins_and_fails_closed_by_default(self) -> None:
        with mock.patch.dict(
            os.environ, {_orc_module.PROVIDER_ORDER_ENV: "DeepInfra, Together"}, clear=False
        ):
            routing = _orc_module.provider_routing_from_env()
        self.assertEqual(routing, {"order": ["DeepInfra", "Together"], "allow_fallbacks": False})

    def test_env_can_re_enable_fallbacks(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                _orc_module.PROVIDER_ORDER_ENV: "DeepInfra",
                _orc_module.PROVIDER_ALLOW_FALLBACKS_ENV: "1",
            },
            clear=False,
        ):
            routing = _orc_module.provider_routing_from_env()
        self.assertTrue(routing["allow_fallbacks"])

    def test_env_quantizations_pin_without_order(self) -> None:
        with mock.patch.dict(
            os.environ, {_orc_module.PROVIDER_QUANTIZATIONS_ENV: "fp16,bf16"}, clear=False
        ):
            routing = _orc_module.provider_routing_from_env()
        self.assertEqual(routing["quantizations"], ["fp16", "bf16"])
        # No order pinned → nothing to fail closed against, so the
        # fallback switch must NOT be forced off.
        self.assertNotIn("allow_fallbacks", routing)

    def test_blank_env_yields_no_routing(self) -> None:
        """A blank value must not become ``[""]`` — that would pin every
        call to a provider named empty string and fail every request."""
        for blank in ("", "   ", ",", " , "):
            with self.subTest(blank=blank):
                with mock.patch.dict(
                    os.environ,
                    {
                        _orc_module.PROVIDER_ORDER_ENV: blank,
                        _orc_module.PROVIDER_QUANTIZATIONS_ENV: blank,
                    },
                    clear=False,
                ):
                    self.assertIsNone(_orc_module.provider_routing_from_env())


class TestProviderRoutingIsolation(unittest.TestCase):
    """The routing block is operator configuration, not caller-owned
    mutable state: once a client is built, nothing outside it may change
    what goes on the wire."""

    @staticmethod
    def _client_capturing(captured: dict, routing: dict) -> OpenRouterClient:
        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=_mock_chat_response("{}"))

        return OpenRouterClient(
            api_key=_DUMMY_KEY,
            _transport=httpx.MockTransport(handler),
            provider_routing=routing,
        )

    def test_mutating_the_caller_dict_does_not_change_later_requests(self) -> None:
        routing = {"order": ["DeepInfra"], "allow_fallbacks": False}
        captured: dict = {}
        client = self._client_capturing(captured, routing)
        routing["allow_fallbacks"] = True
        client.generate_content(model="m", contents="json")
        self.assertFalse(captured["provider"]["allow_fallbacks"])

    def test_mutating_the_nested_order_list_does_not_change_later_requests(self) -> None:
        """A shallow copy would pass the test above and still fail here."""
        routing = {"order": ["DeepInfra"], "allow_fallbacks": False}
        captured: dict = {}
        client = self._client_capturing(captured, routing)
        routing["order"].append("SomewhereElse")
        client.generate_content(model="m", contents="json")
        self.assertEqual(captured["provider"]["order"], ["DeepInfra"])

    def test_conflicting_provider_in_config_extra_is_refused(self) -> None:
        """``build_config(**extra)`` is a documented forward-compat channel,
        so it can carry ``provider``. Two disagreeing sources of routing must
        fail loudly — silently preferring one is the hardest kind of bug to
        find in a live pipeline."""
        captured: dict = {}
        client = self._client_capturing(captured, {"order": ["DeepInfra"]})
        config = client.build_config(provider={"order": ["SomewhereElse"]})
        with self.assertRaisesRegex(ValueError, "provider"):
            client.generate_content(model="m", contents="json", config=config)

    def test_provider_in_config_extra_passes_through_when_unpinned(self) -> None:
        """With no pin configured there is no conflict — the forward-compat
        channel keeps working."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=_mock_chat_response("{}"))

        client = OpenRouterClient(api_key=_DUMMY_KEY, _transport=httpx.MockTransport(handler))
        config = client.build_config(provider={"order": ["CallerChoice"]})
        client.generate_content(model="m", contents="json", config=config)
        self.assertEqual(captured["provider"]["order"], ["CallerChoice"])


class TestServingProviderTelemetry(unittest.TestCase):
    """Which provider actually served a call is the missing fact behind
    "same prompt, different answer": without it a routing change and a
    model non-determinism look identical in the logs.
    """

    @staticmethod
    def _client_returning(payload: dict) -> OpenRouterClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        return OpenRouterClient(api_key=_DUMMY_KEY, _transport=httpx.MockTransport(handler))

    def test_response_exposes_serving_provider(self) -> None:
        payload = _mock_chat_response("{}") | {"provider": "DeepInfra"}
        response = self._client_returning(payload).generate_content(
            model="deepseek/deepseek-v4-flash", contents="json"
        )
        self.assertEqual(response.provider, "DeepInfra")

    def test_response_exposes_generation_id_and_served_model(self) -> None:
        response = self._client_returning(_mock_chat_response("{}")).generate_content(
            model="deepseek/deepseek-v4-flash", contents="json"
        )
        self.assertEqual(response.generation_id, "gen-test-123")
        self.assertEqual(response.served_model, "deepseek/deepseek-v4-flash")

    def test_missing_provider_is_none_not_an_error(self) -> None:
        response = self._client_returning(_mock_chat_response("{}")).generate_content(
            model="deepseek/deepseek-v4-flash", contents="json"
        )
        self.assertIsNone(response.provider)

    def test_empty_choices_response_still_carries_provider(self) -> None:
        payload = {"id": "gen-x", "model": "m", "choices": [], "provider": "Novita"}
        response = self._client_returning(payload).generate_content(
            model="deepseek/deepseek-v4-flash", contents="json"
        )
        self.assertEqual(response.text, "")
        self.assertEqual(response.provider, "Novita")

    def test_non_string_provider_is_suppressed(self) -> None:
        """``provider`` is documented as a string. If the upstream shape ever
        changes, a dict must not silently reach consumers that annotate the
        field ``str | None`` — suppress it and say so in the log."""
        payload = _mock_chat_response("{}") | {"provider": {"name": "DeepInfra"}}
        client = self._client_returning(payload)
        with self.assertLogs(_orc_module.logger, level="WARNING") as captured:
            response = client.generate_content(model="m", contents="json")
        self.assertIsNone(response.provider)
        self.assertIn("provider", captured.output[0])

    def test_provider_change_is_logged(self) -> None:
        providers = iter(["DeepInfra", "DeepInfra", "Novita"])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=_mock_chat_response("{}") | {"provider": next(providers)}
            )

        client = OpenRouterClient(api_key=_DUMMY_KEY, _transport=httpx.MockTransport(handler))
        with self.assertLogs(_orc_module.logger, level="INFO") as captured:
            for _ in range(3):
                client.generate_content(model="deepseek/deepseek-v4-flash", contents="json")
        # First sighting + the switch, but NOT the unchanged repeat.
        self.assertEqual(len(captured.output), 2)
        self.assertIn("DeepInfra", captured.output[0])
        self.assertIn("Novita", captured.output[1])

    def test_provider_tracked_per_model(self) -> None:
        """Flash and Pro route independently — a Pro sighting must not
        mask a Flash switch."""
        by_model = {"a": "P1", "b": "P2"}

        def handler(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.content)["model"]
            return httpx.Response(
                200, json=_mock_chat_response("{}") | {"provider": by_model[model]}
            )

        client = OpenRouterClient(api_key=_DUMMY_KEY, _transport=httpx.MockTransport(handler))
        with self.assertLogs(_orc_module.logger, level="INFO") as captured:
            client.generate_content(model="a", contents="json")
            client.generate_content(model="b", contents="json")
            client.generate_content(model="a", contents="json")
        # One first-sighting line per model; the repeat of "a" is silent.
        self.assertEqual(len(captured.output), 2)


if __name__ == "__main__":
    unittest.main()
