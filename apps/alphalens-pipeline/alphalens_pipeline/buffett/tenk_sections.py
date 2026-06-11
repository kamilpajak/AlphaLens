"""Pure 10-K section splitter — carve plain text into Buffett-relevant items (#506).

:func:`fetch_10k_text` (``thematic.verification.tenk_grep``) returns the FULL
plaintext of a 10-K with HTML stripped and whitespace collapsed; it does NOT
split the document into its numbered items. This module supplies that split for
the three sections the qualitative Buffett layer reasons over:

* **Item 1 — Business** (what the company does → understandability F0)
* **Item 1A — Risk Factors** (durability / threats → moat trend F3)
* **Item 7 — Management's Discussion and Analysis** (management's own narrative →
  candor F4)

Heuristic (documented so a reviewer can judge its limits):

* Each item is found by a case-insensitive regex on its heading token
  (``item 1.``, ``item 1a.``, ``item 7.``) anchored to a word boundary. The
  heading may be followed by ``.`` and arbitrary whitespace.
* A section runs from the END of its own heading up to the START of the NEXT
  item heading of ANY number (``item <n>[a]``) — so Item 1 stops at Item 1A,
  Item 1A stops at Item 1B, Item 7 stops at Item 7A / Item 8, etc. The final
  section runs to end-of-document.
* A heading that never appears yields ``None`` for that section (the caller
  treats ``None`` as "section unavailable", never as empty content).
* Each section is truncated to ``max_chars_per_section`` characters to bound the
  LLM prompt size (full 10-Ks run to hundreds of KB; the qualitative classifier
  only needs the opening narrative of each item). Truncation is a hard
  character slice — it may cut mid-sentence, which is acceptable for a
  classification prompt.

This module is PURE: no SEC calls, no file I/O, no network. It operates on a
string the caller already fetched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Default per-section character cap. Item 1 / 1A / 7 narratives are long; the
# qualitative classifier only needs the leading description of each, and a
# generous cap keeps the DeepSeek Pro prompt well inside the context window.
_DEFAULT_MAX_CHARS_PER_SECTION = 30000

# Matches any 10-K item heading: "item", whitespace, a number, an optional
# letter suffix (1A, 7A), then a "." or whitespace boundary. Used both to find
# the three target headings and to find the NEXT heading that bounds a section.
_ANY_ITEM_HEADING = re.compile(r"\bitem\s+\d+[a-z]?\b\.?", re.IGNORECASE)

# Item 8 (Financial Statements) often does NOT carry its statements under the
# heading: many filers put a one-line "filed under Item 15" pointer there and
# place the real statements in a back-of-document block. When the inline Item 8
# body is shorter than this floor we treat it as such a stub and fall back to
# scanning for a financial-statements anchor (below).
_ITEM_8_STUB_FLOOR = 600

# Unambiguous lead-in phrases for a financial-statements block. Used ONLY as the
# Item 8 stub fallback. Deliberately multi-word — a bare "F-1" would match visa
# / form references, so it is excluded.
_ITEM_8_ANCHORS = (
    "index to financial statements",
    "consolidated balance sheet",
    "consolidated statements of operations",
    "consolidated statement of operations",
)

# Item 8 is mostly numeric tables + footnotes; the qualitative classifier only
# needs the lead narrative + statement headers, so it carries a tighter cap than
# the narrative sections (Item 1 / 1A / 7) to bound LLM token cost (#505).
_DEFAULT_MAX_CHARS_ITEM_8 = 10000


@dataclass(frozen=True)
class TenKSections:
    """The three Buffett-relevant 10-K sections, each ``None`` when not found.

    ``item_1`` — Business; ``item_1a`` — Risk Factors; ``item_7`` — MD&A. A
    ``None`` value means the heading was absent from the supplied text (not that
    the section was empty).
    """

    item_1: str | None
    item_1a: str | None
    item_7: str | None
    # Item 8 — Financial Statements and Supplementary Data (#505). Defaults to
    # ``None`` so existing constructors (qualitative tests, the all-None early
    # return) keep working unchanged when a shared type gains a field.
    item_8: str | None = None


def _extract_section(text: str, *, item_token: str, max_chars: int) -> str | None:
    """Return the longest body following an ``item_token`` heading.

    ``item_token`` is the bare number+suffix (e.g. ``"1"``, ``"1a"``, ``"7"``).
    The heading is matched case-insensitively with an optional trailing ``.``.

    Real 10-Ks list every item heading TWICE: once in the table of contents
    (each entry tiny, bounded by the next TOC line) and once at the real section
    body (long, bounded by the next real heading). Taking the FIRST match grabs
    the TOC entry ("Business 3"). So we scan ALL occurrences and keep the one
    whose body — text up to the next item heading of any number — is LONGEST;
    the TOC fragments lose to the real section every time. ``None`` when the
    heading never appears. The winning slice is stripped and truncated to
    ``max_chars``.
    """
    heading = re.compile(rf"\bitem\s+{item_token}\b\.?", re.IGNORECASE)
    best_body: str | None = None
    for match in heading.finditer(text):
        start = match.end()
        next_match = _ANY_ITEM_HEADING.search(text, start)
        end = next_match.start() if next_match is not None else len(text)
        body = text[start:end].strip()
        if best_body is None or len(body) > len(best_body):
            best_body = body
    if best_body is None:
        return None
    return best_body[:max_chars]


def _extract_item_8(text: str, *, max_chars: int) -> str | None:
    """Return Item 8 (Financial Statements) with a stub-aware anchor fallback.

    First try the normal heading extraction (Item 8 → next item heading). When
    the inline body is substantive (``>= _ITEM_8_STUB_FLOOR``) it is the real
    statements block and is returned. When it is shorter — the common
    "incorporated by reference / filed under Item 15" pointer — scan for the
    FIRST financial-statements anchor (``_ITEM_8_ANCHORS``) and return from that
    anchor up to the next item heading (or end). ``None`` only when there is
    neither an Item 8 heading nor an anchor. The result is truncated to
    ``max_chars``.
    """
    primary = _extract_section(text, item_token="8", max_chars=max_chars)
    if primary is not None and len(primary) >= _ITEM_8_STUB_FLOOR:
        return primary
    # Stub or absent: look for a back-of-document statements block.
    lowered = text.lower()
    anchor_pos: int | None = None
    for anchor in _ITEM_8_ANCHORS:
        pos = lowered.find(anchor)
        if pos != -1 and (anchor_pos is None or pos < anchor_pos):
            anchor_pos = pos
    if anchor_pos is None:
        # No statements block found — return whatever the inline body was
        # (a legitimately terse Item 8) or ``None``.
        return primary
    next_match = _ANY_ITEM_HEADING.search(text, anchor_pos + 1)
    end = next_match.start() if next_match is not None else len(text)
    body = text[anchor_pos:end].strip()
    if not body:
        return primary
    return body[:max_chars]


def split_10k_sections(
    text: str,
    *,
    max_chars_per_section: int = _DEFAULT_MAX_CHARS_PER_SECTION,
    max_chars_item_8: int = _DEFAULT_MAX_CHARS_ITEM_8,
) -> TenKSections:
    """Split a 10-K plaintext into Item 1 / 1A / 7, each truncated + ``None``-safe.

    Pure function — no I/O. See the module docstring for the heading heuristic
    and truncation contract. Never raises on junk / empty input: a string with
    no item headings yields a :class:`TenKSections` of all ``None``.
    """
    if not text:
        return TenKSections(item_1=None, item_1a=None, item_7=None, item_8=None)
    return TenKSections(
        item_1=_extract_section(text, item_token="1", max_chars=max_chars_per_section),
        item_1a=_extract_section(text, item_token="1a", max_chars=max_chars_per_section),
        item_7=_extract_section(text, item_token="7", max_chars=max_chars_per_section),
        item_8=_extract_item_8(text, max_chars=max_chars_item_8),
    )


__all__ = ["TenKSections", "split_10k_sections"]
