"""GuruScorer — single-prompt Gemini LLM call returning conviction 0-100.

Follows the GuruAgent paper insight (arXiv 2510.01664): prompt engineering
drives outperformance, not model sophistication. So this scorer is intentionally
simple — ONE LLM call per (ticker, asof) pair, structured JSON output parsed
with regex fallback, disk cache keyed by (prompt_sha, ticker, asof) for
reproducibility.

Cost tracking reads ``response.usage_metadata`` (LangChain-standard shape for
Gemini via ChatGoogleGenerativeAI). Prices parametrized so they can be updated
when Google changes Gemini 3.1 Pro pricing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from alphalens.archive.guru.prompt import GuruPrompt

logger = logging.getLogger(__name__)

# Gemini 3.1 Pro preview pricing (April 2026 snapshot; update as needed)
DEFAULT_INPUT_PRICE_PER_1M = 1.25
DEFAULT_OUTPUT_PRICE_PER_1M = 5.00


class ScorerError(RuntimeError):
    """Raised when the LLM response cannot be parsed into a conviction score."""


@dataclass(frozen=True)
class ConvictionResult:
    ticker: str
    asof: pd.Timestamp
    conviction: float
    rationale: str
    prompt_sha: str
    raw_response: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class GuruScorer:
    def __init__(
        self,
        *,
        prompt: GuruPrompt,
        llm: Any,
        cache_dir: Path,
        input_price_per_1m: float = DEFAULT_INPUT_PRICE_PER_1M,
        output_price_per_1m: float = DEFAULT_OUTPUT_PRICE_PER_1M,
    ):
        self._prompt = prompt
        self._llm = llm
        self._cache_dir = Path(cache_dir) / prompt.content_sha256[:16]
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._input_price = input_price_per_1m
        self._output_price = output_price_per_1m

    def score(
        self,
        *,
        ticker: str,
        asof: pd.Timestamp,
        context_text: str,
    ) -> ConvictionResult:
        cache_path = self._cache_path(ticker, asof)
        cached = self._load_cached(cache_path)
        if cached is not None:
            return cached

        full_prompt = f"{self._prompt.text}\n\n---\n\n{context_text}"
        response = self._llm.invoke(full_prompt)
        raw_content = getattr(response, "content", "") or ""
        conviction, rationale = self._parse_response(raw_content)
        input_tokens, output_tokens = self._extract_token_counts(response)
        cost = self._compute_cost(input_tokens, output_tokens)

        result = ConvictionResult(
            ticker=ticker.upper(),
            asof=pd.Timestamp(asof),
            conviction=conviction,
            rationale=rationale,
            prompt_sha=self._prompt.content_sha256,
            raw_response=raw_content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )
        self._save_cached(cache_path, result)
        return result

    def _cache_path(self, ticker: str, asof: pd.Timestamp) -> Path:
        asof_str = pd.Timestamp(asof).strftime("%Y%m%d")
        return self._cache_dir / f"{ticker.upper()}_{asof_str}.json"

    def _load_cached(self, path: Path) -> ConvictionResult | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        try:
            return ConvictionResult(
                ticker=data["ticker"],
                asof=pd.Timestamp(data["asof"]),
                conviction=float(data["conviction"]),
                rationale=data["rationale"],
                prompt_sha=data["prompt_sha"],
                raw_response=data["raw_response"],
                input_tokens=int(data["input_tokens"]),
                output_tokens=int(data["output_tokens"]),
                cost_usd=float(data["cost_usd"]),
            )
        except (KeyError, ValueError, TypeError):
            return None

    def _save_cached(self, path: Path, result: ConvictionResult) -> None:
        data = asdict(result)
        data["asof"] = result.asof.strftime("%Y-%m-%d")
        path.write_text(json.dumps(data, indent=2))

    def _parse_response(self, content: str) -> tuple[float, str]:
        extracted = _strip_markdown_codeblock(content)
        try:
            parsed = json.loads(extracted)
        except json.JSONDecodeError:
            match = re.search(r"\{[^{}]*\"conviction\"[^{}]*\}", content, re.DOTALL)
            if not match:
                raise ScorerError(
                    f"cannot parse conviction JSON from response: {content[:200]}"
                ) from None
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise ScorerError(f"malformed JSON in response: {exc}") from exc

        raw_conviction = parsed.get("conviction")
        if raw_conviction is None:
            raise ScorerError(f"response missing 'conviction' key: {parsed}")
        try:
            conviction = float(raw_conviction)
        except (TypeError, ValueError) as exc:
            raise ScorerError(f"conviction not numeric: {raw_conviction!r}") from exc
        conviction = max(0.0, min(100.0, conviction))
        rationale = str(parsed.get("rationale", ""))
        return conviction, rationale

    def _extract_token_counts(self, response: Any) -> tuple[int, int]:
        usage = getattr(response, "usage_metadata", None)
        if isinstance(usage, dict):
            return (
                int(usage.get("input_tokens", 0)),
                int(usage.get("output_tokens", 0)),
            )
        return 0, 0

    def _compute_cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1_000_000.0 * self._input_price
            + output_tokens / 1_000_000.0 * self._output_price
        )


def _strip_markdown_codeblock(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        # Remove leading ```json or ``` and trailing ```
        lines = stripped.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped
