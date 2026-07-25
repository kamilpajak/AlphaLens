"""Acceptance suite for the broker-agnostic auto-manager.

A single, readable Given/When/Then spec of WHAT the auto-manager guarantees,
run against a fake in-memory broker (never Saxo). Doubles as the human-readable
contract: the class docstrings state each guarantee in plain business language.

- ``fake_broker.FakeBroker`` — a stateful in-memory broker that implements the
  generic ``Broker`` Protocol + the exit-capability Protocols. Because it is not
  Saxo, the suite also proves the manager is genuinely broker-agnostic.
- ``world.ManagerWorld`` — the scenario DSL. Every test reads as a sentence;
  the mechanics (uic numbers, journals, env flags) live inside the DSL.
"""
