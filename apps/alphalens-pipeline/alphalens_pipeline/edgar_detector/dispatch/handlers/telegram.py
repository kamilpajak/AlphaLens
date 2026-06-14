from __future__ import annotations

from ....data.alt_data.telegram_client import TelegramClient
from ...classifier import ClassifiedEvent, Severity
from .base import AlertHandler

SEVERITY_EMOJI = {
    Severity.HIGH: "🚨",
    Severity.MEDIUM: "⚠️",
    Severity.LOW: "ℹ️",
}


class TelegramHandler(AlertHandler):
    """Format a classified EDGAR event and deliver it via the canonical
    :class:`TelegramClient` (shared retry + token-sanitised logging)."""

    def __init__(self, bot_token: str, chat_id: str, *, client: TelegramClient | None = None):
        if not chat_id:
            raise ValueError("chat_id required")
        self.chat_id = chat_id
        # The client validates bot_token (raises ValueError on empty) and owns
        # the HTTP + retry + credential sanitisation.
        self._client = client or TelegramClient(bot_token)

    def handle(self, classified: ClassifiedEvent) -> None:
        self.send_message(self._format(classified))

    def send_message(self, text: str) -> None:
        self._client.send_message(self.chat_id, text)

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
