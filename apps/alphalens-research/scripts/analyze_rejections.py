"""Analyze Layer 3 rejection reasoning with DeepSeek v4 Flash → scorer improvement leads.

Reads every rejected sample from `docs/research/acceptance_{scorer}_reports/`, sends
the Portfolio Manager's decision text to DeepSeek v4 Flash (via OpenRouter) for
structured categorization, and aggregates across the cohort. Output: per-pick JSONL +
aggregate markdown with dominant rejection themes and suggested scorer pre-filters.

Cost: ~50 Flash calls × ~3k tokens each = ~150k tokens total. Seconds of wall time.

Usage:
  .venv/bin/python scripts/analyze_rejections.py
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
ACCEPT_RATINGS = {"BUY", "OVERWEIGHT"}
MODEL = "deepseek/deepseek-v4-flash"

PROMPT_TEMPLATE = """You are auditing a trading system's rejection of a stock pick. Below is the Portfolio Manager's final verdict text. Classify it.

Output STRICT JSON (no markdown fences, no prose outside JSON):
{{
  "primary_reason": one of ["valuation_extreme","technical_broken","competitive_moat_erosion","dilution_cash_burn","macro_headwind","fundamentals_weak","management_governance","regulatory_legal","execution_risk","momentum_exhausted","other"],
  "secondary_reasons": [same categories as above, 0-3 items],
  "red_flags": [3-6 short phrases identifying specific concerns, each max 60 chars, e.g. "P/S ratio >100", "death cross 50DMA", "negative FCF 2y"],
  "conviction": one of ["low","medium","high","very_high"],
  "suggested_scorer_filter": single sentence (max 120 chars) describing a rule-based pre-filter that would have caught this pick before Layer 3 saw it. Examples: "reject P/S > 50 for pre-profit names", "reject if close < 50DMA by >5%", "reject revenue growth from <$5M base"
}}

Ticker: {ticker}
Date: {date}
Rating: {rating}

