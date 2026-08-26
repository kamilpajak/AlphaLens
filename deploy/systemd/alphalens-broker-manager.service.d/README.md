# SIM auto-manager drop-ins — the soak configuration, stated in the repo

The base unit `../alphalens-broker-manager.service` starts a SIM daemon that
reconciles and journals but places nothing (no arm, every feature flag at its
off default). Every file here is one deliberate decision that shapes the soak;
`ls` answers "what does SIM actually run" without reading any values.

`apps/alphalens-research/tests/test_deploy_systemd_units.py`
(`TestSimBrokerManagerDropIns`) pins the composed environment of this
directory to the exact set measured on the VPS on 2026-08-26, so a variable
added, dropped, or changed on the repo side turns CI red. A hand edit on the
HOST is still invisible from here — that is #1135.

## Why this directory exists (#1136)

Until 2026-08-26 the SIM configuration existed only on the VPS: eleven
untracked drop-ins, accumulated file by file since 2026-07-24. Reading
`deploy/systemd/` told you SIM ran the base unit's defaults; it actually ran
armed, ten positions wide, on a declared 100k frame with trailing exits.
Every soak conclusion ("SIM validated this") was drawn under a configuration
that was not written down anywhere.

Two of the eleven files set the same variable to opposite values:
`oco-enable.conf` (2026-07-24) and `zz-oco-disable.conf` (2026-08-06), the
`zz-` prefix existing only to win systemd's lexical ordering. The numeric
`NN-` prefixes here, plus the no-duplicate-variable test, keep that from
recurring. A twelfth file, `live-prices.conf.disabled-for-live-soak`, was a
staged-but-inert activation note from 2026-08-10 whose path was superseded by
the LIVE unit work (ADR 0015/0017); it was deleted in the cutover rather than
versioned.

## Operating rules (same doctrine as the LIVE directory)

- One concern per file, named for the decision, `NN-` prefix.
- Change a value by editing the tracked file here and re-installing — never
  by editing files on the host (that is the practice that produced the
  eleven-file drift).
- Install copies **files**, not the directory: `cp <src>/*.conf <dest>/`
  (`cp -r` of the directory into an existing destination nests it one level
  down where systemd never reads it — the #1137 trap).
- After any change: `systemctl --user daemon-reload && systemctl --user
  restart alphalens-broker-manager`, then verify the composed environment
  with `systemctl --user show alphalens-broker-manager -p Environment`.
