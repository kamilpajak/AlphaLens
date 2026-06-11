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


def _extract_section(text: str, *, item_token: str, max_chars: int) -> str | None:
    """Return the text after ``item_token``'s heading up to the next item heading.

    ``item_token`` is the bare number+suffix (e.g. ``"1"``, ``"1a"``, ``"7"``).
    The heading is matched case-insensitively with an optional trailing ``.``.
    Returns ``None`` when the heading is not present. The returned slice is
    stripped of surrounding whitespace and truncated to ``max_chars``.
    """
    heading = re.compile(rf"\bitem\s+{item_token}\b\.?", re.IGNORECASE)
    match = heading.search(text)
    if match is None:
        return None
    start = match.end()
    # Find the next item heading AFTER this one to bound the section. Skip any
    # heading whose start is at/inside our own match (defensive — search starts
    # past match.end() already, but keep the intent explicit).
    next_match = _ANY_ITEM_HEADING.search(text, start)
    end = next_match.start() if next_match is not None else len(text)
    body = text[start:end].strip()
    return body[:max_chars]


def split_10k_sections(
    text: str,
    *,
    max_chars_per_section: int = _DEFAULT_MAX_CHARS_PER_SECTION,
) -> TenKSections:
    """Split a 10-K plaintext into Item 1 / 1A / 7, each truncated + ``None``-safe.

    Pure function — no I/O. See the module docstring for the heading heuristic
    and truncation contract. Never raises on junk / empty input: a string with
    no item headings yields a :class:`TenKSections` of all ``None``.
    """
    if not text:
        return TenKSections(item_1=None, item_1a=None, item_7=None)
    return TenKSections(
        item_1=_extract_section(text, item_token="1", max_chars=max_chars_per_section),
        item_1a=_extract_section(text, item_token="1a", max_chars=max_chars_per_section),
        item_7=_extract_section(text, item_token="7", max_chars=max_chars_per_section),
    )


__all__ = ["TenKSections", "split_10k_sections"]
