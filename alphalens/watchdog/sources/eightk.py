"""8-K item-number extraction.

SEC Form 8-K RSS entries carry only a generic title. The specific item numbers
(e.g. 2.02 earnings release, 5.02 officer change, 2.04 triggering event) live in
the primary filing HTML. This module extracts the whitelisted set of valid 8-K
items from that HTML without building a DOM tree — regex-only extraction is
sufficient for this grammar and avoids the overhead of a full parser on
potentially multi-MB filings.
"""

from __future__ import annotations

import html as _html
import re

# Strict whitelist of SEC 8-K item numbers (per Form 8-K General Instructions).
# Any "Item X.YY" match outside this set is a cross-reference to another schedule
# (e.g. Item 10.1 of Regulation S-K), not a Form 8-K section.
_VALID_ITEMS = (
    "1.01",
    "1.02",
    "1.03",
    "1.04",
    "2.01",
    "2.02",
    "2.03",
    "2.04",
    "2.05",
    "2.06",
    "3.01",
    "3.02",
    "3.03",
    "4.01",
    "4.02",
    "5.01",
    "5.02",
    "5.03",
    "5.04",
    "5.05",
    "5.06",
    "5.07",
    "5.08",
    "6.01",
    "6.02",
    "6.03",
    "6.04",
    "6.05",
    "7.01",
    "8.01",
    "9.01",
)

# Item N.NN optionally followed by a subsection letter in parens, e.g. "5.02(b)".
# Subsection granularity matters for Item 5.02 (Perplexity 2026-04-18): 5.02(b)/(c)
# = principal-officer events → HIGH; 5.02(a)/(d) = director events → MEDIUM;
# 5.02(e)/(f) = compensation → LOW. Captures both pieces so callers can reassemble.
_ITEM_RE = re.compile(
    r"\bItem\s+("
    + "|".join(re.escape(item) for item in _VALID_ITEMS)
    + r")(?!\d)(\s*\(([a-f])\))?",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _normalize_html_text(html: str) -> str:
    """Strip tags, decode entities, collapse whitespace to a single scannable line."""
    stripped = _TAG_RE.sub(" ", html)
    decoded = _html.unescape(stripped).replace("\xa0", " ")
    return _WS_RE.sub(" ", decoded)


def extract_8k_items(html: str) -> list[str]:
    """Return sorted, de-duplicated list of valid 8-K item codes in the HTML.

    Codes are either bare ('2.02') or subsectioned ('5.02(b)'). Letters are
    normalized to lowercase so '(B)' and '(b)' collapse to the same code.
    """
    if not html:
        return []
    normalized = _normalize_html_text(html)
    codes: set[str] = set()
    for match in _ITEM_RE.finditer(normalized):
        item = match.group(1)
        letter = match.group(3)
        codes.add(f"{item}({letter.lower()})" if letter else item)
    return sorted(codes)


# --- Item 5.02 subsection inference ---------------------------------------
# When a filing uses a bare "Item 5.02" heading (no subsection letter in the
# HTML), Perplexity 2026-04-18 flagged that this covers ~60-70% of real
# principal-officer events — if we stop at bare "5.02" the classifier only
# reaches MEDIUM and the auto-trigger goal is defeated. Carve out the text
# under the Item 5.02 heading and keyword-infer the subsection.

_SIGNATURES_RE = re.compile(
    r"\bSIGNATURES?\b|\bPursuant\s+to\s+the\s+requirements\b",
    re.IGNORECASE,
)
_NEXT_ITEM_RE = re.compile(r"\bItem\s+\d+\.\d+\b", re.IGNORECASE)
_ITEM_5_02_HEADING_RE = re.compile(r"\bItem\s+5\.02\b", re.IGNORECASE)

_OFFICER_RE = re.compile(
    r"\b(chief\s+executive\s+officer|chief\s+financial\s+officer|"
    r"chief\s+operating\s+officer|chief\s+accounting\s+officer|"
    r"principal\s+executive\s+officer|principal\s+financial\s+officer|"
    r"principal\s+operating\s+officer|principal\s+accounting\s+officer|"
    r"CEO|CFO|COO|CAO|president)\b",
    re.IGNORECASE,
)
_DEPARTURE_RE = re.compile(
    r"\b(depart|resign|retir|step(ped|s)?\s+down|termin|remov|dismis|"
    r"not\s+(stand|seek|seeking|standing)\s+for\s+re-?election)",
    re.IGNORECASE,
)
_APPOINTMENT_RE = re.compile(
    r"\b(appoint|elect|named\s+as|nominat|hir|promot)",
    re.IGNORECASE,
)
_DIRECTOR_RE = re.compile(r"\bdirector(s)?\b", re.IGNORECASE)


def extract_5_02_section(html: str) -> str:
    """Return the narrative text under the Item 5.02 heading.

    Cuts at the next Item X.YY heading or at a signatures block (whichever
    comes first). Returns empty string when no Item 5.02 heading is present.
    """
    if not html:
        return ""
    text = _normalize_html_text(html)
    heading = _ITEM_5_02_HEADING_RE.search(text)
    if not heading:
        return ""
    start = heading.start()
    tail = text[heading.end() :]
    end_candidates: list[int] = []
    next_item = _NEXT_ITEM_RE.search(tail)
    if next_item:
        end_candidates.append(heading.end() + next_item.start())
    sig = _SIGNATURES_RE.search(tail)
    if sig:
        end_candidates.append(heading.end() + sig.start())
    end = min(end_candidates) if end_candidates else len(text)
    return text[start:end].strip()


def infer_5_02_subsection(section_text: str) -> str | None:
    """Infer the 5.02 subsection code from the section narrative.

    Priority order (most material first): 5.02(b) officer departure,
    5.02(c) officer appointment, 5.02(a) director departure,
    5.02(d) director election. Returns None when the text has no clear
    directional signal — caller keeps the bare '5.02' code (MEDIUM fallback).
    """
    if not section_text:
        return None

    has_officer = bool(_OFFICER_RE.search(section_text))
    has_departure = bool(_DEPARTURE_RE.search(section_text))
    has_appointment = bool(_APPOINTMENT_RE.search(section_text))
    # A director event is only one where the officer keyword is absent — any
    # officer reference outranks plain "director" because the event is then
    # about the officer, even if they also happened to be on the board.
    director_only = bool(_DIRECTOR_RE.search(section_text)) and not has_officer

    if has_officer and has_departure:
        return "5.02(b)"
    if has_officer and has_appointment:
        return "5.02(c)"
    if director_only and has_departure:
        return "5.02(a)"
    if director_only and has_appointment:
        return "5.02(d)"
    return None
