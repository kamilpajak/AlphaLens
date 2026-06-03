"""Live NVDA -> QUBT/IONQ chain probe — opt-in via NVDA_QUBT_LIVE_TEST=1.

The end-to-end counterpart of the hermetic
``tests/thematic/test_nvda_qubt_integration.py``. The hermetic test pins our
call contracts against our own mocks; it is blind to real-data health — a
DeepSeek model that stops returning themes, a Polygon news-schema change, or a
SEC-EDGAR access break would all pass it. This probe runs the REAL chain on the
2026-04-14 NVDA quantum-launch scenario and asserts SHAPE + non-emptiness only,
never values (the LLM is non-deterministic, so QUBT/IONQ are NOT pinned here).

Three independent shape probes (decoupled with hardcoded inputs so each stage's
break localises):
  * extract  — ``extract_one(NEWS_ROW)`` returns a dict with a non-empty
               ``themes`` list (None = the silent-empty / model-retired signal).
  * propose  — ``propose_candidates(theme="quantum_computing")`` returns the
               ``{"candidates": [...], "search_keywords": [...]}`` dict shape
               with at least one ticker-bearing candidate.
  * verify   — ``verify_candidate(ticker="IONQ", ...)`` returns the
               ``gates_*`` / ``verified`` verdict keys (gate pass/fail is data-
               dependent, so only the shape is asserted).

COSTS REAL MONEY (two DeepSeek calls: extract Flash + mapper Pro) and hits
Polygon news + SEC EDGAR (the verify gates). Opt-in + weekly only, NEVER per-PR
(``@skipUnless`` skips it under the default ``unittest discover``).

    NVDA_QUBT_LIVE_TEST=1 .venv/bin/python -m unittest tests.live.test_nvda_qubt_live -v
"""

from __future__ import annotations

import datetime as dt
import os
import unittest

from tests.live import PermanentProbeError, TransientProbeError, run_probes

_LIVE = os.environ.get("NVDA_QUBT_LIVE_TEST") == "1"

ASOF = dt.date(2026, 4, 14)
POSITIVE_CONTROL = "IONQ"

NEWS_ROW = {
    "id": "nvda_ising_2026_04_14",
    "source": "nvidianews.nvidia.com",
    "tickers": ["NVDA"],
    "title": (
        "NVIDIA Launches Ising, the World's First Open AI Models to Accelerate "
        "the Path to Useful Quantum Computers"
    ),
    "body": (
        "NVIDIA today announced NVIDIA Ising, a family of open AI models for "
        "quantum error correction and calibration, complementing the CUDA-Q "
        "platform and the NVQLink QPU-GPU interconnect. The tools target two of "
        "the hardest problems in building useful quantum computers."
    ),
    "published_at": "2026-04-14T13:00:00Z",
}


def _classify_llm_error(exc: Exception) -> None:
    """Re-raise an httpx error as transient (429 / 5xx / network) or permanent."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429 or 500 <= code < 600:
            raise TransientProbeError(f"HTTP {code}") from exc
        raise PermanentProbeError(f"HTTP {code}: {exc}") from exc
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        raise TransientProbeError(f"network error: {exc}") from exc
    raise PermanentProbeError(f"unexpected error: {exc}") from exc


@unittest.skipUnless(_LIVE, "set NVDA_QUBT_LIVE_TEST=1 to run the live NVDA/QUBT chain probe")
class TestNvdaQubtLive(unittest.TestCase):
    def test_chain_extract_propose_verify_shape(self):
        from alphalens_pipeline.data.alt_data.polygon_client import get_default_polygon_client
        from alphalens_pipeline.thematic.extraction import event_extractor
        from alphalens_pipeline.thematic.mapping import orchestrator, theme_mapper

        def _probe_extract() -> None:
            # extract_one swallows LLM failures and returns None — that None IS
            # the silent-empty / model-retired signal this layer exists to catch.
            event = event_extractor.extract_one(NEWS_ROW)
            if event is None:
                raise PermanentProbeError(
                    "extract_one returned None (no event from non-empty news)"
                )
            themes = event.get("themes")
            if not isinstance(themes, list) or not themes:
                raise PermanentProbeError(f"extract_one produced no themes: {themes!r}")

        def _probe_propose() -> None:
            try:
                result = theme_mapper.propose_candidates(theme="quantum_computing")
            except Exception as exc:
                _classify_llm_error(exc)
                return
            if not isinstance(result, dict) or "candidates" not in result:
                raise PermanentProbeError(f"propose_candidates broke its dict shape: {result!r}")
            candidates = result["candidates"]
            if not isinstance(candidates, list) or not candidates:
                raise PermanentProbeError("propose_candidates returned no candidates")
            if not all("ticker" in c for c in candidates):
                raise PermanentProbeError("a candidate is missing its ticker key")

        def _probe_verify() -> None:
            try:
                verdict = orchestrator.verify_candidate(
                    ticker=POSITIVE_CONTROL,
                    themes=["quantum_computing"],
                    asof=ASOF,
                    polygon_client=get_default_polygon_client(),
                )
            except Exception as exc:
                _classify_llm_error(exc)
                return
            for key in ("ticker", "gates_passed", "gates_failed", "gates_unknown", "verified"):
                if key not in verdict:
                    raise PermanentProbeError(
                        f"verify_candidate verdict missing {key!r}: {verdict!r}"
                    )

        run_probes(
            self,
            {"extract": _probe_extract, "propose": _probe_propose, "verify": _probe_verify},
            label="nvda-qubt",
        )


if __name__ == "__main__":
    unittest.main()
