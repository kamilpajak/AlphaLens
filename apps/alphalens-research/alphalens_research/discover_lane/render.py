from __future__ import annotations

from html import escape

from .models import BriefCandidate, DateBlock, DiscoverCandidate

_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:2rem;background:#0f1115;color:#e6e6e6}
h1{font-size:1.3rem}h2{font-size:1.05rem;border-bottom:1px solid #333;padding-bottom:.3rem;margin-top:2rem}
.cols{display:flex;gap:1.5rem;align-items:flex-start}.col{flex:1}
.col h3{font-size:.85rem;text-transform:uppercase;letter-spacing:.05em;color:#9aa}
.card{background:#1a1d24;border:1px solid #2a2e38;border-radius:8px;padding:.7rem .85rem;margin:.5rem 0}
.card.unresolved{opacity:.5}
.tk{font-weight:700}.mcap{color:#7fd1b9}.src{color:#8a8fa3;font-size:.8rem}
.badge{display:inline-block;font-size:.7rem;padding:.05rem .4rem;border-radius:4px;margin-left:.3rem;background:#2a2e38}
.bar{color:#9aa;font-size:.85rem;margin:.3rem 0 .6rem}
"""


def _fmt_mcap(mcap: float | None) -> str:
    if mcap is None:
        return "—"
    if mcap >= 1e12:
        return f"${mcap / 1e12:.1f}T"
    if mcap >= 1e9:
        return f"${mcap / 1e9:.1f}B"
    return f"${mcap / 1e6:.0f}M"


def _discover_card(c: DiscoverCandidate, shared: set[str]) -> str:
    badges = ""
    if c.in_pipeline_universe:
        badges += '<span class="badge">in-universe</span>'
    if c.ticker in shared:
        badges += '<span class="badge">also-in-brief</span>'
    if not c.resolved:
        badges += '<span class="badge">unresolved</span>'
    src = (
        f'<a class="src" href="{escape(c.source_event_url)}">{escape(c.source_event_title)}</a>'
        if c.source_event_url
        else f'<span class="src">{escape(c.source_event_title)}</span>'
    )
    cls = "card unresolved" if not c.resolved else "card"
    return (
        f'<div class="{cls}"><span class="tk">{escape(c.ticker)}</span> '
        f'{escape(c.company)} <span class="mcap">{_fmt_mcap(c.mcap)}</span>{badges}<br>'
        f'<span class="src">{escape(c.theme)}</span><br>{escape(c.rationale)}<br>'
        f'{src} · <span class="src">{escape(str(c.citation_count))} sources</span></div>'
    )


def _brief_card(c: BriefCandidate, shared: set[str]) -> str:
    badge = '<span class="badge">also-in-perplexity</span>' if c.ticker in shared else ""
    return (
        f'<div class="card"><span class="tk">{escape(c.ticker)}</span> '
        f'{escape(c.company)} <span class="mcap">{_fmt_mcap(c.mcap)}</span>{badge}<br>'
        f'<span class="src">{escape(c.theme)}</span><br>'
        f'<span class="src">{escape(c.source_event_title)}</span></div>'
    )


def _block_html(block: DateBlock) -> str:
    shared = set(block.comparison.shared)
    bar = (
        f"Perplexity {len(block.discover)} · brief {len(block.brief)} · "
        f"shared {len(block.comparison.shared)} · "
        f"median mcap P={_fmt_mcap(block.comparison.discover_median_mcap)} "
        f"vs B={_fmt_mcap(block.comparison.brief_median_mcap)}"
    )
    disc = "".join(_discover_card(c, shared) for c in block.discover) or '<div class="card">—</div>'
    brf = "".join(_brief_card(c, shared) for c in block.brief) or '<div class="card">—</div>'
    return (
        f"<h2>{escape(block.date)}</h2><div class='bar'>{escape(bar)}</div>"
        f"<div class='cols'><div class='col'><h3>Perplexity-Discover</h3>{disc}</div>"
        f"<div class='col'><h3>Brief</h3>{brf}</div></div>"
    )


def render_report(blocks: list[DateBlock], generated_stamp: str) -> str:
    body = "".join(_block_html(b) for b in blocks)
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Discover-lane experiment</title><style>{_CSS}</style></head><body>"
        f"<h1>Discover-lane experiment</h1>"
        f"<p class='src'>generated {escape(generated_stamp)}</p>{body}</body></html>"
    )
