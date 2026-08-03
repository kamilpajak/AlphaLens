#!/usr/bin/env python
"""Retrospectively label the (catalyst event, candidate ticker) ROLE.

Measurement only — nothing here feeds selection, ordering, or the brief. It
answers one question over historical brief rows: **is there a transmission
channel from the catalyst event to this candidate at all?**

Why role and not polarity: the thematic pipeline is a second-order beneficiary
mapper, so a bearish event is often exactly right (a breach is what sells
security software). Filtering on the event's sign would delete that cohort.
What the pipeline has no representation of is the candidate's ROLE relative to
the event — including "no causal channel", which neither a sign nor a
victim/beneficiary binary can express.

Design constraints, both test-enforced in
``tests/test_classify_catalyst_roles.py``:

* **Blind** — the prompt never sees ``layer4_weighted_score``, ``rank_in_day``,
  ``llm_confidence`` or the mapper's ``rationale``. Feeding the pipeline's own
  claim back in would measure self-consistency, not the channel.
* **No silent degradation** — empty / malformed / off-taxonomy responses get
  their own sentinels and never fold into a real role (PR #869 bug class).

The labels are themselves unvalidated LLM output, so the run is gated on
known-answer anchors (``ANCHORS``); a single mismatch fails the gate and the
aggregate is not to be trusted.

Usage::

    python apps/alphalens-research/scripts/classify_catalyst_roles.py \\
        --input  /path/to/role_audit_input.parquet \\
        --cache  /path/to/role_labels.jsonl \\
        --out    /path/to/role_labels.csv
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "deepseek/deepseek-v4-pro"

# deepseek-v4-pro is a REASONING model and its reasoning tokens are charged
# against max_tokens. Measured on a row that failed persistently at 400:
#   max_tokens=400  -> finish_reason='length', 1963 chars of reasoning, EMPTY content
#   max_tokens=2000 -> finish_reason='stop', answered using 75 tokens
# Too small a budget does not fail loudly - it returns an empty answer, and it
# does so on exactly the rows that need the most reasoning, so the loss is
# systematically biased rather than random.
_MAX_OUTPUT_TOKENS = 2000

# Retry backoff, kept as cheap insurance against transient endpoint errors.
# NOTE: the 25%->57% "degradation" first blamed on rate limiting was a
# misdiagnosis - tasks ran strict-first then permissive, and the permissive
# rubric needs more reasoning, so the rising failure rate tracked the framing,
# not elapsed time. The real cause was the output budget above.
_RETRY_BACKOFF_BASE_S = 2.0

# The taxonomy. "unaffected" is the load-bearing member: a victim/beneficiary
# binary would tag a no-channel name "victim" and hand a long-only tool a
# short-side conclusion, which is a second wrong answer.
ROLES: tuple[str, ...] = (
    "subject-adverse",
    "subject-favorable",
    "rival",
    "value-chain",
    "solution-provider",
    "unaffected",
)

_ROLE_GUIDE = """\
- subject-adverse: the candidate IS an entity the event happened to, and the event hurts it.
- subject-favorable: the candidate IS an entity the event happened to, and the event helps it.
- rival: a COMPETITOR of the candidate is the subject of the event, so the candidate's
  competitive position changes.
- value-chain: the candidate is a supplier, customer, distributor or partner of an entity
  the event happened to, so the event reaches it through commercial relationships.
- solution-provider: the candidate sells a product or service whose demand the event
  itself creates or increases (e.g. a security vendor after a breach, a restructuring
  adviser after mass layoffs, a litigation-finance firm after a wave of suits).
- unaffected: NO plausible transmission channel. The event does not reach the candidate's
  revenue, costs, cost of capital, or competitive position. Use this when the only link is
  lexical - the candidate merely operates in a business that shares vocabulary with the
  headline, or the theme keyword matches but the specific event names unrelated parties."""

FRAMINGS: tuple[str, ...] = ("strict", "permissive")

# The solution-provider / unaffected boundary is a judgement call, not a fact. A
# pilot run showed the strict wording alone driving the headline number (Varonis
# on an AI-agent attack story came back "unaffected", confidence 0.95, with the
# model's own reasoning visibly torn). Running both framings turns a point
# estimate into a band and makes the instrument's bias visible rather than
# baking it into the answer. Only rows both framings call "unaffected" should be
# treated as a defensible no-channel count.
_FRAMING_RUBRIC = {
    "strict": """Judge strictly. A shared word between the theme slug and the company's line of
