"""VCR-style OpenRouter cassette player + recorder (test-strategy Phase 3, L3).

The L3 golden-master replay drives the real thematic pipeline deterministically
and offline. Every LLM call goes through ``OpenRouterClient.generate_content``;
this module records each real (model, contents, config) request → response into
a human-named cassette dir, and replays it later keyed on the FULL request
descriptor.

Two design rules from the test-strategy memo (§3 L3 + §10):

* **Key on the full request descriptor**, not ``sha256(model+prompt)`` alone —
  sampling params + the synthesised system message change the output, so the
  key includes model + contents + the whole ``OpenRouterConfig`` (response_format,
  temperature, max_tokens, system_message, extra).
* **Fail loud on a cache miss.** A changed prompt / model / param is a behaviour
  change to re-record deliberately (run ``record_golden_brief.py`` again), never
  a silent fall-through to a live call.

Both fakes are duck-typed against ``OpenRouterClient``: callers do
``client.build_config(...)`` then ``client.generate_content(model=, contents=,
config=)``. ``build_config`` is self-independent on the real client, so the
replay fake reuses it as the single source of truth rather than duplicating the
Gemini→OpenAI translation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alphalens_pipeline.data.alt_data.openrouter_client import (
    OpenRouterClient,
    OpenRouterConfig,
    _wrap_response,
)

logger = logging.getLogger(__name__)


class CassetteMissError(KeyError):
    """Raised when a replay request has no recorded cassette (fail-loud)."""


def _config_to_dict(config: OpenRouterConfig | None) -> dict[str, Any]:
    """Canonical, JSON-serialisable view of the request config for keying.

    Mirrors exactly the fields ``generate_content`` forwards to the OpenRouter
    body plus the synthesised system message (which IS part of the request and
    changes the output). ``None`` config → ``{}`` so a config-less call keys
    distinctly from an empty-config call only if the caller passes one.
    """
    if config is None:
        return {}
    return {
        "response_format": config.response_format,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "system_message": config.system_message,
        "extra": config.extra,
    }


def cassette_key(*, model: str, contents: str, config: OpenRouterConfig | None) -> str:
    """sha256 over the canonical JSON of the full request descriptor."""
    descriptor = {
        "model": model,
        "contents": contents,
        "config": _config_to_dict(config),
    }
    blob = json.dumps(descriptor, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ReplayOpenRouter:
    """Offline replacement for ``OpenRouterClient`` backed by recorded cassettes.

    Loads every ``*.json`` cassette under ``cassette_dir`` at construction and
    serves ``generate_content`` from them. A miss raises :class:`CassetteMissError`
    when ``fail_on_miss`` (the default) — re-record rather than silently call out.
    """

    def __init__(self, cassette_dir: Path | str, *, fail_on_miss: bool = True) -> None:
        self._dir = Path(cassette_dir)
        self._fail_on_miss = fail_on_miss
        self._cache: dict[str, dict[str, Any]] = {}
        for path in sorted(self._dir.glob("*.json")):
            record = json.loads(path.read_text())
            self._cache[record["key"]] = record

    def build_config(self, **kwargs: Any) -> OpenRouterConfig:
        # build_config is self-independent on the real client (it reads only its
        # kwargs), so reuse it as the single source of truth for the
        # Gemini→OpenAI translation rather than duplicating it here. The
        # type-ignore is because we pass this fake as ``self``; it is never
        # dereferenced inside build_config.
        return OpenRouterClient.build_config(self, **kwargs)  # type: ignore[arg-type]

    def generate_content(
        self,
        *,
        model: str,
        contents: str,
        config: OpenRouterConfig | None = None,
    ) -> SimpleNamespace:
        key = cassette_key(model=model, contents=contents, config=config)
        record = self._cache.get(key)
        if record is None:
            if self._fail_on_miss:
                raise CassetteMissError(
                    f"no cassette for model={model!r} key={key} in {self._dir} — "
                    "re-record with record_golden_brief.py (a changed prompt / "
                    "model / param is a behaviour change, not a live-call fallback)"
                )
            logger.warning("replay cassette miss (key=%s) — returning empty response", key)
            return _wrap_response({"choices": []})
        # Re-wrap the recorded upstream payload through the SAME _wrap_response
        # the production client uses, so .text / .candidates[0].finish_reason.name
        # are byte-identical to a live call.
        return _wrap_response(record["openrouter_response"])

    def __len__(self) -> int:
        return len(self._cache)


class RecordingOpenRouter:
    """Wrap a real ``OpenRouterClient`` and tee every response to a cassette.

    Used once by ``record_golden_brief.py`` against the live API. Each call is
    forwarded to the real client, then the (descriptor → raw payload) pair is
    written to ``cassette_dir/{key}.json``. The cassette stores the human-readable
    request fields alongside the key so a reviewer can see what was recorded.
    """

    def __init__(self, real_client: OpenRouterClient, cassette_dir: Path | str) -> None:
        self._real = real_client
        self._dir = Path(cassette_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def build_config(self, **kwargs: Any) -> OpenRouterConfig:
        return self._real.build_config(**kwargs)

    def generate_content(
        self,
        *,
        model: str,
        contents: str,
        config: OpenRouterConfig | None = None,
    ) -> SimpleNamespace:
        response = self._real.generate_content(model=model, contents=contents, config=config)
        key = cassette_key(model=model, contents=contents, config=config)
        record = {
            "key": key,
            "model": model,
            "contents": contents,
            "config": _config_to_dict(config),
            "openrouter_response": response._raw,
        }
        (self._dir / f"{key}.json").write_text(
            json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False)
        )
        return response


__all__ = [
    "CassetteMissError",
    "RecordingOpenRouter",
    "ReplayOpenRouter",
    "cassette_key",
]
