from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import requests

logger = logging.getLogger(__name__)

API_URL = "https://api.perplexity.ai/chat/completions"
DEFAULT_MODEL = "sonar-pro"
DEFAULT_TIMEOUT_SECONDS = 120

SearchContextSize = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class AskResult:
    content: str
    citations: list[str]
    search_results: list[dict]


class PerplexityClient:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        if not api_key:
            raise ValueError("api_key required")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def ask(
        self,
        query: str,
        search_context_size: SearchContextSize = "medium",
        search_recency_filter: str | None = None,
    ) -> str:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": query}],
            "web_search_options": {"search_context_size": search_context_size},
        }
        if search_recency_filter:
            payload["search_recency_filter"] = search_recency_filter

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def ask_with_citations(
        self,
        query: str,
        *,
        search_context_size: SearchContextSize = "medium",
        search_recency_filter: str | None = None,
        search_after_date_filter: str | None = None,
        search_before_date_filter: str | None = None,
    ) -> AskResult:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": query}],
            "web_search_options": {"search_context_size": search_context_size},
        }
        if search_recency_filter:
            payload["search_recency_filter"] = search_recency_filter
        if search_after_date_filter:
            payload["search_after_date_filter"] = search_after_date_filter
        if search_before_date_filter:
            payload["search_before_date_filter"] = search_before_date_filter

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        raw_citations = data.get("citations") or []
        if isinstance(raw_citations, str):
            raw_citations = [raw_citations]
        raw_results = data.get("search_results") or []
        if isinstance(raw_results, dict):
            raw_results = [raw_results]
        return AskResult(
            content=content, citations=list(raw_citations), search_results=list(raw_results)
        )
