"""Buffett quantitative lens — Mode A observational comparison over the brief.

This package is the **Mode A** (observational / firebreak) Buffett lens: for a
given thematic brief date it scores the brief's candidate tickers on the Buffett
quantitative DELTA — the metrics the daily brief does NOT already carry (owner-
earnings yield, DCF margin of safety, multi-year ROIC / operating-margin trend,
net-buyback proxy, dividend yield) — and prints / writes a comparison table.

It is **additive and unwired**: nothing in the daily thematic-build pipeline,
systemd, Django, or the SPA consumes it. It is a standalone ad-hoc lens the
operator runs via ``alphalens buffett lens <date>``. Mode B (an independent
universe screener that scores the whole market, not just the brief) is out of
scope — see ``docs/research/buffett_thematic_comparison_2026_06_10.md``.
"""

__status__ = "ACTIVE"
