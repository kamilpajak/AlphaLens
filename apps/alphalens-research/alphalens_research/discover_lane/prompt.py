from __future__ import annotations

_TEMPLATE = """\
As of {date}, identify the most significant market-moving news stories affecting \
US-listed equities. Focus on concrete events (deals, regulation, product, supply, \
earnings surprises), not generic commentary.

For each story, name the specific US-listed beneficiary companies. Output ONLY a \
JSON object with this exact shape and nothing else:

{{
  "stories": [
    {{
      "event_title": "<short headline of the triggering event>",
      "event_url": "<one representative source URL>",
      "beneficiaries": [
        {{"ticker": "<US-listed ticker>", "company": "<company name>", "reason": "<one sentence why this company benefits>"}}
      ]
    }}
  ]
}}

Rules: only US-listed equities with a real ticker; give your best-known ticker \
symbol; do not include any price, size, or numeric estimates; reason must be a \
single sentence.
"""


def build_discover_prompt(date_iso: str) -> str:
    return _TEMPLATE.format(date=date_iso)
