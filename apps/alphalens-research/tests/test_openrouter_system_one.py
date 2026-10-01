"""Canonical client — the System One surface (`POST /systemone`).

A System One model answers typed questions about a `state` and returns
probabilities, not text. It is a DIFFERENT endpoint from chat completions and a
different request shape, so it gets its own method on the canonical client
rather than a flag on `generate_content`.

Why it belongs on this client at all: `apps/alphalens-research/tests/
test_no_raw_openrouter_http.py` scans `scripts/` as well as the packages, so a
research script cannot post to openrouter.ai itself. Routing it here also keeps
the `HTTP-Referer` / `X-Title` attribution headers the OpenRouter dashboard
needs to split cost per app, and reuses the one TLS keepalive pool per process.

Contracts pinned here:

* **The path is `/systemone`.** Sending a System One body to
  `/chat/completions` is accepted by neither endpoint, and the shapes do not
  overlap: there are no `messages` and there is no completion text.
* **The env provider pin is NOT attached.** `provider_routing_from_env()`
  exists to pin which backend serves DeepSeek. Jev is served by exactly one
  provider, so a pin is at best inert and at worst a refusal, and it would be
  read from the environment — making a script's behaviour depend on the
  operator's shell. `generate_content` attaches it; `system_one` must not.
* **Answers come back typed.** A raw dict would push `answers["x"]["noul"]`
  into every call site and turn a vendor shape change into a `KeyError` deep
  in a loop instead of one failure at the boundary.
* **`usage.cost` is surfaced.** OpenRouter returns the dollar cost per call.
  A batch over a whole store needs to report what it spent without a second
  price table in our code that could drift from the vendor's.
"""

from __future__ import annotations

import json
import unittest

import httpx
from alphalens_pipeline.data.alt_data.openrouter_client import (
    DEFAULT_SYSTEM_ONE_MODEL,
    OPENROUTER_BASE_URL,
    OpenRouterClient,
)

_NOUL_PAYLOAD = {
    "model": "typesafe/jev-1.13-20260917",
    "answers": {"gain": {"type": "noul", "noul": 0.89}},
    "usage": {"input_tokens": 391, "output_tokens": 22, "cost": 1.6422e-05},
    "provider": "TypeSafe",
}


def _client(handler, **kw) -> OpenRouterClient:
    return OpenRouterClient(api_key="test-key", _transport=httpx.MockTransport(handler), **kw)


