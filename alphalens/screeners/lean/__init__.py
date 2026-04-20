"""Lean-based batch universe screener (Layer 2c).

Runs QuantConnect Lean in Docker daily after US market close, ranks a curated
~500-ticker small/mid-cap universe by momentum/breakout/volume, emits top-N as
Candidates into the unified queue.
"""
