"""Canonical stamp of the load-bearing ladder-replay config.

A future change to any replay constant -- the time-stop horizon, the order-TTL
fallback, the arrival-VWAP window, the ratchet rule, the same-bar tiebreak --
would silently make new outcome rows incomparable to old ones replayed under the
prior geometry. Stamping a deterministic version token on every resolved row lets
a tuning analyst ``GROUP BY ladder_config_version`` and detect exactly when the
geometry changed, instead of blending two regimes into one mean.

The token is the value record AND the version identity at once: identical configs
serialise to byte-identical strings (sorted keys), so a plain SQL ``GROUP BY``
partitions rows by geometry with no parsing.

Design note -- per-row token, not a global code-version: the token folds in the
``order_ttl_days`` ACTUALLY used for the row, which a candidate's
``brief_trade_setup`` can override. So the token is a row-specific geometry hash,
not a constant-only "code version" that changes only on a deploy. That is the
intended trade-off: two rows replayed under different entry-TTLs ARE different
configs and must not compare equal (this is exactly what surfaced the 10-vs-7
divergence). An analyst wanting the "before/after a constant change" cut
independent of per-row TTL can read the ``schema`` field or the dedicated
``time_stop_days`` / ``arrival_vwap_window_min`` keys out of the token.
"""

from __future__ import annotations

import json

from alphalens_pipeline.feedback.bar_window import ARRIVAL_VWAP_WINDOW_MIN
from alphalens_pipeline.paper.constants import TIME_STOP_DAYS

# Bumped ONLY when the SHAPE of the stamp changes (a key added / removed /
# renamed), NEVER when a value changes -- a value change is the whole point of
# the stamp and must surface as a different token, not a schema bump.
_STAMP_SCHEMA = 1

# String ids for the two hard-coded replay policies in ``ladder_replay.py``. They
# are ids, not the values themselves: if a rule is ever swapped (e.g. the tiebreak
# flips to tp-first), bump the id so old rows stay distinguishable from new ones.
_RATCHET_RULE = "be_after_tp1_lock_after_tp2"  # break-even after TP1, lock after TP2
_TIEBREAK_RULE = "sl_first"  # same-bar TP-vs-SL ambiguity resolves to the stop


def ladder_config_version(*, order_ttl_days: int) -> str:
    """Return a canonical JSON token of the config that produced a replay row.

    ``order_ttl_days`` is the value ACTUALLY used for this row (a candidate's
    ``brief_trade_setup`` may override the default fallback), so two rows replayed
    under different entry-TTLs carry different tokens by construction -- the
    10-vs-7 kind of divergence becomes visible rather than silent.

    The remaining fields are read from the live constants at call time, so a
    constant edit between deploys yields a new token on the next run.
    """
    config = {
        "schema": _STAMP_SCHEMA,
        "time_stop_days": int(TIME_STOP_DAYS),
        "order_ttl_days": int(order_ttl_days),
        "arrival_vwap_window_min": int(ARRIVAL_VWAP_WINDOW_MIN),
        "ratchet_rule": _RATCHET_RULE,
        "tiebreak_rule": _TIEBREAK_RULE,
    }
    return json.dumps(config, sort_keys=True, separators=(",", ":"))
