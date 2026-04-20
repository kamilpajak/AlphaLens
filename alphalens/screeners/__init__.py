"""Screener pipelines — each module implements `Pipeline.to_candidates(df)`
so the CLI can funnel results through the shared `CandidateQueue`.

Active:
    themed       — Layer 2b curated themed YAML universe (pluggable scorer)
    prescreener  — Layer 2a S&P 500 composite (unvalidated, manual)
    lean         — Layer 2c archived (failed 5-year validation)
"""
