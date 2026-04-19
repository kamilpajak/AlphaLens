"""Lean algorithm project — the files here are mounted into the Lean Docker container.

Keep `features.py` and `scorer.py` dependency-free (pandas/numpy only) so Lean's
interpreter can import them AND host-side unit tests can exercise them directly.
`main.py` is the only file that pulls in Lean's `AlgorithmImports`.
"""
