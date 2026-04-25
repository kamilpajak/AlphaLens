from __future__ import annotations

import logging

import requests

from ...classifier import ClassifiedEvent, Severity
from .base import AlertHandler

logger = logging.getLogger(__name__)

SEVERITY_EMOJI = {
    Severity.HIGH: "🚨",
    Severity.MEDIUM: "⚠️",
    Severity.LOW: "ℹ️",
}


class TelegramHandler(AlertHandler):
    def __init__(self, bot_token: str, chat_id: str):
        if not bot_token:
            raise ValueError("bot_token required")
        if not chat_id:
            raise ValueError("chat_id required")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def handle(self, classified: ClassifiedEvent) -> None:
        self.send_message(self._format(classified))

    def send_message(self, text: str) -> None:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        }
        try:
            resp = requests.post(self.api_url, json=payload, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Telegram send failed: %s", exc)

    @staticmethod
    def _format(classified: ClassifiedEvent) -> str:
        ev = classified.event
        emoji = SEVERITY_EMOJI.get(classified.severity, "")
        items = ev.raw_data.get("items") or []
        items_str = f" (items: {', '.join(items)})" if items else ""
        action = ev.raw_data.get("insider_action")
        action_str = f" [{action}]" if action else ""

        return (
            f"{emoji} *{classified.severity.name}* — {ev.ticker} "
            f"{ev.form_type.value}{action_str}{items_str}\n"
            f"Relevance: {classified.relevance.value} | Action: {classified.action.value}\n"
            f"Filed: {ev.filed_at.isoformat()}\n"
            f"{ev.url}"
        )