def _recorder(payload=_NOUL_PAYLOAD):
    """Return (client_factory_handler, seen) so a test can assert on the request."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["path"] = request.url.path
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=payload)

    return handler, seen


class TestTheSystemOneRequest(unittest.TestCase):
    def test_the_call_goes_to_the_system_one_path_not_chat_completions(self):
        # The discriminating test for the whole method: a copy-paste of
        # generate_content would post to /chat/completions and still parse
        # nothing, because the mock answers every path.
        handler, seen = _recorder()
        _client(handler).system_one(
            state="x", questions={"q": {"type": "noul", "instructions": "?"}}
        )
        self.assertEqual(seen["path"], "/api/v1/systemone")
        self.assertTrue(seen["url"].startswith(OPENROUTER_BASE_URL))

    def test_the_state_and_questions_travel_verbatim(self):
        handler, seen = _recorder()
        state = {"article": "ACME won a contract", "ticker": "ACME"}
        questions = {
            "gain": {"type": "noul", "instructions": "Does `ticker` gain?"},
            "kind": {"type": "choice", "instructions": "Which?", "criteria": {"a": "A", "b": "B"}},
        }
        _client(handler).system_one(state=state, questions=questions)
        self.assertEqual(seen["body"]["state"], state)
        self.assertEqual(seen["body"]["questions"], questions)
        self.assertEqual(seen["body"]["model"], DEFAULT_SYSTEM_ONE_MODEL)

    def test_a_plain_string_state_is_accepted(self):
        # The vendor accepts a bare string as well as an object; refusing one
        # here would make the simplest call the awkward one.
        handler, seen = _recorder()
        _client(handler).system_one(
            state="just text", questions={"q": {"type": "noul", "instructions": "?"}}
        )
        self.assertEqual(seen["body"]["state"], "just text")

    def test_the_model_can_be_overridden(self):
        handler, seen = _recorder()
        _client(handler).system_one(
            state="x",
            questions={"q": {"type": "noul", "instructions": "?"}},
            model="typesafe/jev-1.13",
        )
        self.assertEqual(seen["body"]["model"], "typesafe/jev-1.13")

    def test_the_bearer_token_is_sent_and_appears_in_neither_url_nor_body(self):
        handler, seen = _recorder()
        _client(handler).system_one(
            state="x", questions={"q": {"type": "noul", "instructions": "?"}}
        )
        self.assertEqual(seen["headers"]["authorization"], "Bearer test-key")
        self.assertNotIn("test-key", seen["url"])
        self.assertNotIn("test-key", json.dumps(seen["body"]))

    def test_the_attribution_headers_are_sent(self):
        # Dropping these silently un-splits cost on the OpenRouter dashboard,
        # which is the stated reason this client is canonical.
        handler, seen = _recorder()
        _client(handler).system_one(
            state="x", questions={"q": {"type": "noul", "instructions": "?"}}
        )
        self.assertIn("http-referer", seen["headers"])
        self.assertIn("x-title", seen["headers"])

    def test_the_env_provider_pin_is_not_attached_to_a_system_one_call(self):
        # generate_content attaches this block; system_one must not. Jev has one
        # provider, so the pin is inert at best and a refusal at worst — and it
        # would make a script's routing depend on the operator's shell.
        handler, seen = _recorder()
        client = _client(
            handler, provider_routing={"order": ["DeepSeek"], "allow_fallbacks": False}
        )
        client.system_one(state="x", questions={"q": {"type": "noul", "instructions": "?"}})
        self.assertNotIn("provider", seen["body"])


class TestTheSystemOneAnswers(unittest.TestCase):
    def test_a_noul_question_returns_its_probability(self):
        handler, _ = _recorder()
        out = _client(handler).system_one(
            state="x", questions={"gain": {"type": "noul", "instructions": "?"}}
        )
        self.assertEqual(out.answers["gain"].type, "noul")
        self.assertAlmostEqual(out.answers["gain"].noul, 0.89)
        # Noul carries no confidence — the vendor does not return one, and
        # inventing a default would let a caller threshold on a number we made up.
        self.assertIsNone(out.answers["gain"].confidence)

    def test_a_choice_question_returns_the_pick_its_distribution_and_confidence(self):
        payload = {
            "model": "m",
            "answers": {
                "kind": {
                    "type": "choice",
                    "choice": "billing",
                    "probabilities": {"billing": 0.8, "tech": 0.2},
                    "confidence": 0.74,
                }
            },
            "usage": {"input_tokens": 10, "output_tokens": 1, "cost": 1e-06},
        }
        handler, _ = _recorder(payload)
        a = (
            _client(handler)
            .system_one(state="x", questions={"kind": {"type": "choice", "instructions": "?"}})
            .answers["kind"]
        )
        self.assertEqual(a.choice, "billing")
        self.assertEqual(a.probabilities, {"billing": 0.8, "tech": 0.2})
        self.assertAlmostEqual(a.confidence, 0.74)

    def test_a_score_question_returns_the_position_and_its_distribution(self):
        payload = {
            "model": "m",
            "answers": {
                "sev": {
                    "type": "score",
                    "score": 1.4,
                    "probabilities": {"0": 0.1, "1": 0.4, "2": 0.5},
                    "confidence": 0.6,
                }
            },
            "usage": {"input_tokens": 10, "output_tokens": 1, "cost": 1e-06},
        }
        handler, _ = _recorder(payload)
        a = (
            _client(handler)
            .system_one(state="x", questions={"sev": {"type": "score", "instructions": "?"}})
            .answers["sev"]
        )
        self.assertAlmostEqual(a.score, 1.4)
        self.assertAlmostEqual(a.confidence, 0.6)
        self.assertEqual(a.probabilities, {"0": 0.1, "1": 0.4, "2": 0.5})

    def test_several_questions_come_back_under_their_own_ids(self):
        payload = {
            "model": "m",
            "answers": {"a": {"type": "noul", "noul": 0.1}, "b": {"type": "noul", "noul": 0.9}},
            "usage": {"input_tokens": 10, "output_tokens": 2, "cost": 1e-06},
        }
        handler, _ = _recorder(payload)
        out = _client(handler).system_one(
            state="x",
            questions={
                "a": {"type": "noul", "instructions": "?"},
                "b": {"type": "noul", "instructions": "?"},
            },
        )
        self.assertEqual(sorted(out.answers), ["a", "b"])
        self.assertAlmostEqual(out.answers["a"].noul, 0.1)
        self.assertAlmostEqual(out.answers["b"].noul, 0.9)

    def test_usage_cost_and_input_tokens_are_surfaced(self):
        handler, _ = _recorder()
        out = _client(handler).system_one(
            state="x", questions={"gain": {"type": "noul", "instructions": "?"}}
        )
        self.assertAlmostEqual(out.cost_usd, 1.6422e-05)
        self.assertEqual(out.input_tokens, 391)

    def test_the_answering_model_id_is_reported_not_the_alias_we_sent(self):
        # The alias moves. A batch must be able to record which build answered.
        handler, _ = _recorder()
        out = _client(handler).system_one(
            state="x", questions={"gain": {"type": "noul", "instructions": "?"}}
        )
        self.assertEqual(out.model, "typesafe/jev-1.13-20260917")

    def test_an_answer_type_the_wrapper_does_not_model_is_still_reported(self):
        # Forward compatibility: a new primitive must not crash a batch. The
        # type is surfaced so a caller can skip it, and the modelled fields
        # stay None rather than being guessed from an unknown shape.
        payload = {
            "model": "m",
            "answers": {"q": {"type": "quantile", "quantiles": [1, 2]}},
            "usage": {"input_tokens": 1, "output_tokens": 1, "cost": 0.0},
        }
        handler, _ = _recorder(payload)
        a = (
            _client(handler)
            .system_one(state="x", questions={"q": {"type": "noul", "instructions": "?"}})
            .answers["q"]
        )
        self.assertEqual(a.type, "quantile")
        self.assertIsNone(a.noul)
        self.assertIsNone(a.choice)
        self.assertIsNone(a.score)

    def test_a_missing_usage_block_does_not_crash(self):
        payload = {"model": "m", "answers": {"q": {"type": "noul", "noul": 0.5}}}
        handler, _ = _recorder(payload)
        out = _client(handler).system_one(
            state="x", questions={"q": {"type": "noul", "instructions": "?"}}
        )
        self.assertIsNone(out.cost_usd)
        self.assertIsNone(out.input_tokens)


class TestTheSystemOneFailures(unittest.TestCase):
    def test_a_non_2xx_raises_rather_than_returning_an_empty_answer_set(self):
        # Silently returning no answers would let a batch write a column of
        # nulls and call itself a success.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})

        with self.assertRaises(httpx.HTTPStatusError):
            _client(handler).system_one(
                state="x", questions={"q": {"type": "noul", "instructions": "?"}}
            )

    def test_an_empty_questions_mapping_is_refused_before_the_call(self):
        # A call with no questions costs input tokens and answers nothing.
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json=_NOUL_PAYLOAD)

        with self.assertRaises(ValueError):
            _client(handler).system_one(state="x", questions={})
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
