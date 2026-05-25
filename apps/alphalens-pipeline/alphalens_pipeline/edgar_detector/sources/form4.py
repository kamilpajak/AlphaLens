from __future__ import annotations

import logging
from xml.etree.ElementTree import Element, ParseError

from defusedxml.ElementTree import fromstring as _defused_fromstring

logger = logging.getLogger(__name__)

BUY_CODES = {"P"}  # Open-market purchase
SELL_CODES = {"S"}  # Open-market sale
EXERCISE_CODES = {"M", "F"}  # M = option exercise, F = tax withholding on exercise


def parse_form4_xml(xml_text: str) -> dict:
    """Parse Form 4 ownership XML.

    Priority: real open-market BUY/SELL dominates. If the filing has no P/S
    transactions but contains M/F (option exercise + tax withholding), report
    EXERCISE so the digest entry is informative instead of mute.

    Returns {} on malformed input or missing transactions.
    """
    try:
        root = _defused_fromstring(xml_text)
    except ParseError as exc:
        logger.warning("Malformed Form 4 XML: %s", exc)
        return {}

    transactions = list(root.iter("nonDerivativeTransaction"))
    if not transactions:
        return {}

    buy_shares = buy_value = 0.0
    sell_shares = sell_value = 0.0
    exercise_shares = exercise_value = 0.0

    for tx in transactions:
        code = _extract_transaction_code(tx)
        shares = _extract_value_float(tx, "transactionShares")
        price = _extract_value_float(tx, "transactionPricePerShare")
        if shares is None:
            continue
        value = shares * (price or 0.0)
        if code in BUY_CODES:
            buy_shares += shares
            buy_value += value
        elif code in SELL_CODES:
            sell_shares += shares
            sell_value += value
        elif code in EXERCISE_CODES:
            exercise_shares += shares
            exercise_value += value

    if buy_value > 0 or sell_value > 0:
        if buy_value >= sell_value:
            return {
                "insider_action": "BUY",
                "total_shares": buy_shares,
                "transaction_value_usd": buy_value,
            }
        return {
            "insider_action": "SELL",
            "total_shares": sell_shares,
            "transaction_value_usd": sell_value,
        }

    if exercise_shares > 0:
        return {
            "insider_action": "EXERCISE",
            "total_shares": exercise_shares,
            "transaction_value_usd": exercise_value,
        }

    return {}


def _extract_transaction_code(tx: Element) -> str | None:
    coding = tx.find("transactionCoding")
    if coding is None:
        return None
    code_el = coding.find("transactionCode")
    return code_el.text.strip() if code_el is not None and code_el.text else None


def _extract_value_float(tx: Element, wrapper_tag: str) -> float | None:
    amounts = tx.find("transactionAmounts")
    source = amounts if amounts is not None else tx
    wrapper = source.find(wrapper_tag)
    if wrapper is None:
        return None
    value_el = wrapper.find("value")
    if value_el is None or not value_el.text:
        return None
    try:
        return float(value_el.text.strip())
    except ValueError:
        return None
