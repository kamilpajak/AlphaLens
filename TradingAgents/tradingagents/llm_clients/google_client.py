import logging
import re
import time
from typing import Any, Optional

from langchain_google_genai import ChatGoogleGenerativeAI

from .base_client import BaseLLMClient, normalize_content
from .validators import validate_model

logger = logging.getLogger(__name__)

_QUOTA_MAX_RETRIES = 10
_QUOTA_BASE_DELAY = 40.0  # seconds — Google recommends ~37s


class NormalizedChatGoogleGenerativeAI(ChatGoogleGenerativeAI):
    """ChatGoogleGenerativeAI with normalized content output.

    Gemini 3 models return content as list of typed blocks.
    This normalizes to string for consistent downstream handling.

    Adds retry logic for 429 RESOURCE_EXHAUSTED quota errors that
    require longer waits than the default SDK retry (which maxes at ~8s).
    """

    def invoke(self, input, config=None, **kwargs):
        for attempt in range(_QUOTA_MAX_RETRIES + 1):
            try:
                return normalize_content(super().invoke(input, config, **kwargs))
            except Exception as e:
                if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
                    if attempt < _QUOTA_MAX_RETRIES:
                        delay = _parse_retry_delay(str(e)) or _QUOTA_BASE_DELAY
                        logger.warning(
                            "Gemini quota exceeded, waiting %.0fs before retry %d/%d",
                            delay, attempt + 1, _QUOTA_MAX_RETRIES,
                        )
                        time.sleep(delay)
                        continue
                raise


def _parse_retry_delay(error_msg: str) -> float | None:
    """Extract retry delay from Google API error message."""
    match = re.search(r"retryDelay.*?(\d+(?:\.\d+)?)\s*s", error_msg)
    if match:
        return float(match.group(1)) + 2.0  # add buffer
    return None


class GoogleClient(BaseLLMClient):
    """Client for Google Gemini models."""

    def __init__(self, model: str, base_url: Optional[str] = None, **kwargs):
        super().__init__(model, base_url, **kwargs)

    def get_llm(self) -> Any:
        """Return configured ChatGoogleGenerativeAI instance."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}

        if self.base_url:
            llm_kwargs["base_url"] = self.base_url

        for key in ("timeout", "max_retries", "callbacks", "http_client", "http_async_client"):
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        # Unified api_key maps to provider-specific google_api_key
        google_api_key = self.kwargs.get("api_key") or self.kwargs.get("google_api_key")
        if google_api_key:
            llm_kwargs["google_api_key"] = google_api_key

        # Map thinking_level to appropriate API param based on model
        # Gemini 3 Pro: low, high
        # Gemini 3 Flash: minimal, low, medium, high
        # Gemini 2.5: thinking_budget (0=disable, -1=dynamic)
        thinking_level = self.kwargs.get("thinking_level")
        if thinking_level:
            model_lower = self.model.lower()
            if "gemini-3" in model_lower:
                # Gemini 3 Pro doesn't support "minimal", use "low" instead
                if "pro" in model_lower and thinking_level == "minimal":
                    thinking_level = "low"
                llm_kwargs["thinking_level"] = thinking_level
            else:
                # Gemini 2.5: map to thinking_budget
                llm_kwargs["thinking_budget"] = -1 if thinking_level == "high" else 0

        return NormalizedChatGoogleGenerativeAI(**llm_kwargs)

    def validate_model(self) -> bool:
        """Validate model for Google."""
        return validate_model("google", self.model)