Portfolio Manager verdict:
---
{verdict}
---"""


def collect_rejections(scorer: str) -> list[dict]:
    csv_path = REPO / f"docs/research/acceptance_{scorer.replace('-', '_')}.csv"
    reports_dir = REPO / f"docs/research/acceptance_{scorer.replace('-', '_')}_reports"
    df = pd.read_csv(csv_path)
    rejected = df[(df["accepted"] == 0) & df["rating"].notna() & (df["rating"] != "")]
    rows = []
    for _, r in rejected.iterrows():
        sample_dir = reports_dir / f"{r['date']}_{r['ticker']}"
        decision_file = sample_dir / "5_portfolio" / "decision.md"
        if not decision_file.exists():
            print(f"  skip {r['date']}_{r['ticker']} — no decision.md")
            continue
        rows.append(
            {
                "scorer": scorer,
                "date": r["date"],
                "ticker": r["ticker"],
                "regime": r["regime"],
                "rating": r["rating"],
                "fwd_20d": r.get("fwd_20d"),
                "alpha_20d": r.get("alpha_20d"),
                "alpha_120d": r.get("alpha_120d"),
                "verdict_text": decision_file.read_text(),
            }
        )
    return rows


def classify_with_llm(row: dict, client) -> dict:
    prompt = PROMPT_TEMPLATE.format(
        ticker=row["ticker"],
        date=row["date"],
        rating=row["rating"],
        verdict=row["verdict_text"][:8000],  # cap to avoid huge prompts
    )
    config = client.build_config(response_mime_type="application/json")
    resp = client.generate_content(model=MODEL, contents=prompt, config=config)
    text = resp.text.strip()
    # Some models wrap in ```json fences — strip defensively.
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"    WARN json parse failed: {e}\n    raw: {text[:200]}")
        return {"primary_reason": "parse_error", "raw": text[:500]}
    # Empty-content guard: a valid JSON body with no classification (e.g. `{}` or
    # a blank primary_reason) would otherwise fold silently into the "unknown"
    # tally via r.get("primary_reason", "unknown"). Bucket it visibly instead so
    # a degenerate LLM reply is distinguishable from a genuine "unknown" verdict.
    if not (isinstance(parsed, dict) and str(parsed.get("primary_reason", "")).strip()):
        return {"primary_reason": "empty_content", "raw": text[:500]}
    return parsed


def main() -> None:
    from alphalens_pipeline.data.alt_data.openrouter_client import OpenRouterClient

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        # Load from .env if not already exported
        env_path = REPO / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY not set")

    client = OpenRouterClient(api_key=api_key)

    all_rows = []
    for scorer in ("momentum", "early-stage"):
        print(f"\n=== {scorer} ===")
        rejections = collect_rejections(scorer)
        print(f"  {len(rejections)} rejected samples to classify")
        for i, row in enumerate(rejections, 1):
            print(f"  [{i}/{len(rejections)}] {row['date']} {row['ticker']} ({row['regime']})")
            try:
                classification = classify_with_llm(row, client)
            except Exception as exc:
                print(f"    ERROR: {exc}")
                classification = {"primary_reason": "api_error", "error": str(exc)[:200]}
            row_out = {k: v for k, v in row.items() if k != "verdict_text"}
            row_out.update(classification)
            all_rows.append(row_out)
            time.sleep(0.3)  # gentle pacing for the API rate limit

    # Persist
    out_jsonl = REPO / "docs/research/rejection_analysis.jsonl"
    out_jsonl.write_text("\n".join(json.dumps(r, default=str) for r in all_rows))
    print(f"\nJSONL → {out_jsonl}")

    # Aggregate
    md_lines = [
        "# Layer 3 Rejection Analysis — what patterns would a scorer filter catch?",
        "",
        f"Analyzed {len(all_rows)} rejected picks across both scorers "
        f"({sum(1 for r in all_rows if r['scorer'] == 'momentum')} momentum, "
        f"{sum(1 for r in all_rows if r['scorer'] == 'early-stage')} early-stage).",
        "",
        "## Primary rejection reasons",
        "",
        "| Reason | Momentum | Early-stage | Total |",
        "| --- | ---: | ---: | ---: |",
    ]
    reasons = Counter()
    by_scorer = {"momentum": Counter(), "early-stage": Counter()}
    for r in all_rows:
        p = r.get("primary_reason", "unknown")
        reasons[p] += 1
        by_scorer[r["scorer"]][p] += 1
    for reason, total in reasons.most_common():
        md_lines.append(
            f"| {reason} | {by_scorer['momentum'][reason]} | {by_scorer['early-stage'][reason]} | {total} |"
        )

    md_lines += ["", "## Most common red flags (across all rejections)", ""]
    flags = Counter()
    for r in all_rows:
        for f in r.get("red_flags") or []:
            flags[f.strip().lower()[:80]] += 1
    for flag, count in flags.most_common(20):
        md_lines.append(f"- ({count}×) {flag}")

    md_lines += ["", "## Suggested scorer filters (deduped)", ""]
    filters = Counter()
    for r in all_rows:
        f = (r.get("suggested_scorer_filter") or "").strip().lower()
        if f:
            filters[f] += 1
    for flt, count in filters.most_common(15):
        md_lines.append(f"- ({count}×) {flt}")

    md_lines += [
        "",
        "## False negative focus (rejected picks that rallied ≥10% in 20d)",
        "",
        "These are where Layer 3 rejected a winner — scorer improvement is "
        "less valuable here (we want LESS filtering, not more), but the "
        "categorization shows if Layer 3 has systemic blind spots.",
        "",
        "| Date | Ticker | Scorer | Regime | fwd20d | Primary reason | Filter suggested |",
        "| --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for r in sorted(all_rows, key=lambda x: -(x.get("fwd_20d") or -1)):
        if (r.get("fwd_20d") or 0) >= 0.10:
            md_lines.append(
                f"| {r['date']} | {r['ticker']} | {r['scorer']} | {r['regime']} | "
                f"{(r['fwd_20d'] or 0) * 100:+.1f}% | {r.get('primary_reason', '')} | "
                f"{(r.get('suggested_scorer_filter') or '')[:80]} |"
            )

    out_md = REPO / "docs/research/rejection_analysis.md"
    out_md.write_text("\n".join(md_lines))
    print(f"Markdown → {out_md}")


if __name__ == "__main__":
    main()
