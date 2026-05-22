"""Pre-audit smoke framework — fail fast before a long-running audit.

Driven by 2026-05-11 launch failure: precheck guard tried to load
2014-2017 iVolatility SMD data not present on RunPod, aborting both
audit windows after ~1.5 min of setup compute per phase. The smoke
runner catches that class of failure in <2 min before any tmux
launch.

Public surface:
- :class:`alphalens_research.preaudit.profiles.SmokeProfile` — per-strategy
  declarative config (smoke window, args, data deps).
- :class:`alphalens_research.preaudit.profiles.SMOKE_PROFILES` — registry keyed
  by strategy name (must intersect :data:`alphalens_cli.commands.audit._SCRIPTS`).
- :func:`alphalens_research.preaudit.coverage.check_all_deps` — validate data
  presence + coverage for a profile.
- :func:`alphalens_research.preaudit.runner.run_smoke` — execute a tiny
  end-to-end phase; ephemeral ``--out`` path; ≤5 min timeout.

Scope limitations (per zen review 2026-05-11):
- Does NOT catch OOM-at-scale (cap=300 smoke vs cap=2000 full universe).
- Does NOT catch MooseFS I/O contention under N concurrent workers
  (smoke runs single-process; see
  ``feedback_runpod_moosefs_process_pool_antipattern.md``).
- Catches: missing data, coverage gap, hash drift, const drift,
  CLI passthrough breakage, end-to-end pipeline failure.
"""

__status__ = "ACTIVE"
