"""Data infrastructure — clients, parsers, and PIT stores.

Subpackages:
- alt_data       — SEC EDGAR + Form 4 + Russell + ticker/CIK + yfinance cache (RESEARCH_ONLY)
- fundamentals   — Alpha Vantage + EDGAR companyfacts + runtime cache + gate (RESEARCH_ONLY)
- macro          — FRED client + macro signals (RESEARCH_ONLY)
- store          — single source of truth for as-of-t reads (PIT historicals; ACTIVE)

Plus the flat module:
- factors        — Fama-French / momentum / industry CSV loaders (Ken French data library)

`data/` is a namespace package; lifecycle status lives on each subpackage.
"""