business is NOT a channel. A general sector tailwind is NOT a channel. The event must reach
THIS company specifically - through named commercial relationships, a named competitor, or
demand this particular event creates for this particular company's product. If the event
names other parties and nothing specific connects them to this company, answer "unaffected".""",
    "permissive": """Judge generously. A channel counts even when it operates at the level of
the sector rather than the individual company: if this event plausibly raises demand for the
category of product the company sells, or plausibly shifts the competitive or regulatory
landscape the company operates in, that IS a channel - name it. Reserve "unaffected" for cases
where even a sector-level reading fails, i.e. the company's business has nothing to do with
what the event is about and the only link is a shared word.""",
}

# NOTE: the prompt must never render the pipeline's own verdict fields
# (layer4_weighted_score, rank_in_day, llm_confidence, rationale, gates_passed_str).
# Enforced by tests/test_classify_catalyst_roles.py::TestPromptBlindness.

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "enum": list(ROLES)},
        "channel": {
            "type": "string",
            "description": "One clause naming the causal channel, or 'none' when there is no channel.",
        },
        "direction": {"type": "string", "enum": ["favorable", "adverse", "none"]},
        "confidence": {"type": "number"},
    },
    "required": ["role", "channel", "direction", "confidence"],
}

# Known-answer cases spanning the three roles that decide the question. Chosen
# from the data BEFORE any labelling run, and deliberately unambiguous:
#   SNAP  - the only first-order rows in the corpus; the article is about SNAP's own
#           stock slump, so the candidate is the adverse subject.
#   VRNS/TENB - data-security vendors surfaced on an AI-agent attack story: the
#           textbook solution-provider read the direction-blind scorer exists to catch.
#   RGR   - a firearms maker surfaced on Apple v Epic: purely lexical, no channel.
#   LYFT  - surfaced on an eBay harassment prosecution: purely lexical, no channel.
ANCHORS: tuple[dict[str, str], ...] = (
    {"ticker": "SNAP", "brief_date": "2026-06-19", "expected_role": "subject-adverse"},
    {"ticker": "SNAP", "brief_date": "2026-06-23", "expected_role": "subject-adverse"},
    {"ticker": "VRNS", "brief_date": "2026-07-29", "expected_role": "solution-provider"},
    {"ticker": "TENB", "brief_date": "2026-07-29", "expected_role": "solution-provider"},
    {"ticker": "RGR", "brief_date": "2026-06-30", "expected_role": "unaffected"},
    {"ticker": "LYFT", "brief_date": "2026-08-02", "expected_role": "unaffected"},
)


def _as_list(value: Any) -> list[str]:
    """Normalise a numpy array / list / None / NaN into a list of strings."""
    if value is None:
        return []
    if isinstance(value, float):  # NaN
        return []
    try:
        return [str(v) for v in value]
    except TypeError:
        return []


def build_role_prompt(row: dict, framing: str = "strict") -> str:
    """Render the blind classification prompt for one (event, ticker) pair.

    Carries only what is needed to judge a causal channel: who the candidate
    is, what business it is in, the theme slug that linked them, and the event
    itself (headline, type, sign, named entities, extracted second-order reads).

    ``framing`` selects the strict or permissive rubric - see ``_FRAMING_RUBRIC``.
    """
    if framing not in _FRAMING_RUBRIC:
        raise ValueError(f"unknown framing {framing!r}; expected one of {FRAMINGS}")
    entities = _as_list(row.get("primary_entities"))
    soi = _as_list(row.get("second_order_implications"))
    soi_rendered = "\n".join(f"  - {s}" for s in soi) if soi else "  (none extracted)"

    return f"""You are classifying the relationship between a NEWS EVENT and a PUBLICLY LISTED COMPANY.

A screening tool matched this company to this event through a theme keyword. Your job is to
judge whether a real causal channel exists between the event and the company - not whether
the company is interesting, and not whether the news is good or bad in general.

COMPANY
  Ticker: {row.get("ticker")}
  Name: {row.get("company_name")}
  Sector: {row.get("sector_name")}
  Industry: {row.get("industry_name")}

THEME SLUG THAT LINKED THEM: {row.get("theme")}

EVENT
  Headline: {row.get("source_event_title")}
  Event type: {row.get("catalyst_event_type")}
  Sign for the entity the event is about: {row.get("sentiment")}
  Entities the event is about: {", ".join(entities) if entities else "(none extracted)"}
  Second-order implications extracted from the article:
{soi_rendered}

