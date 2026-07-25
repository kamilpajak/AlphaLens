# What the auto-manager guarantees

This folder is the **plain-language contract** for the Saxo/broker-agnostic
auto-manager. Each test reads like a short story — a few *GIVEN* lines set the
scene, one *WHEN* line runs a management cycle, one *THEN* line checks a promise.
You do not need to read the mechanics (order ids, journals, flags) to understand
what is promised; the class names and the *GIVEN / WHEN / THEN* comments say it.

The tests run the **real** manager against a **fake, in-memory broker** (never a
live account). Because that fake broker is *not* Saxo, the suite also proves the
manager doesn't secretly depend on anything Saxo-specific.

## The six promises

1. **The safety rails are respected** (`test_safety_rails.py`) — the manager will
   not open new risk when it shouldn't: a master "orders off" switch, a cap on
   open positions, a gross-exposure cap, a daily-loss cutoff, and an emergency
   KILL file each stop new orders. The KILL switch still lets it *protect* what it
   already holds.
2. **Every position is protected** (`test_every_position_protected.py`) — the
   moment it can see it holds shares, it covers all of them with a stop. It works
   this out from what the broker actually reports, so even a position it never
   placed itself gets protected.
3. **It never sells more than it owns** (`test_no_oversell.py`) — the total "sell"
   resting to protect a position never exceeds the shares held; a two-legged OCO
   exit counts as one commitment, not two.
4. **It keeps going when the broker misbehaves** (`test_resilience.py`) — a problem
   with one position never stops it protecting the others, and a broker that
   refuses the richer exit gets a plain stop instead of nothing.
5. **It never fails silently** (`test_never_silent.py`) — whenever something goes
   wrong or degrades, it raises an alert. The operator is always told.
6. **It manages each position to its end** (`test_terminal.py`) — a filled entry
   ends the cycle protected; a cancelled entry has its leftover orders cleaned up.

## How to read a test

```python
def test_a_fresh_fill_gets_a_protective_stop(self):
    world = ManagerWorld(self)
    world.entry_fills("KO", shares=100, stop=44.0)   # GIVEN a KO trade opened
    world.run_tick()                                  # WHEN one cycle runs
    world.assert_protected("KO")                      # THEN all 100 shares are covered
```

The vocabulary (`entry_fills`, `run_tick`, `assert_protected`, `orders_are_disabled`,
`broker_rejects_oco`, …) lives in `world.py`. Adding a new guaranteed behaviour is
usually one new sentence built from that vocabulary.
