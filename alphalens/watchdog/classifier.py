from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .portfolio import PortfolioState, Relevance
from .types import Event, FormType

# 8-K items classified by empirical CAR magnitude (CAR citations in tests).
# Item 5.02 is subsectioned per SEC Form 8-K General Instructions:
#   5.02(b) = termination of principal officer (CEO/CFO/COO/PAO) — Salzman -1.5 to -2%
#   5.02(c) = appointment of principal officer — succession signal
#   5.02(a) = director resignation/removal — Salzman ~-0.3%
#   5.02(d) = director election (non-annual) — routine governance
#   5.02(e)/(f) = compensation / salary — procedural
# Extraction captures the subsection letter, so classifier routes precisely.
HIGH_IMPACT_8K_ITEMS = {
    "2.04",  # Triggering events for material financial obligation (Aharony -1.5 to -3%)
    "4.02",  # Non-reliance on prior financial statements (Beneish -1.2 to -1.5%)
    "5.02(b)",  # Principal officer termination
    "5.02(c)",  # Principal officer appointment
    "5.03",  # Amendments to bylaws / fiscal year change
}
MEDIUM_IMPACT_8K_ITEMS = {
    "1.01",  # Material definitive agreement (+0.3%, weak)
    "1.02",  # Termination of material agreement (-0.5%, weak)
    "2.01",  # Completion of acquisition
    "3.02",  # Unregistered shares
    "5.01",  # Change in control
    "5.02",  # Bare 5.02 without subsection (rare; primary HTML parse miss)
    "5.02(a)",  # Director resignation/removal
    "5.02(d)",  # Director election (non-annual)
}
LOW_IMPACT_8K_ITEMS = {
    "2.02",  # Earnings release (priced in via press release)
    "5.02(e)",  # Compensatory arrangements
    "5.02(f)",  # Salary/bonus determination
    "7.01",  # Regulation FD disclosure
    "8.01",  # Other events
    "9.01",  # Financial statements and exhibits
}

LARGE_INSIDER_BUY_USD = 500_000
SMALL_INSIDER_BUY_USD = 50_000
SMALL_SELL_SUPPRESS_USD = 100_000


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

        if self._should_suppress(event):
            action = Action.IGNORE
        else:
            action = self._action_for(severity, relevance)
            action = self._apply_form4_watchlist_override(action, event, relevance, severity)

        return ClassifiedEvent(event=event, severity=severity, relevance=relevance, action=action)

    def _severity_for(self, event: Event) -> Severity:
        form = event.form_type
        raw = event.raw_data

        if form == FormType.FORM_13D:
            return Severity.HIGH
        if form == FormType.FORM_13D_A:
            return Severity.MEDIUM
        if form == FormType.FORM_13G:
            return Severity.MEDIUM  # ~65% passive rebalancing — not HIGH
        if form == FormType.FORM_13G_A:
            return Severity.LOW

        if form == FormType.FORM_8K:
            items = set(raw.get("items") or [])
            if not items:
                return Severity.LOW  # title-only 8-K, usually routine
            if items & HIGH_IMPACT_8K_ITEMS:
                return Severity.HIGH
            if items & MEDIUM_IMPACT_8K_ITEMS:
                return Severity.MEDIUM
            if items & LOW_IMPACT_8K_ITEMS:
                return Severity.LOW
            return Severity.MEDIUM  # unknown item — be slightly conservative

        if form == FormType.FORM_4:
            action = raw.get("insider_action")
            value = raw.get("transaction_value_usd", 0) or 0
            if action == "BUY":
                if value >= LARGE_INSIDER_BUY_USD:
                    return Severity.HIGH
                if value > 0 and value < SMALL_INSIDER_BUY_USD:
                    return Severity.LOW
                return Severity.MEDIUM
            if action == "SELL":
                # SELLs are noise per Cohen/Malloy/Pomorski
                return Severity.LOW
            # Unknown action (parse failed / details not fetched): conservative LOW
            return Severity.LOW

        return Severity.LOW

    def _should_suppress(self, event: Event) -> bool:
        """Filter pure noise entirely — small SELLs dominated by diversification/tax."""
        if event.form_type == FormType.FORM_4:
            action = event.raw_data.get("insider_action")
            value = event.raw_data.get("transaction_value_usd") or 0
            if action == "SELL" and 0 < value < SMALL_SELL_SUPPRESS_USD:
                return True
        return False

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

    @staticmethod
    def _apply_form4_watchlist_override(
        action: Action, event: Event, relevance: Relevance, severity: Severity
    ) -> Action:
        """Form 4 MEDIUM on watchlist → DIGEST (held stays APPROVAL — capital at risk)."""
        if (
            action == Action.APPROVAL
            and event.form_type == FormType.FORM_4
            and relevance == Relevance.WATCHLIST
            and severity == Severity.MEDIUM
        ):
            return Action.DIGEST
        return action