ROLE OPTIONS
{_ROLE_GUIDE}

{_FRAMING_RUBRIC[framing]}

Also state the direction the event implies FOR THIS COMPANY: "favorable", "adverse", or
"none" when there is no channel.

Reply with JSON only: {{"role": ..., "channel": ..., "direction": ..., "confidence": ...}}
where "channel" is one clause naming the causal path (or "none"), and "confidence" is 0-1."""


def _extract_json(text: str) -> dict | None:
    """Parse a JSON object out of raw model text, tolerating code fences."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    if stripped.startswith("```"):
        stripped = stripped.split("```")[1] if "```" in stripped[3:] else stripped[3:]
        if stripped.lstrip().lower().startswith("json"):
            stripped = stripped.lstrip()[4:]
        stripped = stripped.strip("`\n ")
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(stripped[start : end + 1])
        except (ValueError, TypeError):
            return None
    return parsed if isinstance(parsed, dict) else None


def classify_role(
    row: dict,
    client,
    model: str = DEFAULT_MODEL,
    max_attempts: int = 5,
    framing: str = "strict",
    sleep_fn=None,
) -> dict:
    """Classify one row. Never raises on model misbehaviour - returns a sentinel.

    ``parse_status`` is one of ``ok`` / ``empty_content`` / ``unparseable`` /
    ``invalid_role`` / ``error``. Only ``ok`` carries a role inside ``ROLES``.

    An empty response is retried once: DeepSeek's JSON mode intermittently
    returns no choices (documented in the OpenRouter client), and at ~1-in-3
    on a smoke sample that would silently drop a third of the corpus.
    """
    prompt = build_role_prompt(row, framing=framing)
    config = client.build_config(
        response_mime_type="application/json",
        response_schema=_RESPONSE_SCHEMA,
        temperature=0.0,
        max_output_tokens=_MAX_OUTPUT_TOKENS,
    )

    if sleep_fn is None:
        import time

        sleep_fn = time.sleep

    text = ""
    for attempt in range(max_attempts):
        if attempt:
            # Endpoint rate-limits under load; back off rather than hammer.
            sleep_fn(_RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1)))
        try:
            response = client.generate_content(model=model, contents=prompt, config=config)
            text = getattr(response, "text", "") or ""
        except Exception as exc:
            logger.warning("role classification failed for %s: %s", row.get("ticker"), exc)
            return {
                "role": "ERROR",
                "channel": "",
                "direction": "",
                "confidence": None,
                "parse_status": "error",
            }
        if text.strip():
            break

    if not text.strip():
        return {
            "role": "EMPTY",
            "channel": "",
            "direction": "",
            "confidence": None,
            "parse_status": "empty_content",
        }

    parsed = _extract_json(text)
    if parsed is None:
        return {
            "role": "UNPARSEABLE",
            "channel": "",
            "direction": "",
            "confidence": None,
            "parse_status": "unparseable",
        }

    role = str(parsed.get("role", "")).strip()
    if not role:
        return {
            "role": "EMPTY",
            "channel": "",
            "direction": "",
            "confidence": None,
            "parse_status": "empty_content",
        }
    if role not in ROLES:
        return {
            "role": f"INVALID:{role}",
            "channel": str(parsed.get("channel", "")),
            "direction": "",
            "confidence": None,
            "parse_status": "invalid_role",
        }

    return {
        "role": role,
        "channel": str(parsed.get("channel", "")),
        "direction": str(parsed.get("direction", "")),
        "confidence": parsed.get("confidence"),
        "parse_status": "ok",
    }


def anchor_report(labelled: Iterable[dict], anchors: Sequence[dict]) -> dict:
    """Gate the run on known-answer cases. Any mismatch fails.

    Strict by design and pre-committed: loosening the gate after seeing the
    labels would turn the sanity check into a rubber stamp.
    """
    index = {(str(r.get("ticker")), str(r.get("brief_date"))): r for r in labelled}
    mismatches = []
    for anchor in anchors:
        key = (str(anchor["ticker"]), str(anchor["brief_date"]))
        got = index.get(key, {}).get("role", "MISSING")
        if got != anchor["expected_role"]:
            mismatches.append({**anchor, "got": got})
    return {"passed": not mismatches, "mismatches": mismatches, "n_anchors": len(anchors)}


