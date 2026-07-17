"""Saxo Bank OpenAPI adapter (SIM-only) for the broker-agnostic layer.

No ``__status__`` here — the parent ``brokers`` package carries it. Layering
inside this subpackage is strictly one-way::

    broker.py -> client.py -> tokens.py -> errors.py

``client.py`` is THE canonical Saxo HTTP surface (one-client-per-vendor
doctrine; enforced by ``tests/test_no_raw_saxo_http.py``); ``broker.py``
adapts it to the broker-agnostic ``contract.Broker`` Protocol and is the only
module the registry touches.
"""
