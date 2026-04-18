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
    "1.01", "1.02", "1.03", "1.04",
    "2.01", "2.02", "2.03", "2.04", "2.05", "2.06",
    "3.01", "3.02", "3.03",
    "4.01", "4.02",
    "5.01", "5.02", "5.03", "5.04", "5.05", "5.06", "5.07", "5.08",
    "6.01", "6.02", "6.03", "6.04", "6.05",
    "7.01",
    "8.01",
    "9.01",
)

_ITEM_RE = re.compile(
    r"\bItem\s+(" + "|".join(re.escape(item) for item in _VALID_ITEMS) + r")\b",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def extract_8k_items(html: str) -> list[str]:
    """Return sorted, de-duplicated list of valid 8-K item numbers in the HTML."""
    if not html:
        return []
    stripped = _TAG_RE.sub(" ", html)
    decoded = _html.unescape(stripped).replace("\xa0", " ")
    normalized = _WS_RE.sub(" ", decoded)
    return sorted(set(_ITEM_RE.findall(normalized)))
