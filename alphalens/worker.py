"""Drains the candidate queue and runs TradingAgents through the shared runner.

Concerns handled here:
  - daily budget guard (shared across all sources)
  - per-candidate exception handling → mark_failure (which schedules retry or moves to DLQ)
  - Telegram / notifier updates on success/failure
"""

from __future__ import annotations

import logging
from typing import Protocol

from .candidates import Candidate
from .queue import CandidateQueue
from .runner import TradingAgentsRunner

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_PER_DAY = 5


class Sender(Protocol):
    def send_message(self, text: str) -> None: ...


class AnalysisWorker:
    def __init__(
        self,
        queue: CandidateQueue,
        runner: TradingAgentsRunner,
        notifier: Sender,
        budget_per_day: int = DEFAULT_BUDGET_PER_DAY,
    ):
        self.queue = queue
        self.runner = runner
        self.notifier = notifier
        self.budget_per_day = budget_per_day

    def process_one(self) -> bool:
        done_today = self.queue.count_done_today()
        if done_today >= self.budget_per_day:
            # Log locally — don't notify. Worker fires every 5 min; Telegraming on
            # every exhausted tick spams 12/h until midnight. Success messages
            # already intrinsically signal the budget state.
            logger.info(
                "Budget exhausted (%d/%d). Skipping pending jobs until tomorrow.",
                done_today,
                self.budget_per_day,
            )
            return False

        job = self.queue.claim_next()
        if job is None:
            return False

        candidate = _rehydrate_candidate(job)
        candidate_id = int(job["id"])

        try:
            result = self.runner.run(candidate, candidate_id=candidate_id)
            self.queue.mark_success(
                candidate_id,
                decision=result.rating,
                duration_sec=result.duration_sec,
                cost_usd=result.cost_usd,
                model_used=result.model_used,
            )
            self._notify(_format_success(candidate, result.rating, job))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Worker runner failed for %s (source=%s): %s",
                candidate.ticker,
                candidate.source,
                exc,
                exc_info=True,
            )
            self.queue.mark_failure(candidate_id, error=str(exc))
            self._notify(f"❌ Auto-analysis failed for {candidate.ticker}: {exc}")

        return True

    def process_all(self) -> int:
        count = 0
        while self.process_one():
            count += 1
        return count

    def close(self) -> None:
        self.queue.close()

    def _notify(self, msg: str) -> None:
        try:
            self.notifier.send_message(msg)
        except Exception as exc:  # noqa: BLE001
            logger.error("Notifier send failed: %s", exc)


def _rehydrate_candidate(job: dict) -> Candidate:
    """Build a Candidate from a queue row. Used by the worker to hand off to the runner."""
    import json
    from datetime import datetime

    payload = json.loads(job["payload"]) if job.get("payload") else {}
    return Candidate(
        ticker=job["ticker"],
        source=job["source"],
        detected_at=datetime.fromisoformat(job["enqueued_at"]),
        priority=int(job["priority"]),
        payload=payload,
        dedup_key=job["dedup_key"],
    )


def _format_success(candidate: Candidate, rating: str, job: dict) -> str:
    lines = [
        f"🤖 Auto-analysis done — {candidate.ticker}",
        f"Decision: *{rating}*",
        f"Source: {candidate.source}",
    ]
    url = candidate.payload.get("url") if candidate.source == "watchdog_sec" else None
    if url:
        lines.append(f"Trigger: {url}")
    return "\n".join(lines)