# ---- runner -------------------------------------------------------------


def _load_cache(path: Path) -> dict[tuple[str, str, str], dict]:
    if not path.exists():
        return {}
    cached: dict[tuple[str, str, str], dict] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("parse_status") != "ok":
            continue  # a failed label is not a result - re-try it on the next run
        key = (str(rec.get("ticker")), str(rec.get("brief_date")), str(rec.get("framing")))
        cached[key] = rec
    return cached


def main(argv: list[str] | None = None) -> int:
    import pandas as pd

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=None, help="classify only the first N rows")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    frame = pd.read_parquet(args.input)
    frame = frame[frame["sentiment"].notna()].copy()
    if args.limit:
        frame = frame.head(args.limit)
    rows = frame.to_dict("records")
    logger.info("rows to classify: %d", len(rows))

    cache = _load_cache(args.cache)
    logger.info("cached labels: %d", len(cache))

    from alphalens_pipeline.data.alt_data.openrouter_client import get_default_openrouter_client

    client = get_default_openrouter_client()
    lock = threading.Lock()
    args.cache.parent.mkdir(parents=True, exist_ok=True)
    handle = args.cache.open("a")

    def work(task: tuple[dict, str]) -> dict:
        row, framing = task
        key = (str(row.get("ticker")), str(row.get("brief_date")), framing)
        if key in cache:
            return cache[key]
        label = classify_role(row, client, model=args.model, framing=framing)
        record = {
            "ticker": row.get("ticker"),
            "brief_date": row.get("brief_date"),
            "framing": framing,
            "theme": row.get("theme"),
            "catalyst_event_type": row.get("catalyst_event_type"),
            "sentiment": row.get("sentiment"),
            "source_event_title": row.get("source_event_title"),
            "layer4_weighted_score": row.get("layer4_weighted_score"),
            "catalyst_strength": row.get("catalyst_strength"),
            **label,
        }
        # Persist before anything downstream can fail - never re-pay for a call.
        with lock:
            handle.write(json.dumps(record, default=str) + "\n")
            handle.flush()
        return record

    tasks = [(row, framing) for framing in FRAMINGS for row in rows]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(work, tasks))
    handle.close()

    out = pd.DataFrame(results)
    out.to_csv(args.out, index=False)
    print(f"\nclassified {len(results)} (row x framing) -> {args.out}")
    print("\nparse_status by framing:")
    print(pd.crosstab(out["framing"], out["parse_status"]).to_string())

    ok = out[out["parse_status"] == "ok"]
    for framing in FRAMINGS:
        sub = ok[ok["framing"] == framing]
        gate = anchor_report(sub.to_dict("records"), ANCHORS)
        print(
            f"\nANCHOR GATE [{framing}]: {'PASS' if gate['passed'] else 'FAIL'} "
            f"({gate['n_anchors']} anchors, {len(sub)} labelled rows)"
        )
        for miss in gate["mismatches"]:
            print(
                f"  MISMATCH {miss['ticker']} {miss['brief_date']}: "
                f"expected {miss['expected_role']}, got {miss['got']}"
            )
        if not sub.empty:
            print(f"  role distribution [{framing}]:")
            print(
                "   "
                + (sub["role"].value_counts(normalize=True) * 100)
                .round(1)
                .to_string()
                .replace("\n", "\n   ")
            )

    # The band: only rows BOTH framings agree on are a defensible count.
    wide = ok.pivot_table(
        index=["ticker", "brief_date"], columns="framing", values="role", aggfunc="first"
    )
    both = (
        wide.dropna(subset=list(FRAMINGS))
        if set(FRAMINGS).issubset(wide.columns)
        else wide.iloc[0:0]
    )
    if not both.empty:
        agree = both["strict"] == both["permissive"]
        print(f"\nrows labelled under BOTH framings: {len(both)}")
        print(f"framing agreement: {100 * agree.mean():.1f}%")
        lower = ((both["strict"] == "unaffected") & (both["permissive"] == "unaffected")).mean()
        upper = (both["strict"] == "unaffected").mean()
        print("\nNO-CHANNEL BAND (share of joined brief rows):")
        print(f"  lower bound (both framings say unaffected): {100 * lower:.1f}%")
        print(f"  upper bound (strict framing alone):         {100 * upper:.1f}%")
        print("\nstrict x permissive:")
        print(pd.crosstab(both["strict"], both["permissive"]).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
