from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .portfolio import PortfolioState, Relevance
from .types import Event, FormType

HIGH_IMPACT_8K_ITEMS = {
    "1.01",   # Material definitive agreement
    "1.02",   # Termination of material agreement
    "2.04",   # Triggering events for material financial obligation
    "4.02",   # Non-reliance on prior financial statements
    "5.02",   # Departure/appointment of directors or officers
    "5.03",   # Amendments to bylaws / fiscal year change
}

LARGE_INSIDER_BUY_USD = 500_000
SMALL_TRANSACTION_USD = 50_000


class Severity(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Action(Enum):
    AUTO_TRIGGER = "auto_trigger"
    APPROVAL = "approval"
    DIGEST = "digest"
    IGNORE = "ignore"


@dataclass
class ClassifiedEvent:
    event: Event
    severity: Severity
    relevance: Relevance
    action: Action


class SignalClassifier:
    def classify(self, event: Event, portfolio: PortfolioState) -> ClassifiedEvent:
        severity = self._severity_for(event)
        relevance = portfolio.relevance_for(event.ticker)
        action = self._action_for(severity, relevance)
        return ClassifiedEvent(event=event, severity=severity, relevance=relevance, action=action)

    def _severity_for(self, event: Event) -> Severity:
        form = event.form_type
        raw = event.raw_data

        if form in (FormType.FORM_13D, FormType.FORM_13G,
                    FormType.FORM_13D_A, FormType.FORM_13G_A):
            return Severity.HIGH

        if form == FormType.FORM_8K:
            items = set(raw.get("items") or [])
            return Severity.HIGH if items & HIGH_IMPACT_8K_ITEMS else Severity.MEDIUM

        if form == FormType.FORM_4:
            action = raw.get("insider_action")
            value = raw.get("transaction_value_usd", 0) or 0
            if action == "BUY" and value >= LARGE_INSIDER_BUY_USD:
                return Severity.HIGH
            if value and value < SMALL_TRANSACTION_USD:
                return Severity.LOW
            return Severity.MEDIUM

        return Severity.LOW

    def _action_for(self, severity: Severity, relevance: Relevance) -> Action:
        if severity == Severity.HIGH:
            if relevance == Relevance.HELD:
                return Action.AUTO_TRIGGER
            return Action.APPROVAL
        if severity == Severity.MEDIUM:
            if relevance == Relevance.FOREIGN:
                return Action.DIGEST
            return Action.APPROVAL
        # Severity.LOW
        if relevance == Relevance.FOREIGN:
            return Action.IGNORE
        return Action.DIGEST
