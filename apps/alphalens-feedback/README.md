# alphalens-feedback

Shared, dependency-free feedback-ledger primitives (SQLite `Decision` store +
VIX `regime`) consumed by BOTH the pipeline and the Django API. Pure stdlib by
design so the slim Django image imports the ledger without the heavy pipeline
dependency tree. See the package docstring in `alphalens_feedback/__init__.py`.
