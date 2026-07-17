"""Broker-agnostic execution layer — Saxo first, SIM-only (ADR 0014).

The execution layer is a downstream consumer of the T5 SETUP output in the
ADR 0013 trade-side map. Hard rules inherited from that ADR:

- **R2** — no broker/execution output (fills, rejections, balances) ever
  feeds T2 SELECTION (enforced by ``tests/test_module_dependencies.py``);
- **R3** — order placement (P2) carries its own ``execution_config_version``
  poolability key;
- **T8 no-pooling** — live fills are a NEW measurement source, keyed
  separately from the broker-free price-path replays, never silently merged.

Package layering (strict, one-way)::

    contract.py  (broker-agnostic; zero vendor imports)
    registry.py  (lazy factory map -> Broker)
    saxo/broker.py -> saxo/client.py -> saxo/tokens.py -> saxo/errors.py

Consumers (CLI, future reconciler) import ONLY ``contract`` + ``registry``.
P1 ships reads (account / positions / instrument resolution) on the Saxo SIM
environment; the LIVE gateway is structurally unreachable
(``saxo/client.py::LIVE_TRADING_ENABLED`` rail, lifted only by a future ADR).
Design memo: ``docs/research/saxo_broker_layer_design_2026_07_17.md``.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
