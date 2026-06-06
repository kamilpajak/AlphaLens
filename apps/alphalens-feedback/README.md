# alphalens-feedback

Shared, dependency-free feedback primitives consumed by BOTH the pipeline and
the Django API: the VIX `regime` helper and the `migrate` teardown for the
removed `decisions` table. Pure stdlib by design so the slim Django image
imports `regime` without the heavy pipeline dependency tree. The SQLite
`Decision` store was removed with the Track-A click ledger (#465). See the
package docstring in `alphalens_feedback/__init__.py`.
