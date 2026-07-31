"""Pure exit-geometry leaf.

Single source of truth for the ATR bracket exit computation used today by the
``/edge`` what-if lens (``atr_bracket_1p5`` / "bezpazery") and, later, by the
SIM broker-manager once it is wired for order placement. Stdlib-only so this
package can be relocated unchanged into the future ``broker_contract``
extraction (see the broker-manager extraction design memo,
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``)
without carrying any research-side or broker-side dependency along with it.
"""

__status__ = "ACTIVE"
