"""Every entry-trail FIRE on record, with the ceiling each one was armed with.

NOT a test module — a shared fixture imported by
``tests/brokers/automanager/test_entry_trail_ceiling_breach.py``.

Issue #1317 in one line: the G1 gap-through ceiling
(``entry_trailing_design_2026_08_12.md`` §3 G1, sent as ``StopLimitPrice`` on the
native ``TrailingStopIfTraded`` order) does NOT bind — 8 of these 23 fires
executed ABOVE the ceiling their own order carried.

**Provenance.** Read 2026-09-04 from the VPS, by joining two sources on the
broker order id:

- the ceiling and trigger from the daemons' journald arm line,
  ``entry-trail <label>: armed native trailing order <id> @ trigger T (ceiling C)``
  (``journalctl --user -u alphalens-broker-manager.service
  -u alphalens-broker-manager-live.service --since 2026-08-01``);
- the realized fill from the terminal ``fired`` records in
  ``~/.alphalens/broker_orders/{sim,live}/entry_trails.jsonl``.

The join is the reason this file exists. journald rotates, and the armed ceiling
was NEVER written to the journal before the change these constants pin — so
without this snapshot the whole measurement becomes unrepeatable.

**Do NOT reconstruct the ceiling from the ``fired`` measurement.** The obvious
shortcut, ``would_be_trigger * (1 + CEILING_EPS_FRAC)``, reproduces the arm line
exactly on 22 of these 23 rows and is wrong by −0.3324 on BAH: the ``fired``
blob's ``would_be_trigger`` is derived from the MINIMUM trough ever journaled,
while the arm used the trough as it stood at arm time. :data:`BAH_DERIVED_CEILING`
pins that gap so nobody reintroduces the shortcut.

**Reading rule for the SIM rows.** SIM fills are synthetic (see the 2026-08-07
probe: SIM filled a deep-through limit and a near-touch limit at the SAME
reference price), so the 18 SIM rows show that SIM's engine ignores the field —
they are not evidence about the real matching engine. The single LIVE breach
(AMBA) is. Both are kept because the question is whether the field binds AT ALL,
and neither source alone answers it.
"""

from __future__ import annotations

from typing import NamedTuple


class EntryTrailFire(NamedTuple):
    """One reconciled entry-trail fire, as measured."""

    crid: str
    order_id: str
    env: str  # "SIM" | "LIVE"
    trigger: float  # the initial trigger the arm POSTed (OrderPrice)
    ceiling: float  # the ceiling the arm POSTed (StopLimitPrice)
    fill: float  # the realized avg fill price (fired.avg_price)

    @property
    def breached(self) -> bool:
        """Whether this fire executed ABOVE the ceiling its own order carried."""
        return self.fill > self.ceiling


FIRES: tuple[EntryTrailFire, ...] = tuple(
    EntryTrailFire(*row)
    for row in (
        ("MRVI-2026-08-26-entry-t0", "5039891030", "SIM", 8.4018, 8.4186, 8.2),
        ("MARA-2026-08-25-entry-t0", "5039891044", "SIM", 11.6178, 11.641, 11.52),
        ("PSNL-2026-08-26-entry-t0", "5039891053", "SIM", 16.6629, 16.6962, 16.73),
        ("IMCR-2026-08-26-entry-t0", "5039891931", "SIM", 36.4714, 36.5444, 36.74),
        ("MARA-2026-08-25-entry-t1", "5039893228", "SIM", 11.0952, 11.1174, 11.05),
        ("IBRX-2026-08-25-entry-t0", "5039893270", "SIM", 8.0903, 8.1064, 8.08),
        ("PL-2026-08-26-entry-t0", "5039895886", "SIM", 20.5322, 20.5732, 20.46),
        ("MRVI-2026-08-26-entry-t1", "5039899803", "SIM", 7.8591, 7.8748, 7.8),
        ("SAIC-2026-08-26-entry-t0", "5039902058", "SIM", 125.6652, 125.9165, 126.09),
        ("GME-2026-08-27-entry-t0", "5039898744", "SIM", 18.1001, 18.1363, 18.02),
        ("IOVA-2026-08-26-entry-t0", "5039932464", "SIM", 7.8088, 7.8245, 7.77),
        ("LPX-2026-08-27-entry-t0", "5039933050", "SIM", 69.6465, 69.7858, 69.84),
        ("PFSI-2026-08-25-entry-t0", "5039932448", "SIM", 73.1137, 73.26, 73.12),
        ("IBRX-2026-08-25-entry-t1", "5039948009", "SIM", 7.7888, 7.8043, 7.79),
        ("PL-2026-08-26-entry-t1", "5039954464", "SIM", 19.2457, 19.2842, 19.18),
        ("BAH-2026-08-26-entry-t0", "5039978418", "SIM", 74.5006, 74.6497, 75.0),
        ("PL-2026-08-26-entry-t2", "5040006542", "SIM", 18.3412, 18.3779, 18.39),
        ("RHI-2026-09-03-entry-t1", "5040027337", "SIM", 42.1497, 42.234, 42.34),
        ("OLN-2026-08-16-entry-t0", "5435139849", "LIVE", 18.7031, 18.7405, 18.5574),
        ("SMG-2026-08-19-entry-t0", "5436761165", "LIVE", 60.0689, 60.189, 59.9261),
        ("GME-2026-08-27-entry-t0", "5438283280", "LIVE", 18.1001, 18.1363, 18.02),
        ("RHI-2026-09-02-entry-t0", "5439436793", "LIVE", 42.8733, 42.959, 42.87),
        ("AMBA-2026-09-04-entry-t0", "5440084826", "LIVE", 58.8327, 58.9504, 59.0),
    )
)
"""Every fire on record at 2026-09-04, SIM first then LIVE, in journal order."""

EXPECTED_BREACHES = 8
"""8 of 23 — the headline number of #1317. Pinned so a detector that stops
detecting fails a test instead of going quiet."""

AMBA = FIRES[-1]
"""The LIVE fire that opened the issue: ceiling 58.9504, fill 59.00."""

AMBA_BREACH_ABS = 0.04959999999999809
"""``AMBA.fill - AMBA.ceiling`` — about 5 ticks over the cap."""

AMBA_BREACH_BPS = 8.413853001845295
"""``AMBA_BREACH_ABS / AMBA.ceiling * 1e4``."""

BAH = FIRES[15]
"""The largest breach on record (SIM): ceiling 74.6497, fill 75.00."""

BAH_BREACH_ABS = 0.3503000000000043
BAH_BREACH_BPS = 46.925841630978326

BAH_DERIVED_CEILING = 74.31733799999998
"""What ``would_be_trigger (73.80 * 1.005) * (1 + CEILING_EPS_FRAC)`` gives for
BAH — 0.3324 BELOW the ceiling the order was actually armed with. The shortcut
matches on the other 22 rows and is wrong here, which is why the ceiling is
journaled rather than derived."""
