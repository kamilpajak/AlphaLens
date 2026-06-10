# Buffett score lens (Mode A) — thematic comparison design memo

**Status: DRAFT**
**Date: 2026-06-10**
**Ticket: #511 (epic #500 — Buffett quantitative lens)**
**Branch: `feature/buffett-pr8-comparison`**
**Scope: a standalone, additive, unwired CLI + pure assembler that scores a daily thematic brief's CANDIDATE tickers on the Buffett quantitative DELTA and prints / writes a comparison table.**

---

## 1. What Mode A is

Mode A is the **observational / firebreak** Buffett lens. For a given brief date
it loads the daily thematic brief's candidates and, per candidate, computes the
Buffett quantitative metrics the brief does **not** already carry, then prints an
aligned comparison table (optionally writing a parquet).

It is **additive and unwired**:

- it does NOT run in the daily thematic-build pipeline,
- it has no systemd unit, no Django model / endpoint, no SPA surface,
- it changes no candidate ranking and feeds back into no model.

It is a tool the operator runs ad hoc: `alphalens buffett lens <date>`. The lens
is a second pair of eyes on the same candidates — exactly the augmentation,
not-replacement framing of the whole thematic tool (operator cherry-picks → group
discusses → each member decides).

**Mode B is explicitly out of scope.** Mode B would be an independent universe
screener that scores the whole market (not just the brief's candidates) on Buffett
criteria and surfaces names the thematic pipeline never proposed. That is a
separate, larger track (its own proposal-stage universe, its own PIT discipline,
its own multiple-testing accounting). This memo is Mode A only.

## 2. Why only the DELTA over the brief

The brief row already exposes, per candidate: `roic_pct`, `fcff_yield_pct`,
`valuation_pe` / `valuation_ps` / `valuation_ev_ebitda`, and `market_cap`.
Re-printing those would be noise. The Buffett lens ADDS what the brief lacks:

| Lens field | Source | Brief already has it? |
|---|---|---|
| owner-earnings yield | `owner_earnings_as_of` (latest) / market cap | no |
| DCF margin of safety | `discount_owner_earnings` + net cash bridge | no |
| multi-year ROIC trend (latest + 3y avg) | `annual_series_as_of` (per-year inline) | only the single TTM `roic_pct` |
| multi-year operating-margin trend | `annual_series_as_of` (per-year) | no |
| net-buyback proxy | `capital_allocation_as_of` (latest) | no |
| dividend yield | yfinance `dividends` (trailing 365d) / price | no |

Each metric reuses an existing, already-tested building block (#501 annual
series, #502 owner earnings, #503 DCF, #504 buyback + dividends, the magic-formula
`compute_roic`); the lens is the assembler that arranges them per candidate.

## 3. Fixed DCF assumptions — and why they are screening proxies

The per-share intrinsic value is a deliberately simple, conservative
capitalisation, exposed as overridable module constants so the assumptions are
visible (that visibility is the whole point of the lens):

- **Base = latest non-None owner earnings** (`owner_earnings_as_of`), NOT
  `ocf − capex`. Owner earnings (net income + D&A − maintenance capex − ΔWC) is
  the Buffett cash figure.
- **No growth** (`DEFAULT_GROWTH = 0.0`, `DEFAULT_TERMINAL_GROWTH = 0.0`) — do not
  pay for growth you have to assume.
- **Fixed 10% hurdle** (`DEFAULT_HURDLE_RATE = 0.10`), NOT a per-name WACC — one
  rate across the basket keeps the lens a *comparison screen*, not a precise
  per-company valuation.
- 10-year explicit horizon then a Gordon terminal value.
- Enterprise value → equity via `net_cash = cash − (long_term_debt +
  short_term_debt)` from the latest annual statement; per share = equity / shares;
  margin of safety = `1 − price/intrinsic` (positive = below intrinsic).

Why these are proxies, not a valuation:

- **Owner earnings is a LEVERED proxy** — it starts from net income (post-interest),
  so adding net cash on top double-counts the capital structure for leveraged
  firms (the #503 levered-proxy caveat carries straight over). The error is
  second-order for low-leverage issuers and material for high-leverage ones; do
  NOT read the per-share figure as theoretically clean.
- A flat 10% hurdle ignores each name's actual cost of capital.
- No-growth capitalisation systematically *under*-values genuine compounders —
  intentionally, as a margin-of-safety bias, not an estimate of fair value.

The margin-of-safety computation is guarded so it can NEVER raise: it returns
`None` unless both a positive per-share intrinsic value and a price are present
(the underlying `margin_of_safety` raises on a non-positive intrinsic value).

## 4. Honest data coverage — the "too hard" pile

A thematic basket is full of small, recent, pre-profit names. Many will have no
multi-year history, no owner earnings, no dividends. Rather than fabricate zeros,
every field fails soft to `None`, and the panel reports `data_coverage` — the
fraction of the six Buffett-delta fields (owner-earnings yield, ROIC, margin of
safety, op margin, net buyback, dividend yield) that actually resolved (0..1).

A low coverage row is itself the signal: it is the Buffett "too hard" pile — a
business the quantitative lens cannot characterise, which is exactly the kind of
name to treat with caution. Never fabricate; surface the gap.

## 5. Structure

- `alphalens_pipeline/buffett/__init__.py` — namespace (`__status__ = "ACTIVE"`).
- `alphalens_pipeline/buffett/comparison.py` — `BuffettPanel` dataclass +
  `compute_panel(ticker, theme, asof, *, store, mcap_fn, dividends_fn)` (pure-ish,
  every external call wrapped to fail soft) + `build_comparison(brief_date, ...)`
  (load_brief → compute_panel per candidate, brief order preserved).
- `alphalens_cli/commands/buffett.py` — `alphalens buffett lens <DATE>
  [--briefs-dir] [--out]`, lazy-importing the store + assembler inside the command
  body (lazy-CLI-import convention). Registered in `alphalens_cli/main.py`.

The package lives on the **pipeline** side (it is a data-consuming assembler over
the `alphalens_pipeline.data` building blocks) and imports nothing from
`alphalens_research` — the workspace DAG stays intact.

## 6. Non-goals / deferred

- **Mode B independent universe screener** — separate track (see §1).
- **PIT price history** — the lens uses the store's live snapshot price
  (`with_prices=True`), acceptable for an ad-hoc `asof ≈ today` lens; a historical
  replay would need a PIT price loader.
- **Per-name WACC** — out of scope; the flat hurdle is the deliberate screening
  choice.
- **Wiring into the brief / dashboard** — explicitly NOT done; this is Mode A
  observational only.
