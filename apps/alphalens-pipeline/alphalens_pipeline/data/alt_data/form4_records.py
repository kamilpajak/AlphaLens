"""Form 4 XML → Form4Record dataclasses.

Parses SEC EDGAR Form 4 and Form 4/A filings. Form 5 and other documentTypes
are rejected. Derivative transactions are ignored; Form 4 `derivativeTable`
rows cover option exercises (code M) and similar which are not part of the
Layer 2d cluster-buy signal.

The parser is intentionally permissive about missing optional fields
(relationship flags, officerTitle, transactionPricePerShare) — production
filings often omit them — but strict about structural elements
(``ownershipDocument``, ``issuer``, ``reportingOwner``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

_VALID_DOCUMENT_TYPES = {"4", "4/A"}


class Form4ParseError(RuntimeError):
    """Raised when Form 4 XML cannot be parsed or fails schema validation."""


@dataclass(frozen=True)
class Form4Record:
    issuer_cik: str  # 10-digit zero-padded
    ticker: str | None
    accession_number: str
    filing_date: date
    reporting_owner_cik: str  # 10-digit zero-padded
    reporting_owner_name: str
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    is_other: bool
    officer_title: str | None
    transaction_date: date
    transaction_code: str
    transaction_shares: Decimal
    transaction_price_per_share: Decimal | None
    acquired_disposed: str
    is_amendment: bool
    footnotes: tuple[tuple[str, str], ...]


def parse_form4_xml(
    xml_bytes: bytes,
    *,
    accession_number: str,
    filing_date: date,
) -> list[Form4Record]:
    """Parse a Form 4 XML body into Form4Records.

    Returns one record per (reportingOwner × nonDerivativeTransaction). An
    empty ``nonDerivativeTable`` yields ``[]``. ``filing_date`` must come
    from the EDGAR submissions index (not XML) to preserve PIT discipline.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise Form4ParseError(f"malformed XML: {exc}") from exc

    if root.tag != "ownershipDocument":
        raise Form4ParseError(f"root element must be ownershipDocument, got {root.tag!r}")

    document_type = _text(root, "documentType")
    if document_type not in _VALID_DOCUMENT_TYPES:
        raise Form4ParseError(f"unsupported documentType {document_type!r}")
    is_amendment = document_type == "4/A"

    issuer = root.find("issuer")
    if issuer is None:
        raise Form4ParseError("missing <issuer>")
    issuer_cik = _zero_pad_cik(_text(issuer, "issuerCik"))
    ticker = _text(issuer, "issuerTradingSymbol") or None

    owners = root.findall("reportingOwner")
    if not owners:
        raise Form4ParseError("missing <reportingOwner>")

    footnotes = tuple(
        (f.get("id") or "", (f.text or "").strip()) for f in root.findall("footnotes/footnote")
    )

    non_derivative_txs = root.findall("nonDerivativeTable/nonDerivativeTransaction")

    records: list[Form4Record] = []
    for owner in owners:
        owner_meta = _parse_reporting_owner(owner)
        for tx in non_derivative_txs:
            tx_meta = _parse_transaction(tx)
            records.append(
                Form4Record(
                    issuer_cik=issuer_cik,
                    ticker=ticker,
                    accession_number=accession_number,
                    filing_date=filing_date,
                    reporting_owner_cik=owner_meta["cik"],
                    reporting_owner_name=owner_meta["name"],
                    is_director=owner_meta["is_director"],
                    is_officer=owner_meta["is_officer"],
                    is_ten_percent_owner=owner_meta["is_ten_percent_owner"],
                    is_other=owner_meta["is_other"],
                    officer_title=owner_meta["officer_title"],
                    is_amendment=is_amendment,
                    footnotes=footnotes,
                    **tx_meta,
                )
            )
    return records


def _text(elem: ET.Element, path: str) -> str:
    node = elem.find(path)
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _zero_pad_cik(raw: str) -> str:
    digits = raw.lstrip("0") or "0"
    if not digits.isdigit():
        raise Form4ParseError(f"non-numeric CIK: {raw!r}")
    return digits.zfill(10)


def _bool_flag(elem: ET.Element, tag: str) -> bool:
    node = elem.find(tag)
    if node is None or not (node.text or "").strip():
        return False
    return (node.text or "").strip() in {"1", "true", "True"}


def _parse_reporting_owner(owner: ET.Element) -> dict:
    id_node = owner.find("reportingOwnerId")
    rel_node = owner.find("reportingOwnerRelationship")
    if id_node is None or rel_node is None:
        raise Form4ParseError("reportingOwner missing id or relationship")
    return {
        "cik": _zero_pad_cik(_text(id_node, "rptOwnerCik")),
        "name": _text(id_node, "rptOwnerName"),
        "is_director": _bool_flag(rel_node, "isDirector"),
        "is_officer": _bool_flag(rel_node, "isOfficer"),
        "is_ten_percent_owner": _bool_flag(rel_node, "isTenPercentOwner"),
        "is_other": _bool_flag(rel_node, "isOther"),
        "officer_title": _text(rel_node, "officerTitle") or None,
    }


def _parse_transaction(tx: ET.Element) -> dict:
    coding = tx.find("transactionCoding")
    amounts = tx.find("transactionAmounts")
    tx_date_node = tx.find("transactionDate/value")
    if coding is None or amounts is None or tx_date_node is None or not tx_date_node.text:
        raise Form4ParseError("transaction missing coding/amounts/date")

    code = _text(coding, "transactionCode")
    shares = _decimal(_text_value(amounts, "transactionShares"), field="transactionShares")
    price_str = _text_value(amounts, "transactionPricePerShare")
    price = _decimal(price_str, field="transactionPricePerShare", optional=True)
    acq_disp = _text_value(amounts, "transactionAcquiredDisposedCode") or "A"

    return {
        "transaction_date": _parse_iso_date(tx_date_node.text),
        "transaction_code": code,
        "transaction_shares": shares,
        "transaction_price_per_share": price,
        "acquired_disposed": acq_disp,
    }


def _parse_iso_date(raw: str) -> date:
    """Parse an ISO date tolerantly.

    SEC Form 4 usually emits plain ``YYYY-MM-DD`` but some filings append a
    timezone offset (e.g. ``2026-04-09-05:00``) or a time component. Take
    the first 10 chars and parse that — anything after is ignored. Raises
    :class:`Form4ParseError` on malformed input so the scorer's except
    clause treats it as a skippable filing rather than an uncaught crash.
    """
    text = (raw or "").strip()
    if len(text) < 10:
        raise Form4ParseError(f"transaction date too short: {text!r}")
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise Form4ParseError(f"invalid transaction date: {text!r}") from exc


def _text_value(elem: ET.Element, tag: str) -> str:
    """Read ``<tag><value>...</value></tag>`` returning '' if absent."""
    node = elem.find(f"{tag}/value")
    return node.text.strip() if node is not None and node.text else ""


def _decimal(raw: str, *, field: str, optional: bool = False) -> Decimal | None:
    if not raw:
        if optional:
            return None
        raise Form4ParseError(f"missing required {field}")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise Form4ParseError(f"invalid {field}: {raw!r}") from exc
