# LIVE auto-manager drop-ins — the deliberate production overrides

The base unit `../alphalens-broker-manager-live.service` ships the conservative
soak pins: placement disarmed, one open position, quarter gross, the tightest
fee floor, entry trailing off. Installing the base unit alone therefore starts a
daemon that reconciles and journals but opens nothing new.

Every file here is a decision to run production **wider or armed** compared with
that baseline. One concern per file, named for the decision, so `ls` answers
"what did we deliberately turn on" without reading any values. systemd applies
drop-ins after the base unit in lexical filename order, so each value below wins
over the base unit's line for the same variable.

No variable is set by more than one file here, so the order does not currently
decide anything. The numeric prefixes exist so that it never has to be
discovered the hard way: the SIM unit, which uses bare names, already had to
grow a file called `zz-oco-disable.conf` to win an ordering fight with
`oco-enable.conf`.

Operating the LIVE overrides as drop-ins mirrors how the SIM unit has been run
since 2026-07-24.

## Why this directory exists

Until 2026-08-25 these values existed **only** on the VPS: four in an untracked
drop-in and four typed straight into the installed copy of the base unit. The
repository declared different numbers for five of them, so reading
`deploy/systemd/` gave a confidently wrong answer about how much money the live
rail was risking, and re-installing the unit from the repository would have
silently changed live trading behaviour. Issue #1121.

## Install

    cp -r deploy/systemd/alphalens-broker-manager-live.service.d ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user restart alphalens-broker-manager-live.service

Verify what actually took effect — the composed environment, not the files:

    systemctl --user show alphalens-broker-manager-live.service -p Environment

## Changing a value here

These are risk decisions. Change one in a commit of its own, say why in the
commit message, and record the date in the file. Six of the eight rails are also
bounded by `assert_live_rails()`, which refuses to boot a value outside its
range — widening past a bound is a code change in `live_rails.py`, deliberately.

## Known gap

The SIM unit's eleven drop-ins are still untracked (issue #1121 covers the LIVE
side only). They carry no real money, and two of them contradict each other
(`oco-enable.conf` then `zz-oco-disable.conf`), so tracking them verbatim would
version a mess rather than a decision. That cleanup is its own task.
