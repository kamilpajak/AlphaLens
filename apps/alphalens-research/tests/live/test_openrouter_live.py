"""Live OpenRouter / DeepSeek probe — opt-in via OPENROUTER_LIVE_TEST=1.

Pins incident #3: a retired model id returns 404, ``event_extractor.extract_one``
swallows it (returns None), the day yields 0 events from non-empty news, and the
run still exits 0 — a silent empty brief (the retired-Gemini class). This probe
makes the model-id contract LOUD: each model the pipeline depends on must answer
200, finish cleanly (not MAX_TOKENS), and return JSON that conforms to
``EVENT_RESPONSE_SCHEMA``. A 404 is a PERMANENT failure (the exact retirement
signal), so it fails even though it is a single model.

COSTS REAL MONEY (no free tier on v4-pro) — one small extraction call per model,
two models total (fractions of a cent/run; the JSON is tiny even though the
token cap mirrors the production 8000 budget). That is why it is opt-in +
weekly, NEVER per-PR.

    OPENROUTER_LIVE_TEST=1 .venv/bin/python -m unittest tests.live.test_openrouter_live -v
"""

from __future__ import annotations

import json
import os
import unittest

from tests.live import PermanentProbeError, TransientProbeError, run_probes

_LIVE = os.environ.get("OPENROUTER_LIVE_TEST") == "1"

# A short, unambiguous M&A headline that MUST extract to an EVENT_RESPONSE_SCHEMA
# object. The schema (incl. the event_type/sentiment enums) is injected into the
# system message by build_config(response_schema=...), so a healthy model returns
# conforming JSON; we assert the SHAPE, never the field values.
_PROMPT = (
    "Extract the event from this headline as a json object: "
    "'Acme Corp agrees to acquire Beta Inc for $5 billion in cash.'"
)


def _model_ids() -> dict[str, str]:
    """The model ids the pipeline actually depends on, read from the live
    constants so this probe fails when a swap forgets to update a model id."""
    from alphalens_pipeline.thematic.argumentation.generator import FLASH_MODEL, PRO_MODEL

    return {"extract/brief-flash": FLASH_MODEL, "mapper/brief-pro": PRO_MODEL}


@unittest.skipUnless(_LIVE, "set OPENROUTER_LIVE_TEST=1 to run the live OpenRouter probe")
class TestOpenRouterLive(unittest.TestCase):
    def test_models_resolve_and_return_schema_shaped_json(self):
        import httpx
        import jsonschema
        from alphalens_pipeline.data.alt_data.openrouter_client import (
            get_default_openrouter_client,
        )
        from alphalens_pipeline.thematic.extraction.schema import EVENT_RESPONSE_SCHEMA

        client = get_default_openrouter_client()
        config = client.build_config(
            response_mime_type="application/json",
            response_schema=EVENT_RESPONSE_SCHEMA,
            # Mirror the production extractor budget (event_extractor uses 8000):
            # v4-pro is a reasoning model, so a tight cap truncates (MAX_TOKENS)
            # even on a trivial extraction. The JSON itself is tiny, so real
            # cost stays a fraction of a cent — the cap only bounds the worst case.
            max_output_tokens=8000,
        )

        def _make(model: str):
            def _probe() -> None:
                try:
                    resp = client.generate_content(model=model, contents=_PROMPT, config=config)
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    if code == 404:
                        raise PermanentProbeError(
                            f"model {model} returned 404 — retired? (the #3 signal)"
                        ) from exc
                    # 429 + any 5xx are server-side / rate-limit glitches ->
                    # transient; only a 4xx (other than 404) is a real shape /
                    # contract break -> permanent.
                    if code == 429 or 500 <= code < 600:
                        raise TransientProbeError(f"{model} HTTP {code}") from exc
                    raise PermanentProbeError(f"{model} HTTP {code}: {exc}") from exc
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    raise TransientProbeError(f"{model} network error: {exc}") from exc

                reason = resp.candidates[0].finish_reason.name
                if reason == "MAX_TOKENS":
                    raise PermanentProbeError(f"{model} truncated (finish_reason=MAX_TOKENS)")
                text = (getattr(resp, "text", "") or "").strip()
                if not text:
                    raise PermanentProbeError(f"{model} returned empty text (no choices?)")
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise PermanentProbeError(f"{model} non-JSON body: {exc}") from exc
                # SHAPE-ONLY: schema conformance (keys + enums + types), never values.
                try:
                    jsonschema.validate(payload, EVENT_RESPONSE_SCHEMA)
                except jsonschema.ValidationError as exc:
                    raise PermanentProbeError(
                        f"{model} JSON violates EVENT_RESPONSE_SCHEMA: {exc.message}"
                    ) from exc

            return _probe

        run_probes(self, {n: _make(m) for n, m in _model_ids().items()}, label="openrouter")


if __name__ == "__main__":
    unittest.main()
