# apps/alphalens-research/scripts/discover_lane_experiment.py
"""One-shot experiment: parallel Perplexity-driven candidate generation.

Renders a side-by-side HTML report (Perplexity-Discover vs the real brief) for the
latest N brief dates. RESEARCH_ONLY. See
docs/research/discover_lane_experiment_design_2026_06_24.md.

Usage:
    PERPLEXITY_API_KEY=... .venv/bin/python \
        apps/alphalens-research/scripts/discover_lane_experiment.py --last 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
from pathlib import Path

import pandas as pd
from alphalens_pipeline.data.alt_data.yfinance_client import get_default_yfinance_client
from alphalens_pipeline.literature_scanner.perplexity_client import PerplexityClient
from alphalens_pipeline.thematic.config.universe import load_input_universe
from alphalens_research.discover_lane.compare import compare_candidates
from alphalens_research.discover_lane.enrich import enrich_candidates
from alphalens_research.discover_lane.models import BriefCandidate, DateBlock
from alphalens_research.discover_lane.parse import parse_discover_response
from alphalens_research.discover_lane.prompt import build_discover_prompt
from alphalens_research.discover_lane.render import render_report

logger = logging.getLogger("discover_lane_experiment")

DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"
DEFAULT_OUT_DIR = Path.home() / ".alphalens" / "discover_lane_experiment"


def _brief_dates(briefs_dir: Path, last: int) -> list[str]:
    dates = sorted(p.stem for p in briefs_dir.glob("*.parquet"))
    return dates[-last:]


def _load_brief(briefs_dir: Path, date_iso: str) -> list[BriefCandidate]:
    df = pd.read_parquet(briefs_dir / f"{date_iso}.parquet")
    out: list[BriefCandidate] = []
    for _, row in df.iterrows():
        mcap = row.get("market_cap")
        out.append(
            BriefCandidate(
                ticker=str(row["ticker"]).upper(),
                company=str(row.get("company_name", "")),
                theme=str(row.get("theme", "")),
                source_event_title=str(row.get("source_event_title", "")),
                mcap=float(mcap) if pd.notna(mcap) else None,
            )
        )
    return out


def _cached_ask(client: PerplexityClient, date_iso: str, cache_dir: Path):
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{date_iso}.json"
    if cache_path.exists():
        raw = json.loads(cache_path.read_text())
        return raw["content"], raw["search_results"]
    d = dt.date.fromisoformat(date_iso)
    after = (d - dt.timedelta(days=7)).strftime("%m/%d/%Y")
    before = d.strftime("%m/%d/%Y")
    result = client.ask_with_citations(
        build_discover_prompt(date_iso),
        search_context_size="high",
        search_after_date_filter=after,
        search_before_date_filter=before,
    )
    cache_path.write_text(
        json.dumps({"content": result.content, "search_results": result.search_results})
    )
    return result.content, result.search_results


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Discover-lane experiment")
    ap.add_argument("--last", type=int, default=3, help="number of latest brief dates")
    ap.add_argument("--briefs-dir", type=Path, default=DEFAULT_BRIEFS_DIR)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = ap.parse_args()

    api_key = os.environ.get("PERPLEXITY_API_KEY")
    if not api_key:
        raise SystemExit("PERPLEXITY_API_KEY is required")

    client = PerplexityClient(api_key=api_key)
    yf = get_default_yfinance_client()
    universe = {t.upper() for t in load_input_universe()}
    cache_dir = args.out_dir / "cache"

    blocks: list[DateBlock] = []
    for date_iso in _brief_dates(args.briefs_dir, args.last):
        brief_path = args.briefs_dir / f"{date_iso}.parquet"
        if not brief_path.exists():
            logger.warning("no brief parquet for %s; skipping", date_iso)
            continue
        logger.info("processing %s", date_iso)
        content, search_results = _cached_ask(client, date_iso, cache_dir)
        discover = enrich_candidates(
            parse_discover_response(content, search_results),
            yf_client=yf,
            universe=universe,
        )
        brief = _load_brief(args.briefs_dir, date_iso)
        blocks.append(
            DateBlock(
                date=date_iso,
                discover=discover,
                brief=brief,
                comparison=compare_candidates(discover, brief),
            )
        )

    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"report_{stamp.replace(':', '').replace('-', '')}.html"
    out_path.write_text(render_report(blocks, generated_stamp=stamp))
    logger.info("wrote %s (%d dates)", out_path, len(blocks))


if __name__ == "__main__":
    main()
